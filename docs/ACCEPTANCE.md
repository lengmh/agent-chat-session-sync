# Release acceptance

Passing unit tests is necessary but does not declare a release successful. The release gate is:

```text
clean source commit
  → exact candidate artifact bundle and SHA256SUMS verified
  → stamped package installed
  → Hook imports that exact package
  → Local Endpoint security and worker service identity pass
  → real Codex and Claude Code sessions start
  → Hook receipts are durable
  → a dedicated Feishu group is created
  → user prompt and assistant response are readable from Feishu
  → a Feishu reply resumes the same rollout
  → test group and workspace are removed (or intentionally retained for diagnosis)
```

## Commands

POSIX development gate:

```bash
PYTHONWARNINGS=error::ResourceWarning uv run --locked python -m unittest discover -s tests -v
uv run --locked python -m compileall -q src tests
git diff --check
./scripts/install.sh
agent-chat-session-sync acceptance-live --agent codex --timeout 300
agent-chat-session-sync acceptance-live --agent claudecode --timeout 300
```

Windows 11 x64 Alpha candidate gate:

```powershell
# Verify all three payloads against SHA256SUMS before running an unsigned Alpha EXE.
$artifactDir = (Resolve-Path .\dist).Path
$checksumFile = Join-Path $artifactDir 'SHA256SUMS'
Get-Content $checksumFile | ForEach-Object {
    $expected, $name = $_ -split '  ', 2
    $actual = (Get-FileHash -LiteralPath (Join-Path $artifactDir $name) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "SHA-256 mismatch: $name" }
}

$env:ACSS_EXPECTED_COMMIT = (git rev-parse HEAD)
uv run --locked python .\scripts\verify-release-artifacts.py $artifactDir

$wheel = @(Get-ChildItem -LiteralPath $artifactDir -Filter '*.whl')
if ($wheel.Count -ne 1) { throw "Expected one wheel, found $($wheel.Count)" }
$ccConnect = Join-Path $artifactDir 'cc-connect-windows-x64.exe'
$ccConnectTarget = 'C:\path\to\active\cc-connect.exe'
function Get-ExpectedSha256([string]$Name) {
    $entry = @(Get-Content $checksumFile | Where-Object { $_ -match "  $([regex]::Escape($Name))$" })
    if ($entry.Count -ne 1) { throw "Expected one checksum entry for $Name" }
    return ($entry[0] -split '  ', 2)[0]
}
$wheelSha256 = Get-ExpectedSha256 $wheel[0].Name
$ccConnectSha256 = Get-ExpectedSha256 (Split-Path -Leaf $ccConnect)

$installArgs = @(
    '-PythonPackage', $wheel[0].FullName,
    '-ExpectedPythonSha256', $wheelSha256,
    '-CcConnectBinary', $ccConnect,
    '-CcConnectTarget', $ccConnectTarget,
    '-ExpectedCcConnectSha256', $ccConnectSha256
)
pwsh -NoProfile -File .\scripts\install-windows.ps1 -WhatIf @installArgs
pwsh -NoProfile -File .\scripts\install-windows.ps1 @installArgs

$acss = Join-Path $env:LOCALAPPDATA 'agent-chat-session-sync\venv\Scripts\agent-chat-session-sync.exe'
& $acss doctor
& $acss acceptance-live --agent codex --timeout 300
& $acss acceptance-live --agent claudecode --timeout 300
```

The Windows gate installs the candidate wheel and validates the candidate sdist
and `cc-connect-windows-x64.exe` produced for the same clean commit. `doctor` must
validate package/build/Hook provenance, the current-user Task Scheduler identity,
the effective Named Pipe DACL, cc-connect `local_endpoint_v2` capabilities and
`instance_id`. The Alpha executable is not Authenticode-signed.
The PowerShell hash loop verifies files listed in the checksum manifest; the
repository verifier additionally rejects missing, extra, duplicate, unstamped,
or commit-mismatched artifacts.

Each live command prints a unique reply token. Send it as a normal user message in that Agent's test group. The harness verifies that
the token appears in the exact rollout created at the start of the run. By default it then disbands the group, invalidates
the test binding and removes the temporary workspace.

Automatic cleanup requires the Feishu application to hold `im:chat:delete` or `im:chat`. If that scope is missing, the
bidirectional message evidence remains valid but the release gate is incomplete: retain the binding/workspace for diagnosis,
grant the scope or dissolve the group manually, and finish local cleanup before declaring the release accepted.

Use `--keep-resources` while investigating a failure. `--skip-reply` verifies only Agent → Hook → worker → Feishu and
is explicitly diagnostic; it cannot satisfy the bidirectional release gate.

## Required fault coverage

Automated tests must cover at least:

| Condition | Required result |
|---|---|
| temporary Hook session ID | eventually resolves to the stable rollout |
| missing transcript | receipt remains queued |
| delayed rollout creation | later worker attempt resolves it |
| compaction | transcript identity continues to win |
| two sessions in one cwd | waits for confirmation instead of guessing |
| worker restart | pending inbox/outbox resumes |
| cc-connect Local Endpoint unavailable | retry without loss |
| cc-connect `instance_id` changes | durable bindings replay; messages are not replayed |
| unauthorized or broad Windows Pipe access | Named Pipe DACL gate fails |
| foreign same-name Scheduled Task | install/uninstall refuses ownership |
| Task restart or user relogin | one worker resumes queued work |
| Feishu timeout | retry with the same idempotency key |
| duplicate Hook receipt | one durable receipt/outbox delivery |
| source/package/Hook mismatch | installation verification fails |
| missing/extra artifact or bad SHA-256 | candidate release verification fails |

For an acceptance failure, collect `events`, `status`, `worker.log`, and `sync.log`. Do not report deployment completion
until candidate artifact verification, provenance, Local Endpoint/service checks,
cleanup, and the full live bidirectional flow all pass. A second interactive
Windows user denial test is optional Alpha hardening rather than a mandatory gate;
the effective DACL itself is mandatory.
