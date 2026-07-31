from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_chat_session_sync.locking import LockUnavailableError, exclusive_file_lock


class LockingTests(unittest.TestCase):
    def test_nonblocking_worker_lock_rejects_second_process_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "private" / "worker.lock"
            with exclusive_file_lock(path, blocking=False):
                with self.assertRaises(LockUnavailableError):
                    with exclusive_file_lock(path, blocking=False):
                        self.fail("second lock unexpectedly succeeded")
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
