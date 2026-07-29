from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[TextIO]:
    """Take a process-wide file lock on Unix or Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if __import__("os").name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        if __import__("os").name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
