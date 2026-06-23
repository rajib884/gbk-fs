"""The transport-agnostic core: the ``GbkFs`` class implementing every tool.

``server.py`` is a thin FastMCP wrapper over these methods; tests drive this class directly.
Each method returns plain data (dicts / lists) and raises :class:`GbkFsError` subclasses on
failure. No cross-call content cache is kept (NFR7/CR8) — only a per-session set of "files
read in this session" (for the Write safety check, FR8) and per-path locks.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any

from . import encoding as enc
from .config import Config
from .errors import (
    Conflict,
    GbkFsError,
    InvalidArguments,
    IsBinary,
    NotFound,
)
from .fileio import atomic_write, file_sha256, looks_binary, read_bytes, sha256_hex
from .locks import PathLocks


class GbkFs:
    def __init__(self, config: Config):
        self.config = config
        self.locks = PathLocks()
        self._read_in_session: set[str] = set()

    # ---------------------------------------------------------------- helpers
    def _resolve(self, path: str) -> tuple[Path, str]:
        from .paths import resolve_in_root, rel_to_root  # local import: keep module graph flat

        real = resolve_in_root(self.config.root, path)
        rel = rel_to_root(self.config.root, real)
        if self.config.is_denied(rel):
            from .errors import Denied

            raise Denied(
                f"path {rel!r} matches a deny-glob (binary/non-source); refusing to touch it"
            )
        return real, rel

    @staticmethod
    def _key(real: Path) -> str:
        return os.path.normcase(os.path.realpath(real))

    def _mark_read(self, real: Path) -> None:
        self._read_in_session.add(self._key(real))

    def _is_read(self, real: Path) -> bool:
        return self._key(real) in self._read_in_session

    def _check_conflict(
        self, raw: bytes, real: Path, expected_hash: str | None, expected_mtime: int | None
    ) -> None:
        if expected_hash is not None and sha256_hex(raw) != expected_hash:
            raise Conflict(
                f"file changed on disk since snapshot (hash mismatch): {real}. "
                f"Re-read before editing."
            )
        if expected_mtime is not None and real.stat().st_mtime_ns != int(expected_mtime):
            raise Conflict(
                f"file changed on disk since snapshot (mtime mismatch): {real}. Re-read first."
            )

    @staticmethod
    def _split_lines(view_lf: str) -> list[str]:
        """Split an LF view into display lines, dropping the trailing empty from a final NL."""
        if view_lf == "":
            return []
        lines = view_lf.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines

    @staticmethod
    def _number(lines: list[str], start: int) -> str:
        """cat -n style: right-justified line number, tab, content (parity with native Read)."""
        return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(lines, start))

    def _detect(self, raw: bytes, rel: str, explicit: str | None) -> enc.Detected:
        return enc.detect_encoding(
            raw,
            explicit=explicit,
            rule_encoding=self.config.encoding_for(rel),
            default_encoding=self.config.default_encoding,
        )

    def _unified_diff(self, rel: str, before: str, after: str) -> str:
        diff = difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
            n=2,
        )
        lines = list(diff)
        if len(lines) > 200:
            lines = lines[:200] + [f"... (diff truncated, {len(lines) - 200} more lines)"]
        return "\n".join(lines)

    # ---------------------------------------------------------------- read
    def read_file(
        self,
        path: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
        encoding: str | None = None,
    ) -> dict[str, Any]:
        real, rel = self._resolve(path)
        if not real.exists():
            raise NotFound(f"file not found: {rel}")
        if real.is_dir():
            raise InvalidArguments(f"path is a directory, not a file: {rel}")

        raw, truncated = read_bytes(real, limit=self.config.max_read_bytes)
        if looks_binary(raw[:8192]):
            raise IsBinary(f"file appears to be binary (NUL bytes); not decoding: {rel}")

        det = self._detect(raw, rel, encoding)
        body = raw[len(det.bom):]
        text = enc.decode_body(body, det.decode_codec, errors=self.config.on_decode_error)
        eol = enc.detect_eol(text) or self.config.default_eol
        final_nl = enc.has_final_newline(text)
        view = enc.normalize_to_lf(text)
        all_lines = self._split_lines(view)
        line_count = len(all_lines)

        start = max(1, offset or 1)
        end = line_count if limit is None else min(line_count, start - 1 + max(0, limit))
        selected = all_lines[start - 1 : end] if start <= line_count else []
        numbered = self._number(selected, start)

        self._mark_read(real)
        return {
            "path": rel,
            "abs": str(real),
            "content": numbered,
            "detected_encoding": det.logical,
            "eol": eol,
            "has_bom": det.has_bom,
            "final_newline": final_nl,
            "line_count": line_count,
            "start_line": start if selected else 0,
            "end_line": start - 1 + len(selected),
            "truncated": truncated,
            "sha256": file_sha256(real),
            "mtime_ns": real.stat().st_mtime_ns,
        }

    def read_files(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Batch read (§4.7). Partial failure does not abort the batch."""
        if not isinstance(items, list) or not items:
            raise InvalidArguments("read_files needs a non-empty list of {path, ...} items")
        results: list[dict[str, Any]] = []
        for it in items:
            if isinstance(it, str):
                it = {"path": it}
            path = it.get("path")
            if not path:
                results.append({"path": None, "ok": False, "error": "INVALID_ARGS: missing path"})
                continue
            try:
                res = self.read_file(
                    path,
                    offset=it.get("offset"),
                    limit=it.get("limit"),
                    encoding=it.get("encoding"),
                )
                res["ok"] = True
                results.append(res)
            except GbkFsError as e:
                results.append({"path": path, "ok": False, "error": str(e), "code": e.code})
        return {"results": results, "count": len(results)}

    # ---------------------------------------------------------------- write
    def write_file(
        self,
        path: str,
        content: str,
        *,
        encoding: str | None = None,
        eol: str | None = None,
        allow_overwrite_unread: bool = False,
        expected_hash: str | None = None,
        expected_mtime: int | None = None,
    ) -> dict[str, Any]:
        real, rel = self._resolve(path)
        if real.exists() and real.is_dir():
            raise InvalidArguments(f"path is a directory: {rel}")
        exists = real.exists()

        if exists and not self._is_read(real) and not allow_overwrite_unread:
            from .errors import UnreadOverwrite

            raise UnreadOverwrite(
                f"refusing to overwrite {rel!r} which was not read in this session; "
                f"read it first or pass allow_overwrite_unread=true (FR8)."
            )

        with self.locks.hold(real):
            old_raw = real.read_bytes() if exists else b""
            if exists and (expected_hash is not None or expected_mtime is not None):
                self._check_conflict(old_raw, real, expected_hash, expected_mtime)

            # Resolve target logical encoding.
            if encoding:
                logical = enc.normalize_codec(encoding)
                bom = self._bom_for_new(logical)
                detected_eol = None
            elif exists:
                det = self._detect(old_raw, rel, None)
                logical = det.logical
                bom = det.bom
                probe = enc.decode_body(old_raw[len(det.bom):], det.decode_codec, errors="replace")
                detected_eol = enc.detect_eol(probe)
            else:
                logical = self.config.encoding_for(rel) or self.config.default_encoding
                bom = self._bom_for_new(logical)
                detected_eol = None

            target_eol = (eol or detected_eol or self.config.default_eol).lower()
            if target_eol not in enc.EOL_STR:
                raise InvalidArguments(f"eol must be one of crlf/lf/cr, got {target_eol!r}")

            encode_codec = enc.python_encode_codec(logical, self.config.encode_codec)
            content_eol = enc.apply_eol(enc.normalize_to_lf(content), target_eol)
            encoded = enc.encode_text(content_eol, encode_codec)
            data = bom + encoded

            written = atomic_write(real, data)
            self._mark_read(real)

        return {
            "path": rel,
            "bytes_written": written,
            "encoding": logical,
            "eol": target_eol,
            "has_bom": bool(bom),
            "created": not exists,
            "sha256": sha256_hex(data),
            "mtime_ns": real.stat().st_mtime_ns,
        }

    @staticmethod
    def _bom_for_new(logical: str) -> bytes:
        import codecs

        logical = enc.normalize_codec(logical)
        return {
            "utf-8-sig": codecs.BOM_UTF8,
            "utf-16-le": codecs.BOM_UTF16_LE,
            "utf-16-be": codecs.BOM_UTF16_BE,
            "utf-16": codecs.BOM_UTF16_LE,
            "utf-32-le": codecs.BOM_UTF32_LE,
            "utf-32-be": codecs.BOM_UTF32_BE,
        }.get(logical, b"")

    # ---------------------------------------------------------------- edit
    def edit_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
        expected_hash: str | None = None,
        expected_mtime: int | None = None,
    ) -> dict[str, Any]:
        real, rel = self._resolve(path)
        if not real.exists():
            raise NotFound(f"file not found: {rel}")
        if real.is_dir():
            raise InvalidArguments(f"path is a directory: {rel}")
        if old_string == new_string:
            raise InvalidArguments("old_string and new_string are identical; nothing to do")

        with self.locks.hold(real):
            raw = real.read_bytes()
            if expected_hash is not None or expected_mtime is not None:
                self._check_conflict(raw, real, expected_hash, expected_mtime)

            data, result, logical, eol = self._stage_file(
                raw, rel, [{"old_string": old_string, "new_string": new_string,
                            "replace_all": replace_all}]
            )
            atomic_write(real, data)
            self._mark_read(real)

        rr = result[0][1]  # ReplaceResult of the single edit
        return {
            "path": rel,
            "replacements": rr.replacements,
            "encoding": logical,
            "eol": eol,
            "bytes_written": len(data),
            "sha256": sha256_hex(data),
            "mtime_ns": real.stat().st_mtime_ns,
            "diff": self._unified_diff(rel, rr.old_view_lf, rr.new_view_lf),
        }

    def _stage_file(
        self, raw: bytes, rel: str, file_edits: list[dict[str, Any]]
    ) -> tuple[bytes, list[tuple[dict, enc.ReplaceResult]], str, str]:
        """Apply a sequence of edits to one file's bytes in memory (no write).

        Returns ``(new_data, [(edit, ReplaceResult), ...], logical_encoding, eol)``. Edits
        apply in order, each seeing the previous result (CR7). Raises on any failure so the
        caller can abort an atomic batch before writing anything.
        """
        det = self._detect(raw, rel, None)
        bom = det.bom
        body = raw[len(bom):]
        encode_codec = enc.python_encode_codec(det.logical, self.config.encode_codec)
        probe = enc.decode_body(body, det.decode_codec, errors="replace")
        eol = enc.detect_eol(probe) or self.config.default_eol

        results: list[tuple[dict, enc.ReplaceResult]] = []
        for e in file_edits:
            res = enc.replace_in_body(
                body,
                decode_codec=det.decode_codec,
                encode_codec=encode_codec,
                eol=eol,
                old_string=e["old_string"],
                new_string=e["new_string"],
                replace_all=bool(e.get("replace_all", False)),
            )
            body = res.new_body
            results.append((e, res))

        return bom + body, results, det.logical, eol

    # ---------------------------------------------------------------- apply_edits (batch)
    def apply_edits(self, edits: list[dict[str, Any]], *, atomic: bool = True) -> dict[str, Any]:
        if not isinstance(edits, list) or not edits:
            raise InvalidArguments("apply_edits needs a non-empty list of edits")
        for i, e in enumerate(edits):
            if not isinstance(e, dict) or "path" not in e or "old_string" not in e or "new_string" not in e:
                raise InvalidArguments(
                    f"edit[{i}] needs path, old_string and new_string"
                )

        return self._apply_atomic(edits) if atomic else self._apply_nonatomic(edits)

    def _apply_nonatomic(self, edits: list[dict[str, Any]]) -> dict[str, Any]:
        """Independent edits; per-edit status; failures don't abort others (§4.7)."""
        results: list[dict[str, Any]] = []
        all_ok = True
        for i, e in enumerate(edits):
            try:
                r = self.edit_file(
                    e["path"],
                    e["old_string"],
                    e["new_string"],
                    replace_all=bool(e.get("replace_all", False)),
                    expected_hash=e.get("expected_hash"),
                    expected_mtime=e.get("expected_mtime"),
                )
                results.append({"index": i, "path": r["path"], "ok": True,
                                "replacements": r["replacements"]})
            except GbkFsError as exc:
                all_ok = False
                results.append({"index": i, "path": e.get("path"), "ok": False,
                                "error": str(exc), "code": exc.code})
        return {"ok": all_ok, "atomic": False, "results": results}

    def _apply_atomic(self, edits: list[dict[str, Any]]) -> dict[str, Any]:
        """All-or-nothing batch (CR6): stage + validate everything, then commit; roll back on
        any failure so the tree is never left half-changed."""
        # Resolve and group edits by file, preserving submission order (CR7).
        order: list[str] = []
        groups: dict[str, list[tuple[int, dict]]] = {}
        reals: dict[str, Path] = {}
        rels: dict[str, str] = {}
        for i, e in enumerate(edits):
            real, rel = self._resolve(e["path"])  # OUTSIDE_ROOT / DENIED abort the batch
            key = self._key(real)
            if key not in groups:
                groups[key] = []
                order.append(key)
                reals[key] = real
                rels[key] = rel
            groups[key].append((i, e))

        with self.locks.hold_many([reals[k] for k in order]):
            staged: dict[str, bytes] = {}
            originals: dict[str, bytes] = {}
            per_edit: dict[int, dict[str, Any]] = {}

            # ---- stage + validate (no writes) ----
            for key in order:
                real = reals[key]
                rel = rels[key]
                if not real.exists():
                    raise NotFound(f"file not found: {rel}")
                raw = real.read_bytes()
                originals[key] = raw

                # optimistic concurrency: any provided snapshot must match the original (CR5)
                for idx, e in groups[key]:
                    self._check_conflict(raw, real, e.get("expected_hash"), e.get("expected_mtime"))

                file_edits = [e for _idx, e in groups[key]]
                data, results, _logical, _eol = self._stage_file(raw, rel, file_edits)
                staged[key] = data
                for (idx, _e), (_edit, res) in zip(groups[key], results):
                    per_edit[idx] = {"index": idx, "path": rel, "ok": True,
                                     "replacements": res.replacements}

            # ---- commit (write everything; roll back on failure) ----
            committed: list[tuple[Path, bytes]] = []
            try:
                for key in order:
                    real = reals[key]
                    atomic_write(real, staged[key])
                    committed.append((real, originals[key]))
                    self._mark_read(real)
            except BaseException:
                for real, orig in reversed(committed):
                    try:
                        atomic_write(real, orig)
                    except OSError:
                        pass
                raise

        results_list = [per_edit[i] for i in sorted(per_edit)]
        return {"ok": True, "atomic": True, "files": len(order), "results": results_list}

    # ---------------------------------------------------------------- stat
    def stat_file(self, path: str, *, encoding: str | None = None) -> dict[str, Any]:
        real, rel = self._resolve(path)
        if not real.exists():
            raise NotFound(f"file not found: {rel}")
        if real.is_dir():
            raise InvalidArguments(f"path is a directory: {rel}")
        st = real.stat()
        sample, _ = read_bytes(real, limit=8192)
        if looks_binary(sample):
            return {
                "path": rel, "size": st.st_size, "is_binary": True,
                "encoding": None, "eol": None, "has_bom": False, "line_count": None,
                "mtime_ns": st.st_mtime_ns,
            }
        raw, truncated = read_bytes(real, limit=self.config.max_read_bytes)
        det = self._detect(raw, rel, encoding)
        text = enc.decode_body(raw[len(det.bom):], det.decode_codec, errors="replace")
        view = enc.normalize_to_lf(text)
        return {
            "path": rel,
            "size": st.st_size,
            "is_binary": False,
            "encoding": det.logical,
            "eol": enc.detect_eol(text) or self.config.default_eol,
            "has_bom": det.has_bom,
            "final_newline": enc.has_final_newline(text),
            "line_count": len(self._split_lines(view)),
            "truncated": truncated,
            "mtime_ns": st.st_mtime_ns,
            "sha256": file_sha256(real) if not truncated else None,
        }

    # ---------------------------------------------------------------- list / glob
    def list_files(
        self,
        *,
        glob: str = "**/*",
        path: str | None = None,
        sort: str = "name",
        with_details: bool = False,
        limit: int = 1000,
    ) -> dict[str, Any]:
        from .paths import compile_globs, match_any, rel_to_root

        base = self._resolve(path)[0] if path else Path(os.path.realpath(self.config.root))
        if not base.exists():
            raise NotFound(f"path not found: {path}")
        glob_re = compile_globs([glob])

        entries: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(base):
            # prune denied directories (e.g. .git) so we never descend into them
            dirnames[:] = [
                d for d in dirnames
                if not self.config.is_denied(rel_to_root(self.config.root, Path(dirpath) / d) + "/x")
            ]
            for fn in filenames:
                full = Path(dirpath) / fn
                rel_root = rel_to_root(self.config.root, full)
                if self.config.is_denied(rel_root):
                    continue
                rel_base = os.path.relpath(full, base).replace("\\", "/")
                if not match_any(rel_base, glob_re):
                    continue
                entry: dict[str, Any] = {"path": rel_root}
                if with_details:
                    try:
                        st = full.stat()
                        entry["size"] = st.st_size
                        entry["mtime_ns"] = st.st_mtime_ns
                        sample, _ = read_bytes(full, limit=8192)
                        if looks_binary(sample):
                            entry["detected_encoding"] = None
                            entry["is_binary"] = True
                        else:
                            det = self._detect(sample, rel_root, None)
                            entry["detected_encoding"] = det.logical
                            entry["is_binary"] = False
                    except OSError:
                        pass
                entries.append(entry)

        if sort == "mtime" and with_details:
            entries.sort(key=lambda e: e.get("mtime_ns", 0), reverse=True)
        else:
            entries.sort(key=lambda e: e["path"].lower())

        truncated = len(entries) > limit
        return {"files": entries[:limit], "count": len(entries[:limit]), "truncated": truncated}

    # ---------------------------------------------------------------- search
    def search_content(self, **kwargs: Any) -> dict[str, Any]:
        from .search import search_content as _search

        return _search(self, **kwargs)
