from __future__ import annotations

import unittest

from agent_chat_session_sync.acceptance import LiveAcceptance
from agent_chat_session_sync.cli import build_parser


class AcceptanceTests(unittest.TestCase):
    def test_extracts_thread_id_from_current_codex_json(self) -> None:
        output = '\n'.join(
            [
                '{"type":"item.completed","item":{}}',
                '{"type":"thread.started","thread_id":"abc-123"}',
            ]
        )
        self.assertEqual(LiveAcceptance._thread_id(output), "abc-123")

    def test_extracts_legacy_thread_shape_and_ignores_invalid_lines(self) -> None:
        output = 'not-json\n{"type":"thread_started","threadId":"legacy-456"}'
        self.assertEqual(LiveAcceptance._thread_id(output), "legacy-456")

    def test_missing_thread_id_is_empty(self) -> None:
        self.assertEqual(LiveAcceptance._thread_id('{"type":"turn.completed"}'), "")

    def test_acceptance_cli_defaults_to_full_bidirectional_check(self) -> None:
        args = build_parser().parse_args(["acceptance-live"])
        self.assertEqual(args.timeout, 300)
        self.assertFalse(args.keep_resources)
        self.assertFalse(args.skip_reply)

    def test_acceptance_cli_exposes_diagnostic_skip(self) -> None:
        args = build_parser().parse_args(
            ["acceptance-live", "--timeout", "12", "--keep-resources", "--skip-reply"]
        )
        self.assertEqual(args.timeout, 12)
        self.assertTrue(args.keep_resources)
        self.assertTrue(args.skip_reply)


if __name__ == "__main__":
    unittest.main()
