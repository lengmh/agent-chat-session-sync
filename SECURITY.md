# Security policy

## Supported versions

The latest tagged `0.x` release receives security fixes on a best-effort basis. Older
development snapshots are unsupported. This project is experimental and should run
as an unprivileged desktop user; do not run either daemon as root.

## Reporting a vulnerability

Use the repository Security tab and select **Report a vulnerability** so credentials,
local paths, Socket details, and exploit steps remain private. Repository maintainers
must enable GitHub private vulnerability reporting before the first public release.
If private reporting is unavailable, open a public issue containing no sensitive
details and ask the maintainers to establish a private channel.

Include the affected release/commit, operating system, cc-connect revision, impact,
and the smallest safe reproduction. Never include Feishu secrets, access tokens,
chat IDs, user IDs, rollout contents, or local configuration files.

We aim to acknowledge a report within seven days. A fix and coordinated disclosure
timeline depend on severity and whether an upstream cc-connect or Agent change is
required.

## Security boundaries

- The worker, cc-connect, Codex, and Claude Code must run under the same intended
  unprivileged user unless a deliberately configured service group is used.
- cc-connect and Codex App Server Unix Sockets must remain local and owner-only
  (`0600`), or group-only (`0660`) with a dedicated service group.
- Feishu credentials stay in the local cc-connect configuration and must never be
  committed, bundled into artifacts, or pasted into issues.
- Remote Agent turns inherit the configured Codex permission profile. Do not replace
  a scoped profile with unrestricted filesystem, network, or Unix Socket access.

If a credential may have been disclosed, revoke or rotate it before reporting.
