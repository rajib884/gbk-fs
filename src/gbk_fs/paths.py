"""Path sandboxing and glob matching.

Security requirement (§5, §7, NFR4): every path the model supplies must resolve — after
normalizing ``..`` and following symlinks — to a location **inside** the configured root.
Anything else is rejected. Deny-globs (binaries, ``.git``, version ``.doc`` files) are
honoured here too.

Glob matching supports ``**`` (cross-directory), ``*`` / ``?`` (single segment) and brace
alternation ``{a,b}`` so the config's ``**/*.{c,h,cpp,inc}`` patterns work on Python 3.10+.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import InvalidArguments, OutsideRoot


def _normalize_input(path: str) -> str:
    """Accept Windows (``d:\\x``) or POSIX (``d:/x``) separators (FR7)."""
    return path.replace("\\", "/")


def resolve_in_root(root: Path, path: str) -> Path:
    """Resolve ``path`` (absolute or repo-relative) to a real path guaranteed inside ``root``.

    Raises OutsideRoot if the resolved real path escapes the sandbox.
    """
    if not path or not path.strip():
        raise InvalidArguments("path must be a non-empty string")

    raw = _normalize_input(path)
    p = Path(raw)
    if not p.is_absolute():
        p = root / raw

    # realpath resolves symlinks and `..`; strict=False so non-existent targets still resolve.
    real = Path(os.path.realpath(p))
    root_real = Path(os.path.realpath(root))

    if not _is_within(real, root_real):
        raise OutsideRoot(
            f"path {path!r} resolves to {real} which is outside the sandbox root {root_real}"
        )
    return real


def _is_within(child: Path, root: Path) -> bool:
    """True if ``child`` is ``root`` or nested under it (case-insensitive on Windows)."""
    try:
        child_parts = _norm_parts(child)
        root_parts = _norm_parts(root)
    except ValueError:
        return False
    if len(child_parts) < len(root_parts):
        return False
    return child_parts[: len(root_parts)] == root_parts


def _norm_parts(p: Path) -> list[str]:
    return [os.path.normcase(part) for part in p.parts]


def rel_to_root(root: Path, real: Path) -> str:
    """POSIX-style path of ``real`` relative to ``root`` (for display / glob matching)."""
    root_real = Path(os.path.realpath(root))
    try:
        rel = real.relative_to(root_real)
    except ValueError:
        # Fall back to os.path.relpath for case/realpath mismatches.
        rel = Path(os.path.relpath(real, root_real))
    return rel.as_posix()


# --------------------------------------------------------------------------------------
# Globbing
# --------------------------------------------------------------------------------------


def _expand_braces(pattern: str) -> list[str]:
    """Expand a single ``{a,b,c}`` group (one level, sufficient for the spec's patterns)."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    pre, post = pattern[: m.start()], pattern[m.end() :]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(pre + alt + post))
    return out


def _glob_to_regex(pattern: str) -> str:
    """Translate one (brace-free) glob to an anchored regex over POSIX-style paths."""
    pattern = _normalize_input(pattern)
    i, n = 0, len(pattern)
    out = ["(?s:"]
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                # `**` spans directories; consume an optional trailing slash.
                i += 2
                if pattern[i : i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "/":
            out.append("/")
        else:
            out.append(re.escape(c))
        i += 1
    out.append(")\\Z")
    return "".join(out)


def compile_globs(patterns: list[str]) -> list[re.Pattern[str]]:
    """Compile glob patterns (with brace expansion) into case-insensitive regexes."""
    regexes: list[re.Pattern[str]] = []
    for pat in patterns:
        for expanded in _expand_braces(pat):
            regexes.append(re.compile(_glob_to_regex(expanded), re.IGNORECASE))
    return regexes


def match_any(rel_path: str, regexes: list[re.Pattern[str]]) -> bool:
    rel = _normalize_input(rel_path)
    return any(rx.match(rel) for rx in regexes)
