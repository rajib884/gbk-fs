"""FastMCP wiring (§8 transport: stdio).

Thin presentation layer over :class:`gbk_fs.core.GbkFs`. Tools return readable strings
(numbered content, diffs, ``path:line`` matches) for clean terminal display; operational
failures surface as MCP tool errors carrying the stable error code.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import __version__
from .core import GbkFs
from .errors import GbkFsError

try:  # pragma: no cover - depends on mcp version
    from mcp.server.fastmcp.exceptions import ToolError
except Exception:  # pragma: no cover
    ToolError = RuntimeError  # type: ignore[assignment,misc]


def _err(exc: GbkFsError):
    """Convert an internal error into a clean MCP tool error (sets isError)."""
    return ToolError(str(exc))


# --------------------------------------------------------------------------------------
# Formatting helpers (data -> readable text)
# --------------------------------------------------------------------------------------


def _meta_line(res: dict[str, Any]) -> str:
    bom = "yes" if res.get("has_bom") else "no"
    trunc = " truncated=yes" if res.get("truncated") else ""
    return (
        f"[gbk-fs] {res['path']} | encoding={res['detected_encoding']} eol={res['eol']} "
        f"bom={bom} lines={res['line_count']}{trunc} sha256={res['sha256']}"
    )


def _format_read(res: dict[str, Any]) -> str:
    body = res["content"] if res["content"] else "(empty selection)"
    return f"{body}\n\n{_meta_line(res)}"


def _format_read_git(res: dict[str, Any]) -> str:
    body = res["content"] if res["content"] else "(empty selection)"
    bom = "yes" if res.get("has_bom") else "no"
    trunc = " truncated=yes" if res.get("truncated") else ""
    meta = (
        f"[gbk-fs git:{res['ref']}] {res['path']} | encoding={res['detected_encoding']} "
        f"eol={res['eol']} bom={bom} lines={res['line_count']}{trunc} sha256={res['sha256']}"
    )
    return f"{body}\n\n{meta}"


def _format_read_files(res: dict[str, Any]) -> str:
    chunks: list[str] = []
    for r in res["results"]:
        if r.get("ok"):
            header = (
                f"===== {r['path']} "
                f"(encoding={r['detected_encoding']}, eol={r['eol']}, lines={r['line_count']}) ====="
            )
            chunks.append(f"{header}\n{r['content'] or '(empty)'}")
        else:
            chunks.append(f"===== {r.get('path')} =====\nERROR {r.get('code','')}: {r.get('error')}")
    return "\n\n".join(chunks)


def _format_search(res: dict[str, Any]) -> str:
    mode = res["mode"]
    if mode == "files":
        lines = res["results"]
        head = f"[gbk-fs search] {res['files_matched']} file(s) matched of {res['files_searched']} searched"
        body = "\n".join(lines) if lines else "(no matches)"
        return f"{body}\n\n{head}" + (" (truncated)" if res.get("truncated") else "")
    if mode == "count":
        body = "\n".join(f"{r['path']}:{r['count']}" for r in res["results"]) or "(no matches)"
        head = f"[gbk-fs search] {res['total_matches']} match line(s) across {len(res['results'])} file(s)"
        return f"{body}\n\n{head}" + (" (truncated)" if res.get("truncated") else "")

    # content
    out: list[str] = []
    for fileres in res["results"]:
        path = fileres["path"]
        for entry in fileres["lines"]:
            if entry.get("separator"):
                out.append("--")
                continue
            sep = ":" if entry.get("is_match") else "-"
            out.append(f"{path}{sep}{entry['line']}{sep}{entry['text']}")
    head = (
        f"[gbk-fs search] {res['total_matches']} match line(s) in "
        f"{res['files_matched']} file(s) of {res['files_searched']} searched"
    )
    body = "\n".join(out) if out else "(no matches)"
    return f"{body}\n\n{head}" + (" (truncated)" if res.get("truncated") else "")


def _format_apply(res: dict[str, Any]) -> str:
    head = f"[gbk-fs apply_edits] atomic={res['atomic']} ok={res['ok']}"
    rows = []
    for r in res["results"]:
        if r.get("ok"):
            rows.append(f"  #{r['index']} {r['path']}: {r['replacements']} replacement(s)")
        else:
            rows.append(f"  #{r['index']} {r.get('path')}: ERROR {r.get('code','')}: {r.get('error')}")
    return head + "\n" + "\n".join(rows)


def _format_list(res: dict[str, Any]) -> str:
    rows = []
    for e in res["files"]:
        if "size" in e:
            extra = f"  ({e['size']} bytes, {e.get('detected_encoding') or 'binary'})"
        else:
            extra = ""
        rows.append(f"{e['path']}{extra}")
    head = f"[gbk-fs] {res['count']} file(s)" + (" (truncated)" if res.get("truncated") else "")
    body = "\n".join(rows) if rows else "(none)"
    return f"{body}\n\n{head}"


# --------------------------------------------------------------------------------------
# Server construction
# --------------------------------------------------------------------------------------


def build_server(core: GbkFs) -> FastMCP:
    mcp = FastMCP(
        "gbk-fs",
        instructions=(
            "Encoding-aware filesystem tools for a GBK-encoded repo. All content is UTF-8 to "
            "you; files persist in their on-disk encoding (GBK family) with byte-level "
            "round-trip fidelity. Prefer these over Bash iconv/grep and over the native "
            "Read/Write/Edit for files in this repo. Use the batch tools (read_files, "
            "apply_edits) instead of many sequential single-file calls."
        ),
    )
    # FastMCP doesn't accept a version; set it on the underlying server so clients see ours.
    mcp._mcp_server.version = __version__

    @mcp.tool()
    def read_file(
        path: Annotated[str, Field(description="Absolute or repo-relative path")],
        offset: Annotated[int | None, Field(description="1-based first line to return")] = None,
        limit: Annotated[int | None, Field(description="Max number of lines to return")] = None,
        encoding: Annotated[str | None, Field(description="Override detected encoding")] = None,
    ) -> str:
        """Read a file as UTF-8 with cat -n line numbers, decoding GBK transparently."""
        try:
            return _format_read(core.read_file(path, offset=offset, limit=limit, encoding=encoding))
        except GbkFsError as e:
            raise _err(e)

    @mcp.tool()
    def read_files(
        items: Annotated[
            list[dict[str, Any]],
            Field(description="List of {path, offset?, limit?, encoding?}. Partial failures are "
                              "reported per item, not fatal."),
        ],
    ) -> str:
        """Batch read many files in one call (parallel, partial-failure tolerant)."""
        try:
            return _format_read_files(core.read_files(items))
        except GbkFsError as e:
            raise _err(e)

    @mcp.tool()
    def read_git(
        path: Annotated[str, Field(description="Absolute or repo-relative path")],
        ref: Annotated[
            str,
            Field(description="Git ref: HEAD, a commit SHA, a branch, HEAD~2, or ':0:' / "
                              "'index' for the staged (index) version"),
        ] = "HEAD",
        offset: Annotated[int | None, Field(description="1-based first line to return")] = None,
        limit: Annotated[int | None, Field(description="Max number of lines to return")] = None,
        encoding: Annotated[str | None, Field(description="Override detected encoding")] = None,
    ) -> str:
        """Read a file from a git ref (HEAD/SHA/branch/index), decoding GBK transparently.

        For recovering a corrupted working file from its clean committed or staged bytes.
        Does NOT mark the working file read, so it won't satisfy write_file's
        unread-overwrite guard.
        """
        try:
            return _format_read_git(
                core.read_git(path, ref, offset=offset, limit=limit, encoding=encoding)
            )
        except GbkFsError as e:
            raise _err(e)

    @mcp.tool()
    def write_file(
        path: Annotated[str, Field(description="Absolute or repo-relative path")],
        content: Annotated[str, Field(description="Full UTF-8 content to write")],
        encoding: Annotated[str | None, Field(description="Target encoding override")] = None,
        eol: Annotated[str | None, Field(description="crlf | lf | cr")] = None,
        allow_overwrite_unread: Annotated[
            bool, Field(description="Permit overwriting a file not read this session")
        ] = False,
        expected_hash: Annotated[
            str | None, Field(description="sha256 from a prior read; rejects if file changed")
        ] = None,
        allow_replacement_chars: Annotated[
            bool, Field(description="Permit writing U+FFFD (replacement char); refused by "
                                    "default as it signals upstream corruption")
        ] = False,
    ) -> str:
        """Create or overwrite a file, encoding to the target on-disk encoding (GBK/GB18030)."""
        try:
            r = core.write_file(
                path, content, encoding=encoding, eol=eol,
                allow_overwrite_unread=allow_overwrite_unread, expected_hash=expected_hash,
                allow_replacement_chars=allow_replacement_chars,
            )
            verb = "Created" if r["created"] else "Wrote"
            return (
                f"{verb} {r['path']}: {r['bytes_written']} bytes, encoding={r['encoding']}, "
                f"eol={r['eol']}, bom={'yes' if r['has_bom'] else 'no'}. sha256={r['sha256']}"
            )
        except GbkFsError as e:
            raise _err(e)

    @mcp.tool()
    def edit_file(
        path: Annotated[str, Field(description="Absolute or repo-relative path")],
        old_string: Annotated[str, Field(description="Exact text to replace (UTF-8 view, LF)")],
        new_string: Annotated[str, Field(description="Replacement text (UTF-8; new CJK is OK)")],
        replace_all: Annotated[bool, Field(description="Replace every occurrence")] = False,
        expected_hash: Annotated[
            str | None, Field(description="sha256 from a prior read; rejects if file changed")
        ] = None,
        allow_replacement_chars: Annotated[
            bool, Field(description="Permit U+FFFD (replacement char) in new_string; refused "
                                    "by default as it signals upstream corruption")
        ] = False,
    ) -> str:
        """Edit a file by exact match; untouched bytes stay byte-identical (no encoding churn)."""
        try:
            r = core.edit_file(
                path, old_string, new_string, replace_all=replace_all, expected_hash=expected_hash,
                allow_replacement_chars=allow_replacement_chars,
            )
            head = (
                f"Edited {r['path']}: {r['replacements']} replacement(s), encoding={r['encoding']}, "
                f"eol={r['eol']}. new sha256={r['sha256']}"
            )
            return f"{head}\n{r['diff']}" if r["diff"] else head
        except GbkFsError as e:
            raise _err(e)

    @mcp.tool()
    def apply_edits(
        edits: Annotated[
            list[dict[str, Any]],
            Field(description="List of {path, old_string, new_string, replace_all?, "
                              "expected_hash?, allow_replacement_chars?}. Same-file edits "
                              "apply in order."),
        ],
        atomic: Annotated[
            bool, Field(description="All-or-nothing: validate all, then commit, else roll back")
        ] = True,
    ) -> str:
        """Apply many edits across files in one call (transactional when atomic=true)."""
        try:
            return _format_apply(core.apply_edits(edits, atomic=atomic))
        except GbkFsError as e:
            raise _err(e)

    @mcp.tool()
    def search_content(
        pattern: Annotated[str | None, Field(description="Regex to search for")] = None,
        patterns: Annotated[
            list[str] | None, Field(description="Multiple regexes, OR-combined in one pass")
        ] = None,
        path: Annotated[str | None, Field(description="File or dir to search (default: root)")] = None,
        glob: Annotated[str | None, Field(description="Filter files, e.g. **/*.c")] = None,
        type: Annotated[str | None, Field(description="rg-style type, e.g. c, py, md")] = None,
        output_mode: Annotated[str, Field(description="content | files | count")] = "content",
        ignore_case: Annotated[bool, Field(description="Case-insensitive")] = False,
        before: Annotated[int, Field(description="Lines of context before (-B)")] = 0,
        after: Annotated[int, Field(description="Lines of context after (-A)")] = 0,
        context: Annotated[int, Field(description="Lines of context around (-C)")] = 0,
        head_limit: Annotated[int, Field(description="Cap results")] = 200,
        multiline: Annotated[bool, Field(description="Pattern may span lines")] = False,
    ) -> str:
        """Search file contents (ripgrep-class), decoding GBK before matching; UTF-8 out."""
        try:
            return _format_search(core.search_content(
                pattern=pattern, patterns=patterns, path=path, glob=glob, type=type,
                output_mode=output_mode, ignore_case=ignore_case, before=before, after=after,
                context=context, head_limit=head_limit, multiline=multiline,
            ))
        except GbkFsError as e:
            raise _err(e)

    @mcp.tool()
    def list_files(
        glob: Annotated[str, Field(description="Glob, e.g. **/*.{c,h}")] = "**/*",
        path: Annotated[str | None, Field(description="Directory to list under")] = None,
        sort: Annotated[str, Field(description="name | mtime")] = "name",
        with_details: Annotated[bool, Field(description="Include size/mtime/encoding")] = False,
        limit: Annotated[int, Field(description="Max entries")] = 1000,
    ) -> str:
        """List files matching a glob (relative to root)."""
        try:
            return _format_list(core.list_files(
                glob=glob, path=path, sort=sort, with_details=with_details, limit=limit,
            ))
        except GbkFsError as e:
            raise _err(e)

    @mcp.tool()
    def stat_file(
        path: Annotated[str, Field(description="Absolute or repo-relative path")],
        encoding: Annotated[str | None, Field(description="Override detected encoding")] = None,
    ) -> dict[str, Any]:
        """Return file metadata (encoding, eol, bom, size, line_count, is_binary) without content."""
        try:
            return core.stat_file(path, encoding=encoding)
        except GbkFsError as e:
            raise _err(e)

    return mcp
