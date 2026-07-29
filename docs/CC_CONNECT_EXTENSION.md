# cc-connect session attach extension

## Why it exists

cc-connect normally learns an Agent session ID after a platform message starts the Agent. A local-first workflow has the opposite ordering: the Agent rollout already exists before the Feishu chat and cc-connect session key exist.

The extension exposes cc-connect's existing `SessionManager.SwitchToAgentSession` operation through its local Unix-socket API. It does not add another session store or change normal platform-first behavior.

The patch also carries two independent Codex extensions:

1. `permission_profile` selects a Codex native permission profile after fail-closed `permissionProfile/list` discovery for the session cwd. It is mutually exclusive with legacy sandbox fields.
2. `app_server_lifecycle` selects either a per-connection `stdio` App Server or a shared daemon connection through `codex app-server proxy`. Both use `thread/resume`, `turn/start`, and the live event stream.

## Contract

`POST /sessions/bind-agent` accepts `project`, `session_key`, `session_id`, an optional `session_name`, and an optional `work_dir`.
When `work_dir` is supplied, the project must use multi-workspace mode. The handler validates that the directory exists under the configured `base_dir`, persists the channel-to-workspace binding, and stores the Agent session ID in that workspace's own session manager.

Responses:

- `200`: binding is active; includes cc-connect's internal session ID.
- `400`: malformed request or missing required fields.
- `404`: configured project was not found.
- `405`: method is not POST.
- `409`: `work_dir` was supplied for a project not configured for multi-workspace.
- `422`: the Agent adapter rejected the native session ID for the selected project.

When `project` is empty, the handler only falls back automatically if exactly one engine is registered.

## Security assumptions

The API server is reachable only through cc-connect's mode-0600 Unix socket. Do not expose this route through a TCP reverse proxy. Validation must run before state mutation when the selected adapter implements `SessionIDValidator`.

Codex project options:

```toml
[projects.agent.options]
backend = "app_server"
app_server_lifecycle = "stdio" # or "daemon"
app_server_url = "stdio://"    # required for stdio lifecycle
app_server_socket = "/absolute/path/to/control.sock" # optional in daemon mode
permission_profile = "cc-connect-workspace"
```

The daemon socket is an OS deployment boundary and must be owner-only (`0600`) or restricted to an intentional service group (`0660`). A Codex permission profile is a separate sandbox boundary for commands run by the agent. Do not grant every Unix socket merely to make daemon connectivity work: the proxy process connects outside the agent command sandbox.

The public App Server protocol cannot attach a second client to the private stdio process launched by Codex Desktop. Shared in-memory lifecycle semantics require all participating clients to use the same public daemon. The extension deliberately does not reverse-engineer Desktop IPC.

## Upstreaming

The patch in `patches/` is scoped to session attach and the two Codex extensions, and includes focused tests. Before rebasing onto a newer cc-connect version:

1. Check whether upstream already exposes an equivalent attach operation.
2. Apply with `git apply --check`; never resolve a failed hunk mechanically.
3. Run the complete upstream Go test suite when the build environment permits it.
4. Verify both Codex and Claude Code validators against real per-project session stores.
5. Keep the route local-only and document compatibility in the release notes.
