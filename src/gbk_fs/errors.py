"""Typed errors for gbk-fs.

Every operational failure raises a :class:`GbkFsError` subclass carrying a stable
``code`` string. The transport layer (``server.py``) turns these into clean,
model-readable error messages; tests assert on ``code``.

Correctness/fidelity is the top priority (NFR1): a corrupting write is worse than a
failed one. These errors exist so failures are *loud and specific* rather than silent.
"""

from __future__ import annotations


class GbkFsError(Exception):
    """Base class for all gbk-fs operational errors.

    ``code`` is a short, stable, machine-readable token (e.g. ``CONFLICT``) that the
    model can branch on; ``message`` is human-readable detail.
    """

    code = "ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


class InvalidArguments(GbkFsError):
    code = "INVALID_ARGS"


class OutsideRoot(GbkFsError):
    """Path resolved outside the configured sandbox root (NFR4, §5, §7)."""

    code = "OUTSIDE_ROOT"


class Denied(GbkFsError):
    """Path matched a deny-glob (binary/known-non-source) and was refused (§5)."""

    code = "DENIED"


class NotFound(GbkFsError):
    code = "NOT_FOUND"


class IsBinary(GbkFsError):
    """Target looks like a binary file; we refuse to decode/return it (§4.1)."""

    code = "BINARY"


class TooLarge(GbkFsError):
    code = "TOO_LARGE"


class DecodeError(GbkFsError):
    """On-disk bytes are not valid under the resolved decode codec (NFR6)."""

    code = "DECODE_ERROR"


class LossyEncode(GbkFsError):
    """Target codec genuinely cannot represent a character (§3 lossy guard, FR6).

    Carries the offending character and its offset so the failure is actionable;
    we never substitute ``?`` or drop characters silently.
    """

    code = "LOSSY_ENCODE"

    def __init__(self, message: str, *, char: str, char_index: int, byte_offset: int):
        super().__init__(message)
        self.char = char
        self.char_index = char_index
        self.byte_offset = byte_offset


class MatchNotFound(GbkFsError):
    """``old_string`` not present in the file (edit_file/apply_edits)."""

    code = "MATCH_NOT_FOUND"


class AmbiguousMatch(GbkFsError):
    """``old_string`` matched more than once and ``replace_all`` was not set."""

    code = "AMBIGUOUS"

    def __init__(self, message: str, *, count: int):
        super().__init__(message)
        self.count = count


class Conflict(GbkFsError):
    """On-disk file changed since the caller's snapshot (CR5 optimistic concurrency)."""

    code = "CONFLICT"


class Busy(GbkFsError):
    """A per-path lock could not be acquired in time (CR4)."""

    code = "BUSY"


class UnreadOverwrite(GbkFsError):
    """Refusing to overwrite an existing file not read in this session (FR8)."""

    code = "UNREAD_OVERWRITE"


class ReplacementChar(GbkFsError):
    """Incoming write/edit content contains U+FFFD, the Unicode replacement character.

    This is the corruption signature this server guards against: U+FFFD almost always means
    the content was produced by a lossy decode upstream (e.g. a GBK file read as UTF-8), so
    persisting it would write corruption to disk. Note the default ``gb18030`` codec *can*
    encode U+FFFD (to bytes ``84 31 a4 37``), so the generic LossyEncode guard never catches
    it — this is a dedicated, codec-independent check (write guard, #1). We refuse by default;
    pass ``allow_replacement_chars=true`` if the character is genuinely intended.
    """

    code = "REPLACEMENT_CHAR"

    def __init__(self, message: str, *, count: int, first_index: int):
        super().__init__(message)
        self.count = count
        self.first_index = first_index


class GitError(GbkFsError):
    """A git invocation failed: not a repository, unknown ref, path absent in the ref, or
    the ``git`` executable is unavailable (read_git, #2)."""

    code = "GIT_ERROR"
