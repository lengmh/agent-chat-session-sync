# Release acceptance

Passing unit tests is necessary but does not declare a release successful. The release gate is:

```text
clean source commit
  → stamped package installed
  → Hook imports that exact package
  → real Codex session starts
  → Hook receipts are durable
  → a dedicated Feishu group is created
  → user prompt and assistant response are readable from Feishu
  → a Feishu reply resumes the same rollout
  → test group and workspace are removed (or intentionally retained for diagnosis)
```

## Commands

```bash
PYTHONWARNINGS=error::ResourceWarning PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
git diff --check
./scripts/install.sh
agent-chat-session-sync acceptance-live --timeout 300
```

The live command prints a unique reply token. Send it as a normal user message in the test group. The harness verifies that
the token appears in the exact rollout created at the start of the run. By default it then disbands the group, invalidates
the test binding and removes the temporary workspace.

Use `--keep-resources` while investigating a failure. `--skip-reply` verifies only Codex → Hook → worker → Feishu and
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
| cc-connect Socket unavailable | retry without loss |
| Feishu timeout | retry with the same idempotency key |
| duplicate Hook receipt | one durable receipt/outbox delivery |
| source/package/Hook mismatch | installation verification fails |

For an acceptance failure, collect `events`, `status`, `worker.log`, and `sync.log`. Do not report deployment completion
until both provenance verification and the full live bidirectional flow pass.
