# Contributing

This repository contains two coupled deliverables: the Python orchestration daemon
and reproducible patches against a pinned cc-connect revision. A change is complete
only when both affected layers are verified.

## Development setup

Use Python 3.11 or newer:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
```

Build and test the pinned cc-connect source with:

```bash
./scripts/build-cc-connect.sh
```

On Windows 11 x64 with PowerShell 7 and Go 1.25.x:

```powershell
./scripts/build-cc-connect-windows.ps1
```

The script checks and applies every patch, runs the selected Go packages, and builds
`dist/cc-connect` on Unix or `dist/cc-connect-windows-x64.exe` on Windows. The Windows
script keeps its checkout, logs, and staging binary under the selected temporary root.
Do not edit generated build or `dist/` files as the submitted source of a fix; update
the corresponding patch under `patches/`.

## Pull requests

- Keep unrelated changes separate.
- Add a regression test for every correctness or reliability fix.
- Do not commit credentials, tokens, chat IDs, rollout transcripts, logs, databases,
  generated binaries, or personal absolute paths.
- Preserve the fixed upstream cc-connect revision unless the pull request explicitly
  performs and documents an upstream compatibility upgrade.
- Update README and `docs/` whenever behavior, permissions, protocol, or installation
  support changes.
- Run Python tests, the patched Go test/build gate, and release artifact verification.

## Release artifacts

From a clean Git worktree with the `build` package installed:

```bash
./scripts/build-release.sh
```

The release is invalid unless the wheel and sdist pass archive verification and are
published with `SHA256SUMS`. A real Codex and Claude Code bidirectional Feishu
acceptance run is also required; unit tests cannot replace that gate.

## Live testing

Live tests create a temporary Agent workspace and Feishu group. Use a dedicated test
application where possible and never run them against another person's credentials.
Follow `docs/ACCEPTANCE.md`; clean up groups and workspaces after verification.
