"""Per-path locking (CR4).

Concurrent writes/edits to the *same* path must be serialized so the
read-raw -> splice -> re-encode flow is never interleaved (which would corrupt a file).
Writes to *different* paths may proceed concurrently (CR2). We use one re-entrant-free
lock per normalized path, created lazily under a registry lock.

The locks are process-local; the gbk-fs server is a single stdio process per session, so
this is sufficient. (Cross-process safety is out of scope — see CR8/NFR7: no shared cache.)
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from .errors import Busy


class PathLocks:
    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _key(self, path: Path) -> str:
        return os.path.normcase(os.path.realpath(path))

    def _get(self, path: Path) -> threading.Lock:
        key = self._key(path)
        with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def hold(self, path: Path, *, timeout: float | None = None):
        """Hold the lock for a single path.

        ``timeout=None`` queues (blocks) until acquired — satisfies CR4's "applied in
        submission order". A positive timeout raises Busy (CR4's "rejected with BUSY").
        """
        lock = self._get(path)
        acquired = lock.acquire(timeout=timeout if timeout is not None else -1)
        if not acquired:
            raise Busy(f"file is busy (locked by another operation): {path}")
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def hold_many(self, paths: list[Path], *, timeout: float | None = None):
        """Hold locks for several paths at once, acquired in a stable order (no deadlock).

        Used by transactional ``apply_edits`` so a whole batch is serialized against any
        single-file op touching the same files.
        """
        # De-dup and sort by key for a global acquisition order.
        unique: dict[str, Path] = {}
        for p in paths:
            unique.setdefault(self._key(p), p)
        ordered = [unique[k] for k in sorted(unique)]

        acquired: list[threading.Lock] = []
        try:
            for p in ordered:
                lock = self._get(p)
                ok = lock.acquire(timeout=timeout if timeout is not None else -1)
                if not ok:
                    raise Busy(f"file is busy (locked by another operation): {p}")
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
