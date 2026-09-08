"""Process locks for short local artifact transactions, including standalone CLI writers."""

from __future__ import annotations

import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


class WorkspaceConflict(RuntimeError):
    """The caller must reload current state before attempting this transition again."""


@contextmanager
def directory_write_lock(directory: Path) -> Generator[None]:
    """Lock a stable inode; never unlink it while another process may hold or await it."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".eve-courier-write.lock").open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise WorkspaceConflict(
                    "another process is saving this workspace; reload and retry"
                ) from error
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise WorkspaceConflict(
                    "another process is saving this workspace; reload and retry"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
