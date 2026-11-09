"""Cross-process guard for a recording lock's read/decide/replace transaction.

The JSON ownership file can be reclaimed; its sibling ``.guard`` cannot be
removed or replaced. All contenders therefore lock the same OS file object.
OS locks are released when the owning process exits, including forced exits.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path


class RecordingLockBusy(RuntimeError):
    """The ownership transaction could not be guarded; make no state changes."""

    def __init__(self, path: Path, reason: str):
        self.path = Path(path)
        self.reason = str(reason)
        super().__init__(f'recording_lock_busy: {self.path}: {self.reason}')


@contextmanager
def recording_lock_guard(path: Path):
    """Exclusively guard all ownership-file reads/deletes/writes, without waiting.

    ``path`` is the recording JSON lock path, not the guard path. Its parent
    must already exist. The guard file is retained even after errors or crashes;
    deleting it would let contenders lock different inodes. Failure to open or
    acquire the guard is fail-closed and raises :class:`RecordingLockBusy`.
    """
    path = Path(path).resolve()
    guard_path = path.with_name(path.name + '.guard')
    try:
        handle = guard_path.open('a+b')
    except OSError as exc:
        raise RecordingLockBusy(guard_path, str(exc)) from exc
    with handle:
        try:
            # Windows byte-range locks need a byte to lock. Concurrent first
            # opens may append two bytes; all contenders still lock byte zero.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RecordingLockBusy(guard_path, str(exc)) from exc
        try:
            yield
        finally:
            # Closing the handle below also releases the lock if explicit
            # unlock fails; preserve any exception from the guarded operation.
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
