[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$StateRoot = $(if ($env:LOCALAPPDATA) {
        Join-Path $env:LOCALAPPDATA 'agent-chat-session-sync'
    }
    else {
        throw 'LOCALAPPDATA is required.'
    }),
    [string]$TempRoot = $(if ($env:ACSS_TEMP_DIR) {
        $env:ACSS_TEMP_DIR
    }
    else {
        [IO.Path]::GetTempPath()
    }),
    [string]$PythonPackage,
    [string]$ExpectedPythonSha256,
    [string]$CcConnectBinary,
    [string]$CcConnectTarget,
    [string]$ExpectedCcConnectSha256,
    [switch]$RestartCcConnect,
    [string]$RollbackManifest
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'PowerShell 7 or newer is required.'
}
if (-not $IsWindows) {
    throw 'This installer requires Windows.'
}
if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne [Runtime.InteropServices.Architecture]::X64 -or -not [Environment]::Is64BitProcess) {
    throw 'This installer supports Windows x64 only.'
}

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$StateRoot = [IO.Path]::GetFullPath($StateRoot)
$TempRoot = [IO.Path]::GetFullPath($TempRoot)
$venvPath = [IO.Path]::GetFullPath((Join-Path $StateRoot 'venv'))
$statePrefix = $StateRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $venvPath.StartsWith($statePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "The dedicated venv must remain inside StateRoot: $StateRoot"
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,
        [Parameter(Mandatory)]
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE"
    }
}

function Write-Manifest {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [hashtable]$Manifest
    )

    $json = $Manifest | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText(
        $Path,
        $json + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
}

function Backup-File {
    param(
        [Parameter(Mandatory)]
        [string]$Source,
        [Parameter(Mandatory)]
        [string]$BackupDirectory,
        [Parameter(Mandatory)]
        [string]$Name
    )

    $record = @{
        path = [IO.Path]::GetFullPath($Source)
        existed = Test-Path -LiteralPath $Source -PathType Leaf
        backup = ''
        recovery = "$Name.created"
        original_sha256 = ''
    }
    if ($record.existed) {
        $destination = Join-Path $BackupDirectory $Name
        [IO.File]::Copy($Source, $destination, $false)
        $record.backup = $Name
        $record.original_sha256 = (
            Get-FileHash -LiteralPath $Source -Algorithm SHA256
        ).Hash.ToLowerInvariant()
    }
    return $record
}

function Restore-File {
    param(
        [Parameter(Mandatory)]
        [hashtable]$Record,
        [Parameter(Mandatory)]
        [string]$BackupDirectory
    )

    $target = [string]$Record.path
    if ([bool]$Record.existed) {
        $parent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        [IO.File]::Copy((Join-Path $BackupDirectory ([string]$Record.backup)), $target, $true)
        return
    }
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        $created = Join-Path $BackupDirectory 'created-during-failed-operation'
        New-Item -ItemType Directory -Path $created -Force | Out-Null
        Move-Item `
            -LiteralPath $target `
            -Destination (Join-Path $created ([string]$Record.recovery))
    }
}

function Get-TaskSnapshot {
    $task = Get-ScheduledTask `
        -TaskPath '\AgentChatSessionSync\' `
        -TaskName 'Worker' `
        -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return @{
            existed = $false
            state = ''
            xml = ''
        }
    }
    return @{
        existed = $true
        state = $task.State.ToString()
        xml = Export-ScheduledTask `
            -TaskPath '\AgentChatSessionSync\' `
            -TaskName 'Worker'
    }
}

function Get-CcConnectSnapshot {
    param(
        [Parameter(Mandatory)]
        [string]$Binary
    )

    if (-not (Test-Path -LiteralPath $Binary -PathType Leaf)) {
        return @{
            existed = $false
            installed = $false
            running = $false
        }
    }
    $output = & $Binary daemon status 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "cc-connect daemon status exited with code $LASTEXITCODE"
    }
    $text = $output -join [Environment]::NewLine
    return @{
        existed = $true
        installed = $text -notmatch 'Status:\s+Not installed'
        running = $text -match 'Status:\s+Running'
    }
}

function Restore-Task {
    param(
        [Parameter(Mandatory)]
        [hashtable]$Snapshot
    )

    if ([bool]$Snapshot.existed) {
        Register-ScheduledTask `
            -TaskPath '\AgentChatSessionSync\' `
            -TaskName 'Worker' `
            -Xml ([string]$Snapshot.xml) `
            -Force | Out-Null
        if ([string]$Snapshot.state -eq 'Running') {
            Start-ScheduledTask `
                -TaskPath '\AgentChatSessionSync\' `
                -TaskName 'Worker'
        }
    }
}

function Remove-ReplacementTask {
    param(
        [Parameter(Mandatory)]
        [string]$AcssExecutable
    )

    if (-not (Test-Path -LiteralPath $AcssExecutable -PathType Leaf)) {
        throw "identity-safe Task uninstaller not found: $AcssExecutable"
    }
    & $AcssExecutable uninstall-service
    if ($LASTEXITCODE -ne 0) {
        throw "identity-safe Task uninstall exited with code $LASTEXITCODE"
    }
}

function Rollback-Operation {
    param(
        [Parameter(Mandatory)]
        [hashtable]$Context
    )

    $errors = [Collections.Generic.List[string]]::new()
    $binaryBackup = Join-Path $Context.backup_dir 'cc-connect.exe'
    $binaryBackupAvailable = Test-Path -LiteralPath $binaryBackup -PathType Leaf
    $targetBinaryExists = Test-Path -LiteralPath $Context.cc_connect_target -PathType Leaf
    $newBinaryNeedsRemoval = ([bool]$Context.binary_new_installed -or $binaryBackupAvailable -or -not [bool]$Context.binary_existed) -and $targetBinaryExists
    $binaryIntentProducedTarget = [bool]$Context.binary_intent -and -not [bool]$Context.binary_existed -and $targetBinaryExists
    $binaryRollbackNeeded = [bool]$Context.binary_new_installed -or [bool]$Context.binary_backup_moved -or $binaryBackupAvailable -or $binaryIntentProducedTarget
    if ([bool]$Context.cc_connect_new_started) {
        try {
            Invoke-Checked $Context.cc_connect_target @('daemon', 'stop')
        }
        catch {
            $errors.Add("new cc-connect daemon stop: $($_.Exception.Message)")
        }
    }
    if ($binaryRollbackNeeded) {
        try {
            if ($newBinaryNeedsRemoval) {
                Move-Item `
                    -LiteralPath $Context.cc_connect_target `
                    -Destination (Join-Path $Context.backup_dir 'failed-cc-connect.exe') `
                    -Force
            }
            if ($binaryBackupAvailable) {
                Move-Item `
                    -LiteralPath $binaryBackup `
                    -Destination $Context.cc_connect_target `
                    -Force
            }
            if ([bool]$Context.cc_connect_snapshot.running) {
                Invoke-Checked $Context.cc_connect_target @('daemon', 'start')
            }
        }
        catch {
            $errors.Add("cc-connect binary: $($_.Exception.Message)")
        }
    }
    elseif ([bool]$Context.cc_connect_stopped -and [bool]$Context.cc_connect_snapshot.running) {
        try {
            Invoke-Checked $Context.cc_connect_target @('daemon', 'start')
        }
        catch {
            $errors.Add("cc-connect restart: $($_.Exception.Message)")
        }
    }
    $replacementTaskRemoved = $true
    if ([bool]$Context.task_changed) {
        try {
            Remove-ReplacementTask -AcssExecutable $Context.acss_executable
        }
        catch {
            $replacementTaskRemoved = $false
            $errors.Add("replacement Task removal: $($_.Exception.Message)")
        }
    }
    $fileRecords = @($Context.files)
    [array]::Reverse($fileRecords)
    foreach ($record in $fileRecords) {
        try {
            Restore-File -Record $record -BackupDirectory $Context.backup_dir
        }
        catch {
            $errors.Add("$($record.path): $($_.Exception.Message)")
        }
    }
    $venvBackup = Join-Path $Context.backup_dir 'venv'
    $venvBackupAvailable = Test-Path -LiteralPath $venvBackup -PathType Container
    if ([bool]$Context.venv_intent -or $venvBackupAvailable) {
        try {
            $currentVenvExists = Test-Path -LiteralPath $Context.venv_path -PathType Container
            if ($currentVenvExists -and ([bool]$Context.venv_new_installed -or $venvBackupAvailable)) {
                Move-Item `
                    -LiteralPath $Context.venv_path `
                    -Destination (Join-Path $Context.backup_dir 'failed-venv')
            }
            if ($venvBackupAvailable) {
                Move-Item `
                    -LiteralPath $venvBackup `
                    -Destination $Context.venv_path
            }
        }
        catch {
            $errors.Add("venv: $($_.Exception.Message)")
        }
    }
    if ([bool]$Context.task_changed -and $replacementTaskRemoved) {
        try {
            Restore-Task -Snapshot $Context.task_snapshot
        }
        catch {
            $errors.Add("original Task restore: $($_.Exception.Message)")
        }
    }
    return $errors
}

function Assert-RollbackContext {
    param(
        [Parameter(Mandatory)]
        [hashtable]$Context,
        [Parameter(Mandatory)]
        [string]$ManifestFile,
        [Parameter(Mandatory)]
        [string]$StateRoot,
        [string]$ExplicitCcConnectTarget
    )

    $manifestDirectory = [IO.Path]::GetFullPath((Split-Path -Parent $ManifestFile))
    if (
        [IO.Path]::GetFullPath([string]$Context.backup_dir) -ne
        $manifestDirectory
    ) {
        throw 'rollback manifest backup_dir does not match its parent directory'
    }
    $expectedVenv = [IO.Path]::GetFullPath((Join-Path $StateRoot 'venv'))
    if ([IO.Path]::GetFullPath([string]$Context.venv_path) -ne $expectedVenv) {
        throw 'rollback manifest venv_path does not match StateRoot'
    }
    $expectedAcss = [IO.Path]::GetFullPath(
        (Join-Path $expectedVenv 'Scripts\agent-chat-session-sync.exe')
    )
    if (
        [string]$Context.acss_executable -and
        [IO.Path]::GetFullPath([string]$Context.acss_executable) -ne $expectedAcss
    ) {
        throw 'rollback manifest acss_executable does not match StateRoot'
    }

    $ccConfig = if ($env:CC_CONNECT_CONFIG) {
        [IO.Path]::GetFullPath($env:CC_CONNECT_CONFIG)
    }
    else {
        [IO.Path]::GetFullPath((Join-Path $HOME '.cc-connect\config.toml'))
    }
    $codexHome = if ($env:CODEX_HOME) {
        [IO.Path]::GetFullPath($env:CODEX_HOME)
    }
    else {
        [IO.Path]::GetFullPath((Join-Path $HOME '.codex'))
    }
    $claudeHome = if ($env:CLAUDE_HOME) {
        [IO.Path]::GetFullPath($env:CLAUDE_HOME)
    }
    else {
        [IO.Path]::GetFullPath((Join-Path $HOME '.claude'))
    }
    $allowedFiles = @{
        $ccConfig = 'cc-connect-config.toml'
        ([IO.Path]::GetFullPath((Join-Path $codexHome 'config.toml'))) = 'codex-config.toml'
        ([IO.Path]::GetFullPath((Join-Path $codexHome 'hooks.json'))) = 'codex-hooks.json'
        ([IO.Path]::GetFullPath((Join-Path $claudeHome 'settings.json'))) = 'claude-settings.json'
        ([IO.Path]::GetFullPath((Join-Path $StateRoot 'service\worker.ps1'))) = 'worker.ps1'
    }
    foreach ($record in @($Context.files)) {
        $recordPath = [IO.Path]::GetFullPath([string]$record.path)
        if (-not $allowedFiles.ContainsKey($recordPath)) {
            throw "rollback manifest contains unmanaged file path: $recordPath"
        }
        $expectedName = [string]$allowedFiles[$recordPath]
        if ([bool]$record.existed -and [string]$record.backup -ne $expectedName) {
            throw "rollback manifest has invalid backup name for $recordPath"
        }
        if ([string]$record.recovery -ne "$expectedName.created") {
            throw "rollback manifest has invalid recovery name for $recordPath"
        }
    }

    $hasBinaryOperation = (
        [bool]$Context.binary_intent -or
        [bool]$Context.binary_backup_moved -or
        [bool]$Context.binary_new_installed -or
        [bool]$Context.cc_connect_stopped -or
        [bool]$Context.cc_connect_new_started -or
        (Test-Path -LiteralPath (Join-Path $manifestDirectory 'cc-connect.exe') -PathType Leaf)
    )
    if ($hasBinaryOperation) {
        if (-not $ExplicitCcConnectTarget) {
            throw 'CcConnectTarget is required to roll back a binary operation'
        }
        $expectedTarget = [IO.Path]::GetFullPath($ExplicitCcConnectTarget)
        if (
            [IO.Path]::GetFullPath([string]$Context.cc_connect_target) -ne
            $expectedTarget
        ) {
            throw 'rollback manifest cc_connect_target does not match CcConnectTarget'
        }
    }
}

if ($RollbackManifest) {
    $manifestFile = [IO.Path]::GetFullPath($RollbackManifest)
    $backupRoot = [IO.Path]::GetFullPath((Join-Path $StateRoot 'backups'))
    $backupPrefix = $backupRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $manifestFile.StartsWith($backupPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "RollbackManifest must be inside $backupRoot"
    }
    if (-not (Test-Path -LiteralPath $manifestFile -PathType Leaf)) {
        throw "Rollback manifest not found: $manifestFile"
    }
    $rollbackContext = (
        Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8
        | ConvertFrom-Json -AsHashtable
    )
    Assert-RollbackContext `
        -Context $rollbackContext `
        -ManifestFile $manifestFile `
        -StateRoot $StateRoot `
        -ExplicitCcConnectTarget $CcConnectTarget
    if (-not $PSCmdlet.ShouldProcess($manifestFile, 'Roll back installation')) {
        return
    }
    $rollbackContext.status = 'rolling_back'
    Write-Manifest -Path $manifestFile -Manifest $rollbackContext
    $rollbackErrors = Rollback-Operation -Context $rollbackContext
    $rollbackContext.status = if ($rollbackErrors.Count) {
        'rollback_failed'
    }
    else {
        'rolled_back'
    }
    $rollbackContext.rollback_errors = @($rollbackErrors)
    Write-Manifest -Path $manifestFile -Manifest $rollbackContext
    if ($rollbackErrors.Count) {
        throw "rollback errors: $($rollbackErrors -join '; ')"
    }
    Write-Host "rolled back $manifestFile"
    return
}

$git = (Get-Command git -ErrorAction Stop).Source
$uv = (Get-Command uv -ErrorAction Stop).Source
$dirty = (& $git -C $root status --porcelain)
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to inspect the source worktree.'
}
if ($dirty) {
    throw 'Refusing to install from a dirty source worktree.'
}
$commit = (& $git -C $root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $commit) {
    throw 'Unable to resolve the source commit.'
}
$pythonInstallSource = $root
$pythonPackageHash = ''
if ($PythonPackage -or $ExpectedPythonSha256) {
    if (-not $PythonPackage -or -not $ExpectedPythonSha256) {
        throw 'PythonPackage and ExpectedPythonSha256 must be supplied together.'
    }
    $PythonPackage = [IO.Path]::GetFullPath($PythonPackage)
    if (-not (Test-Path -LiteralPath $PythonPackage -PathType Leaf)) {
        throw "Python package candidate not found: $PythonPackage"
    }
    if ([IO.Path]::GetExtension($PythonPackage) -ne '.whl') {
        throw "PythonPackage must be a wheel: $PythonPackage"
    }
    $pythonPackageHash = (
        Get-FileHash -LiteralPath $PythonPackage -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($pythonPackageHash -ne $ExpectedPythonSha256.Trim().ToLowerInvariant()) {
        throw "Python package SHA-256 mismatch: expected $ExpectedPythonSha256; actual $pythonPackageHash"
    }
    $pythonInstallSource = $PythonPackage
}

$doPython = $PSCmdlet.ShouldProcess(
    "$venvPath and current-user configuration files",
    'Install Python package and Windows configuration'
)
$doHooks = $PSCmdlet.ShouldProcess(
    'current-user Codex and Claude Code Hook files',
    'Install Codex and Claude Code Hooks'
)
$doTask = $PSCmdlet.ShouldProcess(
    '\AgentChatSessionSync\Worker',
    'Install worker Scheduled Task'
)
$doBinary = $false
if ($CcConnectBinary -or $CcConnectTarget -or $ExpectedCcConnectSha256) {
    if (-not $CcConnectBinary -or -not $CcConnectTarget -or -not $ExpectedCcConnectSha256) {
        throw 'CcConnectBinary, CcConnectTarget, and ExpectedCcConnectSha256 must be supplied together.'
    }
    $CcConnectBinary = [IO.Path]::GetFullPath($CcConnectBinary)
    $CcConnectTarget = [IO.Path]::GetFullPath($CcConnectTarget)
    if (-not (Test-Path -LiteralPath $CcConnectBinary -PathType Leaf)) {
        throw "cc-connect candidate not found: $CcConnectBinary"
    }
    $candidateHash = (
        Get-FileHash -LiteralPath $CcConnectBinary -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($candidateHash -ne $ExpectedCcConnectSha256.Trim().ToLowerInvariant()) {
        throw "cc-connect SHA-256 mismatch: expected $ExpectedCcConnectSha256; actual $candidateHash"
    }
    $doBinary = $PSCmdlet.ShouldProcess(
        $CcConnectTarget,
        'Replace cc-connect binary'
    )
}

if (-not $doPython -and -not $doHooks -and -not $doTask -and -not $doBinary) {
    Write-Host 'No changes selected.'
    return
}

$operationId = '{0}-{1}' -f @(
    [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'),
    [Guid]::NewGuid().ToString('N')
)
$backupDir = Join-Path $StateRoot "backups\$operationId"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$manifestPath = Join-Path $backupDir 'manifest.json'
$context = @{
    operation_id = $operationId
    source_commit = $commit
    python_package = $PythonPackage
    python_package_sha256 = $pythonPackageHash
    backup_dir = $backupDir
    status = 'snapshot'
    files = @()
    task_snapshot = @{}
    task_changed = $false
    venv_path = $venvPath
    venv_existed = $false
    venv_intent = $false
    venv_backup_moved = $false
    venv_new_installed = $false
    acss_executable = ''
    binary_existed = $false
    binary_intent = $false
    binary_backup_moved = $false
    binary_new_installed = $false
    cc_connect_snapshot = @{
        existed = $false
        installed = $false
        running = $false
    }
    cc_connect_stopped = $false
    cc_connect_new_started = $false
    cc_connect_target = $CcConnectTarget
}
Write-Manifest -Path $manifestPath -Manifest $context

$ccConfig = if ($env:CC_CONNECT_CONFIG) {
    [IO.Path]::GetFullPath($env:CC_CONNECT_CONFIG)
}
else {
    [IO.Path]::GetFullPath((Join-Path $HOME '.cc-connect\config.toml'))
}
$codexHome = if ($env:CODEX_HOME) {
    [IO.Path]::GetFullPath($env:CODEX_HOME)
}
else {
    [IO.Path]::GetFullPath((Join-Path $HOME '.codex'))
}
$claudeHome = if ($env:CLAUDE_HOME) {
    [IO.Path]::GetFullPath($env:CLAUDE_HOME)
}
else {
    [IO.Path]::GetFullPath((Join-Path $HOME '.claude'))
}

try {
    $stagedPythonPackage = $pythonInstallSource
    if ($doPython -and $PythonPackage) {
        $stagedPythonPackage = Join-Path $backupDir 'staged-python-package.whl'
        [IO.File]::Copy($PythonPackage, $stagedPythonPackage, $false)
        $stagedPythonHash = (
            Get-FileHash -LiteralPath $stagedPythonPackage -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($stagedPythonHash -ne $pythonPackageHash) {
            throw "staged Python package SHA-256 mismatch: expected $pythonPackageHash; actual $stagedPythonHash"
        }
    }
    $stagedCcConnectBinary = $CcConnectBinary
    if ($doBinary) {
        $stagedCcConnectBinary = Join-Path $backupDir 'staged-cc-connect.exe'
        [IO.File]::Copy($CcConnectBinary, $stagedCcConnectBinary, $false)
        $stagedCcConnectHash = (
            Get-FileHash -LiteralPath $stagedCcConnectBinary -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($stagedCcConnectHash -ne $candidateHash) {
            throw "staged cc-connect SHA-256 mismatch: expected $candidateHash; actual $stagedCcConnectHash"
        }
    }

    if ($doPython) {
        $context.files += @(
            Backup-File -Source $ccConfig -BackupDirectory $backupDir -Name 'cc-connect-config.toml'
            Backup-File -Source (Join-Path $codexHome 'config.toml') -BackupDirectory $backupDir -Name 'codex-config.toml'
        )
        Write-Manifest -Path $manifestPath -Manifest $context

        $stagedVenv = Join-Path $TempRoot "acss-install-venv-$operationId"
        if (Test-Path -LiteralPath $stagedVenv) {
            throw "staged venv path must not already exist: $stagedVenv"
        }
        New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
        Invoke-Checked $uv @('venv', $stagedVenv, '--python', '3.11')
        $oldBuildCommit = $env:ACSS_BUILD_COMMIT
        try {
            $env:ACSS_BUILD_COMMIT = $commit
            Invoke-Checked $uv @(
                'pip'
                'install'
                '--python'
                (Join-Path $stagedVenv 'Scripts\python.exe')
                '--force-reinstall'
                '--no-cache'
                $stagedPythonPackage
            )
        }
        finally {
            if ($null -eq $oldBuildCommit) {
                Remove-Item Env:ACSS_BUILD_COMMIT -ErrorAction SilentlyContinue
            }
            else {
                $env:ACSS_BUILD_COMMIT = $oldBuildCommit
            }
        }

        $context.venv_existed = Test-Path -LiteralPath $venvPath -PathType Container
        $context.venv_intent = $true
        Write-Manifest -Path $manifestPath -Manifest $context
        if ([bool]$context.venv_existed) {
            Move-Item `
                -LiteralPath $venvPath `
                -Destination (Join-Path $backupDir 'venv')
            $context.venv_backup_moved = $true
            Write-Manifest -Path $manifestPath -Manifest $context
        }
        Move-Item -LiteralPath $stagedVenv -Destination $venvPath
        $context.venv_new_installed = $true
        Write-Manifest -Path $manifestPath -Manifest $context
        $acss = Join-Path $venvPath 'Scripts\agent-chat-session-sync.exe'
        if (-not (Test-Path -LiteralPath $acss -PathType Leaf)) {
            throw "installed console script not found: $acss"
        }
        $context.acss_executable = $acss
        $env:ACSS_DATA_DIR = $StateRoot
        $installedProvenance = (& $acss provenance --json) | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) {
            throw "installed package provenance probe failed with code $LASTEXITCODE"
        }
        if (
            $installedProvenance.git_commit -ne $commit -or
            $installedProvenance.build_source -ne "git:$commit"
        ) {
            throw "installed Python package commit mismatch: expected $commit"
        }
        Invoke-Checked $acss @('configure-windows', '--apply')
    }
    else {
        $context.acss_executable = Join-Path $venvPath 'Scripts\agent-chat-session-sync.exe'
    }

    if ($doHooks) {
        if (-not (Test-Path -LiteralPath $context.acss_executable -PathType Leaf)) {
            throw 'Install the Python package before installing Hooks.'
        }
        $context.files += @(
            Backup-File -Source (Join-Path $codexHome 'hooks.json') -BackupDirectory $backupDir -Name 'codex-hooks.json'
            Backup-File -Source (Join-Path $claudeHome 'settings.json') -BackupDirectory $backupDir -Name 'claude-settings.json'
        )
        Write-Manifest -Path $manifestPath -Manifest $context
        Invoke-Checked $context.acss_executable @('install-hooks')
        Invoke-Checked $context.acss_executable @(
            'verify-install'
            '--source'
            $root
            '--expected-commit'
            $commit
        )
    }

    if ($doTask) {
        if (-not (Test-Path -LiteralPath $context.acss_executable -PathType Leaf)) {
            throw 'Install the Python package before installing the worker Scheduled Task.'
        }
        $context.files += @(
            Backup-File `
                -Source (Join-Path $StateRoot 'service\worker.ps1') `
                -BackupDirectory $backupDir `
                -Name 'worker.ps1'
        )
        $context.task_snapshot = Get-TaskSnapshot
        if ([bool]$context.task_snapshot.existed) {
            [IO.File]::WriteAllText(
                (Join-Path $backupDir 'task.xml'),
                [string]$context.task_snapshot.xml,
                [Text.UTF8Encoding]::new($false)
            )
            $context.task_snapshot.backup = 'task.xml'
        }
        $context.task_changed = $true
        Write-Manifest -Path $manifestPath -Manifest $context
        $env:ACSS_DATA_DIR = $StateRoot
        Invoke-Checked $context.acss_executable @('install-service')
    }

    if ($doBinary) {
        $targetParent = Split-Path -Parent $CcConnectTarget
        New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
        $context.cc_connect_snapshot = Get-CcConnectSnapshot -Binary $CcConnectTarget
        $context.binary_existed = [bool]$context.cc_connect_snapshot.existed
        $context.binary_intent = $true
        Write-Manifest -Path $manifestPath -Manifest $context
        if ($RestartCcConnect -and [bool]$context.cc_connect_snapshot.running) {
            & $CcConnectTarget daemon stop
            if ($LASTEXITCODE -ne 0) {
                throw "cc-connect daemon stop exited with code $LASTEXITCODE"
            }
            $context.cc_connect_stopped = $true
            Write-Manifest -Path $manifestPath -Manifest $context
        }
        if ($context.binary_existed) {
            Move-Item `
                -LiteralPath $CcConnectTarget `
                -Destination (Join-Path $backupDir 'cc-connect.exe')
            $context.binary_backup_moved = $true
            Write-Manifest -Path $manifestPath -Manifest $context
        }
        [IO.File]::Copy($stagedCcConnectBinary, $CcConnectTarget, $false)
        $installedCcConnectHash = (
            Get-FileHash -LiteralPath $CcConnectTarget -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($installedCcConnectHash -ne $candidateHash) {
            throw "installed cc-connect SHA-256 mismatch: expected $candidateHash; actual $installedCcConnectHash"
        }
        $context.binary_new_installed = $true
        Write-Manifest -Path $manifestPath -Manifest $context
        if ($RestartCcConnect -and [bool]$context.cc_connect_snapshot.running) {
            Invoke-Checked $CcConnectTarget @('daemon', 'start')
            $context.cc_connect_new_started = $true
            Write-Manifest -Path $manifestPath -Manifest $context
        }
    }

    if ($doTask) {
        Invoke-Checked $context.acss_executable @('doctor')
    }
    $context.status = 'applied'
    Write-Manifest -Path $manifestPath -Manifest $context
    Write-Host "installed commit $commit"
    Write-Host "backup $backupDir"
}
catch {
    $failure = $_
    $context.status = 'rolling_back'
    Write-Manifest -Path $manifestPath -Manifest $context
    $rollbackErrors = Rollback-Operation -Context $context
    $context.status = if ($rollbackErrors.Count) {
        'rollback_failed'
    }
    else {
        'rolled_back'
    }
    $context.rollback_errors = @($rollbackErrors)
    Write-Manifest -Path $manifestPath -Manifest $context
    if ($rollbackErrors.Count) {
        throw "$($failure.Exception.Message); rollback errors: $($rollbackErrors -join '; ')"
    }
    throw $failure
}
