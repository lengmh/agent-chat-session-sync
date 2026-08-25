# Reliability model

## Processing contract

The Codex and Claude Code Hooks are inbox writers, not synchronization workers. They serialize the unmodified Hook payload into
`events.sqlite3` and return immediately. If SQLite cannot accept the receipt, the Hook appends the same payload to a protected
`emergency-inbox.jsonl`, calls `fsync`, and still avoids blocking the Codex turn. The worker atomically renames and imports
that spool before claiming normal work.

An inbox record progresses through:

```text
received
  → resolving_session
  → waiting_confirmation | resolved
  → binding_chat
  → sending
  → delivered
```

`resolving_session` and `waiting_confirmation` are durable waiting states, not discarded errors. Transient failures use
bounded exponential backoff. `dead_letter` exists for explicit terminal policy, but the current worker does not turn a
missing or ambiguous rollout into a dead letter automatically.

## Session identity

Codex resolution uses evidence in this order:

1. A valid `transcript_path` whose filename contains a rollout UUID.
2. A Hook session ID that directly names an existing rollout.
3. An alias in `session_index.jsonl` or Codex global state.
4. Newly persisted rollout files observed by later worker attempts.
5. Correlation using metadata session ID, normalized first prompt, cwd and timestamps.

The last step requires a minimum score and a clear lead over the next candidate. Cwd is only one correlation signal.
Two plausible sessions in the same directory stay in `waiting_confirmation` until stronger evidence appears or an operator
uses `agent-chat-session-sync resolve`.

Claude Code uses the same waiting-state contract, but its stable identity is the native Claude session UUID. Resolution prefers
the Hook `transcript_path`, then the native session file, `history.jsonl`, delayed transcript discovery, and finally multi-factor
correlation. Codex and Claude receipt, outbox, and binding identities are namespaced so equal UUID strings cannot collide.

Dynamic Feishu routes live in cc-connect memory. The durable copy remains in SQLite. The worker discovers cc-connect through
the permission-protected Local Endpoint and replays all bindings on the first successful connection and whenever the reported
process-level `instance_id` changes. Binding replay never replays messages.

On Unix, Local Endpoint authorization is enforced with socket owner/mode checks.
On Windows, the byte-mode Named Pipe DACL permits only the current user SID,
SYSTEM, and Administrators. TCP, WebSocket, remote access, and transport fallback
are rejected rather than treated as degraded operation.

An unavailable, insecure, or incompatible Local Endpoint leaves the inbox record in `binding_chat` with backoff. The worker
does not create a new Feishu chat or deliver an existing binding's queued event until endpoint security, capabilities, and
binding replay are ready.

## Outbox and idempotency

After resolving a rollout, the worker derives a stable key from:

```text
rollout_id + hook_event + turn_id + content_hash
```

It creates the outbox record before network delivery. A delivered record stores the Feishu message ID. Restarting during
`sending` retries the same outbox entry; Feishu requests also receive deterministic per-chunk UUIDs derived from that key.
This provides local deduplication and platform-side idempotency. Operators can inspect `events` and schedule a failed
historical entry immediately with `retry`.

## Deployment identity

Every Hook receipt logs all of:

```text
service_version git_commit package_path python_path config_path
```

The build stamps the wheel without modifying the source tree. Install and doctor
checks compare the candidate commit, installed package stamp, and the package
imported by the exact Hook command. On Windows they also verify the current-user
Task Scheduler principal/action, wrapper content, and package executable provenance.
A clean identity check proves which code runs; it does not replace candidate
artifact verification or live acceptance.

## Operational storage

- Windows data root: `%LOCALAPPDATA%\agent-chat-session-sync`, protected DACL inherited by SQLite/WAL, spool, logs, locks, backups, and service wrapper.
- POSIX data root: `~/.local/share/agent-chat-session-sync`, directory mode 0700 and private files mode 0600.
- `events.sqlite3`: inbox, outbox, bindings and schema metadata; WAL and `synchronous=FULL`.
- `emergency-inbox.jsonl`: fsynced fallback receipts, imported by the worker.
- `sync.log`: Hook receipts and import identity.
- `worker.log`: resolution, retry, binding and delivery outcomes.
- `state.json`: legacy import source only; new bindings live in SQLite.

The Windows worker runs as the current interactive user through
`\AgentChatSessionSync\Worker` with Limited privilege and `IgnoreNew`.
The wrapper stops on exit codes 0 and 4; other nonzero exits wait 10 seconds before
restarting. Install and uninstall refuse a same-name Task whose complete identity
does not match the managed definition.
