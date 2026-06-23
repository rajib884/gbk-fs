"""Encoding-aware content search (§4.4).

Decodes each file with its detected encoding *before* matching and returns UTF-8 results
with correct ``path:line`` locations — replacing the ``grep | iconv`` pattern and never
emitting mojibake (FR3). Files are read/decoded/matched in a thread pool so a multi-file
search completes in roughly ``max(single)`` wall-clock, not the sum (CR1).
"""

from __future__ import annotations

import bisect
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import encoding as enc
from .errors import DecodeError, GbkFsError, InvalidArguments
from .fileio import looks_binary, read_bytes
from .paths import compile_globs, match_any, rel_to_root

# rg-style file-type shorthands -> glob lists.
TYPE_GLOBS: dict[str, list[str]] = {
    "c": ["*.c", "*.h"],
    "cpp": ["*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh", "*.h"],
    "py": ["*.py"],
    "js": ["*.js", "*.jsx", "*.mjs"],
    "ts": ["*.ts", "*.tsx"],
    "md": ["*.md", "*.markdown"],
    "json": ["*.json", "*.jsonc"],
    "txt": ["*.txt"],
    "inc": ["*.inc"],
    "yaml": ["*.yml", "*.yaml"],
    "toml": ["*.toml"],
}


def _gather_files(core, base: Path, glob: str | None, ftype: str | None) -> list[Path]:
    cfg = core.config
    glob_re = compile_globs([glob]) if glob else None
    type_re = compile_globs(TYPE_GLOBS[ftype]) if ftype and ftype in TYPE_GLOBS else None
    if ftype and ftype not in TYPE_GLOBS:
        raise InvalidArguments(f"unknown type {ftype!r}; known: {', '.join(sorted(TYPE_GLOBS))}")

    if base.is_file():
        return [base]

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames
            if not cfg.is_denied(rel_to_root(cfg.root, Path(dirpath) / d) + "/x")
        ]
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = rel_to_root(cfg.root, full)
            if cfg.is_denied(rel):
                continue
            rel_base = os.path.relpath(full, base).replace("\\", "/")
            if glob_re and not match_any(rel_base, glob_re):
                continue
            if type_re and not match_any(rel_base, type_re):
                continue
            files.append(full)
    return files


def _matched_line_numbers(view: str, lines: list[str], regex: re.Pattern[str], multiline: bool):
    """Return ``(set_of_1based_line_numbers, total_matches)``."""
    if multiline:
        offsets = [0]
        acc = 0
        for ln in lines:
            acc += len(ln) + 1
            offsets.append(acc)
        matched: set[int] = set()
        total = 0
        for m in regex.finditer(view):
            total += 1
            s, e = m.start(), m.end()
            ls = bisect.bisect_right(offsets, s) - 1
            le = bisect.bisect_right(offsets, max(e - 1, s)) - 1
            for ln in range(ls, le + 1):
                if 0 <= ln < len(lines):
                    matched.add(ln + 1)
        return matched, total

    matched = set()
    total = 0
    for i, line in enumerate(lines, 1):
        if regex.search(line):
            matched.add(i)
            total += 1
    return matched, total


def _with_context(lines: list[str], matched: set[int], before: int, after: int):
    include: set[int] = set()
    for ln in matched:
        for k in range(ln - before, ln + after + 1):
            if 1 <= k <= len(lines):
                include.add(k)
    out: list[dict[str, Any]] = []
    prev = None
    for ln in sorted(include):
        if prev is not None and ln > prev + 1:
            out.append({"separator": True})
        out.append({"line": ln, "text": lines[ln - 1], "is_match": ln in matched})
        prev = ln
    return out


def search_content(
    core,
    *,
    pattern: str | None = None,
    patterns: list[str] | None = None,
    path: str | None = None,
    glob: str | None = None,
    type: str | None = None,
    output_mode: str = "content",
    ignore_case: bool = False,
    before: int = 0,
    after: int = 0,
    context: int = 0,
    head_limit: int = 200,
    multiline: bool = False,
) -> dict[str, Any]:
    cfg = core.config

    pats: list[str] = []
    if patterns:
        pats.extend(patterns)
    if pattern:
        pats.append(pattern)
    if not pats:
        raise InvalidArguments("provide `pattern` or `patterns`")
    if output_mode not in ("content", "files", "count"):
        raise InvalidArguments("output_mode must be 'content', 'files' or 'count'")

    combined = "|".join(f"(?:{p})" for p in pats)
    flags = 0
    if ignore_case:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL | re.MULTILINE
    try:
        regex = re.compile(combined, flags)
    except re.error as exc:
        raise InvalidArguments(f"invalid regex: {exc}")

    if context:
        before = after = context

    base = core._resolve(path)[0] if path else Path(os.path.realpath(cfg.root))
    if not base.exists():
        raise InvalidArguments(f"search path not found: {path}")
    files = _gather_files(core, base, glob, type)

    def work(full: Path):
        try:
            raw, _ = read_bytes(full, limit=cfg.max_read_bytes)
        except OSError:
            return None
        if looks_binary(raw[:8192]):
            return None
        rel = rel_to_root(cfg.root, full)
        try:
            det = core._detect(raw, rel, None)
            try:
                text = enc.decode_body(raw[len(det.bom):], det.decode_codec,
                                       errors=cfg.on_decode_error)
            except DecodeError:
                # never abort a search over one bad file: fall back to lossy decode
                text = raw[len(det.bom):].decode(det.decode_codec, "replace")
        except GbkFsError:
            return None
        view = enc.normalize_to_lf(text)
        lines = core._split_lines(view)
        matched, total = _matched_line_numbers(view, lines, regex, multiline)
        if not matched:
            return None
        return {"path": rel, "encoding": det.logical, "lines": lines,
                "matched": matched, "match_count": len(matched), "raw_matches": total}

    per_file: list[dict[str, Any]] = []
    if files:
        with ThreadPoolExecutor(max_workers=min(32, len(files))) as ex:
            for r in ex.map(work, files):
                if r is not None:
                    per_file.append(r)
    per_file.sort(key=lambda r: r["path"].lower())

    total_matches = sum(r["match_count"] for r in per_file)

    if output_mode == "files":
        paths = [r["path"] for r in per_file]
        return {
            "mode": "files",
            "files_searched": len(files),
            "files_matched": len(paths),
            "truncated": len(paths) > head_limit,
            "results": paths[:head_limit],
        }

    if output_mode == "count":
        counts = [{"path": r["path"], "count": r["match_count"]} for r in per_file]
        return {
            "mode": "count",
            "files_searched": len(files),
            "total_matches": total_matches,
            "truncated": len(counts) > head_limit,
            "results": counts[:head_limit],
        }

    # content mode: emit matching lines (+context), capped at head_limit match lines
    results: list[dict[str, Any]] = []
    emitted = 0
    truncated = False
    for r in per_file:
        if emitted >= head_limit:
            truncated = True
            break
        remaining = head_limit - emitted
        matched = r["matched"]
        if len(matched) > remaining:
            matched = set(sorted(matched)[:remaining])
            truncated = True
        block = _with_context(r["lines"], matched, before, after)
        emitted += sum(1 for e in block if e.get("is_match"))
        results.append({"path": r["path"], "encoding": r["encoding"], "lines": block})

    return {
        "mode": "content",
        "files_searched": len(files),
        "files_matched": len(per_file),
        "total_matches": total_matches,
        "truncated": truncated,
        "results": results,
    }
