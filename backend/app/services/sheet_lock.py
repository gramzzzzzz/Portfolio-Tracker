"""Exclusive lock around every mutation of the tracking sheet.

Two layers:
  * a re-entrant thread lock (the API server and the price-sync thread share one process);
  * an OS-level ``flock`` on ``<data>/.sheet.lock`` so that a second process (e.g. more than
    one uvicorn worker, or an admin script) can never write at the same time.

``flock`` is not re-entrant across file descriptors of one process, so the OS lock is taken
only by the outermost holder; nested ``with lock:`` blocks just bump a counter.
The lock is held only for the in-memory edit + atomic file replace (milliseconds); network
calls (price fetches) happen before it is taken.
"""
from __future__ import annotations

import fcntl
import os
import threading
import time
from pathlib import Path


class LockTimeout(Exception):
    pass


class SheetLock:
    def __init__(self, path: Path, timeout: float = 30.0):
        self.path = path
        self.timeout = timeout
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    def __enter__(self):
        if not self._rlock.acquire(timeout=self.timeout):
            raise LockTimeout("The sheet is busy, try again in a moment")
        try:
            if self._depth == 0:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
                deadline = time.monotonic() + self.timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            os.close(fd)
                            raise LockTimeout("The sheet is locked by another process")
                        time.sleep(0.05)
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        self._rlock.release()
        return False
