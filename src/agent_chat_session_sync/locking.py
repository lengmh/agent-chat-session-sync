from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from .security import ensure_private_directory, harden_private_file


class LockUnavailableError(RuntimeError):
    """Raised when a non-blocking process lock is already held."""


@contextmanager
def exclusive_file_lock(path: Path, *, blocking: bool = True) -> Iterator[TextIO]:
    """Take a process-wide file lock on Unix or Windows."""
    ensure_private_directory(path.parent)
    handle = path.open("a+", encoding="utf-8")
    harden_private_file(path)
    acquired = False
    try:
        if __import__("os").name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
            except OSError as exc:
                raise LockUnavailableError(f"lock is already held: {path}") from exc
            acquired = True
        else:
            import fcntl

            operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), operation)
            except BlockingIOError as exc:
                raise LockUnavailableError(f"lock is already held: {path}") from exc
            acquired = True
        yield handle
    finally:
        if acquired and __import__("os").name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif acquired:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
