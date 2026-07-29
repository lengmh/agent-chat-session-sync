# Reliability model

## Processing contract

The Codex and Claude Code Hooks are inbox writers, not synchronization workers. They serialize the unmodified Hook payload into
`events.sqlite3` and returns immediately. If SQLite cannot accept the receipt, it appends the same payload to a mode-0600
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

Dynamic Feishu routes live in cc-connect memory. The durable copy remains in SQLite; the worker replays all bindings on startup
and whenever the cc-connect Socket inode or modification time changes.

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

The build stamps the wheel without modifying the source tree. `scripts/install.sh` refuses a dirty repository and invokes
`verify-install`, which compares the repository HEAD, installed package stamp, and the package imported by the exact Hook
command in `~/.codex/hooks.json`. A clean identity check proves which code runs; it does not replace live acceptance.

## Operational storage

- `events.sqlite3`: inbox, outbox, bindings and schema metadata; WAL, `synchronous=FULL`, mode 0600.
- `emergency-inbox.jsonl`: fsynced fallback receipts, imported by the worker.
- `sync.log`: Hook receipts and import identity.
- `worker.log`: resolution, retry, binding and delivery outcomes.
- `state.json`: legacy import source only; new bindings live in SQLite.
