# Changelog

All notable user-facing changes are documented here. This project follows
[Semantic Versioning](https://semver.org/) while remaining in experimental `0.x` development.

## [0.6.0-alpha.1] - Unreleased

### Added

- Windows 11 x64 Alpha runtime support across the CLI, Hooks, SQLite, worker,
  doctor, configuration checks, and current-user Task Scheduler lifecycle.
- Restricted byte-mode Named Pipe transport for the patched cc-connect Local
  Endpoint, including fail-closed DACL checks, transport capabilities, opaque
  process `instance_id`, and durable binding replay after restart.
- Transactional PowerShell 7 installer with `-WhatIf`, private per-operation
  backups, independent component confirmations, SHA-256 verification, foreign
  Task refusal, and manifest-based rollback.
- Ubuntu/Windows Python 3.11–3.14 CI, Go 1.25.x Windows patch/build integration,
  and a manual run-unique real Task Scheduler lifecycle check.
- Exact release bundle validation for wheel, sdist,
  `cc-connect-windows-x64.exe`, and `SHA256SUMS`.

### Known limitations

- Windows 10, ARM64, Windows SCM Service, cross-user shared deployment, and a
  shared Windows Codex App Server daemon are not supported in this Alpha.
- Windows Codex sessions use a per-session `stdio` App Server lifecycle.
- `cc-connect-windows-x64.exe` is not Authenticode-signed; verify
  `SHA256SUMS` before installation.
- Rebuildability means pinned source/toolchain/lock/patch inputs and verified
  contents, not byte-for-byte identical output across machines.

## [0.5.0-alpha.1] - 2026-07-31

First public experimental release.

### Added

- Durable Hook inbox, SQLite outbox, stable event IDs, retries, restart recovery,
  emergency fsync spool, and manual ambiguity resolution.
- Codex Desktop/CLI and Claude Code local-session adapters.
- One dedicated Feishu group per local Agent session, with Agent-specific titles,
  durable binding replay, stale-group recovery, and loop prevention.
- Pinned cc-connect v1.4.1 patch set for existing-session attachment, shared-Bot
  binding routing, Codex native permission profiles, App Server lifecycle support,
  and external rollout refresh.
- Pre-send rollout refresh for messages queued behind an active cc-connect turn.
- macOS LaunchAgent installation, single-worker lock, private local file modes,
  provenance verification, doctor checks, and live bidirectional acceptance tools.
- Reproducible wheel/sdist checks, release SHA-256 checksums, artifact clean-install
  tests, Go patch/build CI, security policy, and contribution guide.

### Known limitations

- Automatic worker service installation currently supports macOS only.
- The complete product requires the pinned patched cc-connect build; installing the
  Python wheel alone is insufficient.
- Automatic acceptance-group cleanup requires Feishu `im:chat:delete` or `im:chat`.
- The cc-connect patch set is maintained out-of-tree until the required generic
  attach/lifecycle interfaces are available upstream.

[0.5.0-alpha.1]: https://github.com/Sanshix/agent-chat-session-sync/releases/tag/v0.5.0-alpha.1
[0.6.0-alpha.1]: https://github.com/Sanshix/agent-chat-session-sync/compare/v0.5.0-alpha.1...v0.6.0-alpha.1
