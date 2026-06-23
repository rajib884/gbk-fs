# gbk-fs — encoding-aware filesystem MCP server

Give Claude Code (or any MCP client) a set of filesystem tools for a **GBK-encoded
codebase** — reading, searching, writing and editing source files that contain Chinese
comments **without** shelling out to `iconv` / `grep`, and **without corrupting** the files
on write.

* **UTF-8 to the model, on-disk encoding on disk.** Reads decode GBK transparently and
  return clean UTF-8; writes encode back to the file's original family.
* **Byte-level round-trip fidelity.** Edits decode only to *locate* the change, then splice
  raw bytes and re-encode only the changed text — every other byte is preserved exactly, so
  Chinese comments, EOLs, BOMs and final newlines never churn.
* **Author new CJK losslessly.** The default encode codec is **GB18030** — a strict
  superset of GBK that is byte-identical to GBK for every GBK character, so existing bytes
  never change *and* any Unicode/CJK character can be written. A strict `gbk` mode is
  available and fails loudly on non-GBK characters rather than substituting `?`.

Implements the requirements in [`gbk-fs-mcp-server-requirements.md`](./gbk-fs-mcp-server-requirements.md).

## Tools

| Tool | Purpose |
|---|---|
| `read_file` | UTF-8 content with `cat -n` line numbers + metadata (`encoding`, `eol`, `bom`, `line_count`, `sha256`) |
| `read_files` | Batch read; parallel; partial failures reported per item |
| `write_file` | Create/overwrite; encodes to target; refuses to clobber an unread file (FR8) |
| `edit_file` | Exact-match replace with byte-faithful splicing; returns a unified diff |
| `apply_edits` | Many edits across files in one call; transactional (`atomic=true`) with rollback |
| `search_content` | ripgrep-class regex search; decodes per-file before matching; UTF-8 out |
| `list_files` | Glob listing (`**`, `{a,b}` supported), optional size/mtime/encoding |
| `stat_file` | Metadata only (encoding, eol, bom, size, line_count, is_binary) |

## Install

The server is self-contained in a virtualenv.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e .      # Windows
# source .venv/bin/activate && pip install -e .   # POSIX
```

Requires Python ≥ 3.10 and `mcp`. No external `iconv`/`ripgrep` binaries are needed.

## Configure

Create a `.gbk-fs.json` at your repo root (JSONC: comments and trailing commas allowed). See
[`.gbk-fs.example.json`](./.gbk-fs.example.json):

```jsonc
{
  "root": "d:/compile/220F_6vPE",   // sandbox boundary; all paths resolve under here
  "defaultEncoding": "gbk",          // how existing files are decoded/detected
  "encodeCodec": "gb18030",          // how new content is encoded: gb18030 (default) | gbk
  "encodingRules": [
    { "glob": "**/*.{c,h,cpp,inc}", "encoding": "gbk" },
    { "glob": "**/*.{md,json,txt}", "encoding": "utf-8" }
  ],
  "defaultEol": "crlf",
  "onDecodeError": "strict",         // strict | replace
  "maxReadBytes": 5000000,
  "denyGlobs": ["**/*.a", "**/*.o", "**/*.doc", "**/.git/**"]
}
```

**Encoding precedence:** explicit `encoding` arg → per-glob rule → auto-detect (BOM sniff →
strict-UTF-8 → `defaultEncoding`).

## Register with Claude Code

Point the server at your real source tree with `--root` (or set `root` in the config). The
sandbox confines all operations to that tree.

```bash
claude mcp add gbk-fs -- /path/to/.venv/bin/python -m gbk_fs --root d:/compile/220F_6vPE
```

Or in `.mcp.json`:

```json
{
  "mcpServers": {
    "gbk-fs": {
      "command": "d:/MCP/.venv/Scripts/python.exe",
      "args": ["-m", "gbk_fs", "--root", "d:/compile/220F_6vPE"]
    }
  }
}
```

You can also pass `--config path/to/.gbk-fs.json`, or set `GBK_FS_ROOT` / `GBK_FS_CONFIG`.

## How edits stay byte-faithful

```
read raw bytes ──▶ decode to a UTF-8 view (locate match) ──▶ map char offsets to byte offsets
      │                                                                   │
      └──────────────── splice raw bytes, re-encoding ONLY new_string ◀───┘
```

Only the replaced span's bytes change. The decode is used purely to find the match; the rest
of the file is copied verbatim, so a `git diff` shows only the line you actually changed.

## Safety

* **Sandbox:** every path is resolved through symlinks/`..` and must land inside `root`.
* **Deny-list:** binaries / `.git` are never decoded or descended into.
* **Concurrency:** atomic writes (temp + rename); per-path locks serialize same-file edits;
  optimistic-concurrency via `expected_hash` rejects lost updates; `apply_edits` is
  all-or-nothing.
* **No silent loss:** an unrepresentable character fails loudly with its offset.

## Develop / test

```bash
.venv/Scripts/python -m pytest        # 42 tests: encoding, fidelity, search, sandbox, concurrency
```

## License

MIT
