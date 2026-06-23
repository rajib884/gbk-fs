"""Low-level file I/O: atomic writes, hashing, binary sniffing (CR3, §4.1)."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def read_bytes(path: Path, *, limit: int | None = None) -> tuple[bytes, bool]:
    """Read up to ``limit`` bytes. Returns ``(data, truncated)``.

    ``truncated`` is True if the file is larger than ``limit`` (the guard rail for huge
    ``.a``/``.doc`` files, §5 ``maxReadBytes``). ``limit=None`` reads the whole file.
    """
    if limit is None:
        return path.read_bytes(), False
    size = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(limit)
    return data, size > limit


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def looks_binary(sample: bytes) -> bool:
    """Heuristic: a NUL byte in the sample => binary (§4.1 'binary files rejected')."""
    return b"\x00" in sample


def atomic_write(path: Path, data: bytes) -> int:
    """Write ``data`` to ``path`` atomically (CR3).

    Strategy: create a uniquely-named temp file in the *same directory* (so ``os.replace``
    is a same-filesystem atomic rename), flush + fsync, then replace the target. A
    concurrent reader sees either the old or the new file, never a torn one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic on POSIX; MoveFileEx(REPLACE_EXISTING) on Windows
        return len(data)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
