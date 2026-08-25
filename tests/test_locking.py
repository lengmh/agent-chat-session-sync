from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
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
            if os.name != "nt":
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_nonblocking_worker_lock_is_exclusive_across_processes(self) -> None:
        holder_code = """
import sys
from pathlib import Path
from agent_chat_session_sync.locking import exclusive_file_lock

with exclusive_file_lock(Path(sys.argv[1]), blocking=False):
    print("READY", flush=True)
    sys.stdin.read(1)
"""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "private" / "worker.lock"
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "READY")
                with self.assertRaises(LockUnavailableError):
                    with exclusive_file_lock(path, blocking=False):
                        self.fail("second process owner unexpectedly acquired the lock")
            finally:
                if holder.stdin:
                    holder.stdin.write("x")
                    holder.stdin.flush()
                _stdout, stderr = holder.communicate(timeout=5)
                self.assertEqual(holder.returncode, 0, stderr)

            with exclusive_file_lock(path, blocking=False):
                pass


if __name__ == "__main__":
    unittest.main()
