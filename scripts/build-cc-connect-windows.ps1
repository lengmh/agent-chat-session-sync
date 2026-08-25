[CmdletBinding()]
param(
    [string]$SourceDir,
    [string]$Output,
    [string]$TempRoot = $(if ($env:ACSS_TEMP_DIR) { $env:ACSS_TEMP_DIR } else { [IO.Path]::GetTempPath() }),
    [string]$GoCommand = 'go',
    [string]$BuildCommit
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'PowerShell 7 or newer is required.'
}
if (-not $IsWindows) {
    throw 'This build entry point requires Windows.'
}
if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne [Runtime.InteropServices.Architecture]::X64) {
    throw 'This build entry point supports Windows x64 only.'
}

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$revision = '5d4c96dd12774574369e75b60084140101c9a59a'
$patches = @(
    'cc-connect-v1.4.1-bind-agent.patch'
    'cc-connect-v1.4.1-binding-routing.patch'
    'cc-connect-v1.4.1-rollout-refresh.patch'
    'cc-connect-v1.4.1-windows-local-endpoint.patch'
)
$operationId = [Guid]::NewGuid().ToString('N')
$TempRoot = [IO.Path]::GetFullPath($TempRoot)
if (-not $SourceDir) {
    $SourceDir = Join-Path $TempRoot "cc-connect-build-$operationId"
}
$SourceDir = [IO.Path]::GetFullPath($SourceDir)
$tempPrefix = $TempRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $SourceDir.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "SourceDir must be inside TempRoot: $TempRoot"
}
if (-not $Output) {
    $Output = Join-Path $root 'dist\cc-connect-windows-x64.exe'
}
$Output = [IO.Path]::GetFullPath($Output)
$logPath = Join-Path $TempRoot "cc-connect-build-$operationId.log"
$stagedBinary = Join-Path $TempRoot "cc-connect-windows-x64-$operationId.exe"

if (Test-Path -LiteralPath $SourceDir) {
    throw "SourceDir must not already exist: $SourceDir"
}

New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
$git = (Get-Command git -ErrorAction Stop).Source
$go = (Get-Command $GoCommand -ErrorAction Stop).Source
if (-not $BuildCommit) {
    $BuildCommit = (& $git -C $root rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'Cannot determine the agent-chat-session-sync build commit.'
    }
}
if ($BuildCommit -notmatch '^[0-9a-fA-F]{40}$') {
    throw "BuildCommit must be a full 40-character Git commit: $BuildCommit"
}
$BuildCommit = $BuildCommit.ToLowerInvariant()
$binaryProvenance = "acss:$BuildCommit;upstream:$revision"

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

$oldGoMaxProcs = $env:GOMAXPROCS
Start-Transcript -Path $logPath | Out-Null
try {
    $goVersion = & $go version
    if ($LASTEXITCODE -ne 0) {
        throw "$go exited with code $LASTEXITCODE"
    }
    if ($goVersion -notmatch '\bgo1\.25(?:\.|\s)') {
        throw "Go 1.25.x is required; found: $goVersion"
    }

    Invoke-Checked $git @('clone', 'https://github.com/chenhg5/cc-connect.git', $SourceDir)
    Invoke-Checked $git @('-C', $SourceDir, 'fetch', 'origin', $revision)
    Invoke-Checked $git @('-C', $SourceDir, 'checkout', '--detach', $revision)

    foreach ($patchName in $patches) {
        $patchPath = Join-Path $root "patches\$patchName"
        Invoke-Checked $git @('-C', $SourceDir, 'apply', '--check', $patchPath)
        Invoke-Checked $git @('-C', $SourceDir, 'apply', $patchPath)
    }

    $env:GOMAXPROCS = '1'
    Push-Location $SourceDir
    try {
        $focusedPattern = 'Test(NewAPIServerUsesSIDDerivedDefaultNamedPipe|NewAPIServerServesHTTPOverExplicitNamedPipe|NewAPIServerNamedPipeDACLIsCurrentUserOnly|HandleBindAgentSessionReportsExternalRefreshCapability|NewAPIServerRejectsNetworkEndpoint|HandleBindAgentSessionRejectsBeforeRoutingMutation|Load_ParsesInternalAPIEndpoint|Load_RejectsNetworkInternalAPIEndpoint)$'
        Invoke-Checked $go @(
            'test'
            './core'
            './config'
            '-run'
            $focusedPattern
            '-count=1'
        )
        Invoke-Checked $go @('test', '-tags', 'no_web goolm', './...', '-count=1')
        Invoke-Checked $go @(
            'build'
            '-trimpath'
            '-tags'
            'no_web goolm'
            '-ldflags'
            "-X main.version=v1.4.1-acss -X main.commit=$binaryProvenance -X main.buildTime=source:$BuildCommit"
            '-o'
            $stagedBinary
            './cmd/cc-connect'
        )
    }
    finally {
        Pop-Location
    }

    if (-not (Test-Path -LiteralPath $stagedBinary -PathType Leaf)) {
        throw "Build did not produce the staged binary: $stagedBinary"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Output) -Force | Out-Null
    [IO.File]::Copy($stagedBinary, $Output, $true)
    $hash = (Get-FileHash -LiteralPath $Output -Algorithm SHA256).Hash.ToLowerInvariant()

    Write-Host "built $Output"
    Write-Host "sha256 $hash"
    Write-Host "provenance $binaryProvenance"
    Write-Host "source $SourceDir"
    Write-Host "log $logPath"
}
finally {
    if ($null -eq $oldGoMaxProcs) {
        Remove-Item Env:GOMAXPROCS -ErrorAction SilentlyContinue
    }
    else {
        $env:GOMAXPROCS = $oldGoMaxProcs
    }
    Stop-Transcript | Out-Null
}
