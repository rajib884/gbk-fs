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

## Tools

| Tool | Purpose |
|---|---|
| `read_file` | UTF-8 content with `cat -n` line numbers + metadata (`encoding`, `eol`, `bom`, `line_count`, `sha256`) |
| `read_files` | Batch read; parallel; partial failures reported per item |
| `read_git` | Read a file's bytes from a git ref (`HEAD`, a SHA, a branch, `:0:`/index) and decode like `read_file` — recover a corrupted working file from its clean committed/staged source |
| `write_file` | Create/overwrite; encodes to target; refuses to clobber an unread file (FR8) or to write `U+FFFD` corruption |
| `edit_file` | Exact-match replace with byte-faithful splicing; returns a unified diff; refuses `U+FFFD` in `new_string` |
| `apply_edits` | Many edits across files in one call; transactional (`atomic=true`) with rollback |
| `search_content` | ripgrep-class regex search; decodes per-file before matching; UTF-8 out. Supports Unicode properties (`\p{Han}`), brace-hex (`\x{4e00}`) and explicit ranges (`[一-鿿]`) |
| `list_files` | Glob listing (`**`, `{a,b}` supported), optional size/mtime/encoding |
| `stat_file` | Metadata only (encoding, eol, bom, size, line_count, is_binary) |

## Install

```bash
git clone https://github.com/rajib884/gbk-fs.git
cd gbk-fs
python -m venv .venv
.venv/Scripts/python -m pip install -e .
```

For better encoding auto-detection: `.venv/Scripts/python -m pip install -e .[detect]`

## Register with Claude Code

Use the **venv** Python so the bundled dependencies are on the path.

**Global install (user scope)** — registers the server once for your user, so it's available
in every project you open:

```bash
claude mcp add gbk-fs --scope user -- /path/to/gbk-fs/.venv/Scripts/python.exe -m gbk_fs
```

Scopes: `--scope user` (all your projects), `--scope project` (shared via a committed
`.mcp.json`), `--scope local` (this project only, the default). Manage with
`claude mcp list`, `claude mcp get gbk-fs`, `claude mcp remove gbk-fs`.

Or configure it manually in `.mcp.json` (project scope):

```json
{
  "mcpServers": {
    "gbk-fs": {
      "command": "/path/to/gbk-fs/.venv/Scripts/python.exe",
      "args": ["-m", "gbk_fs"]
    }
  }
}
```

You can also pass `--root /path/to/your/repo`, `--config path/to/.gbk-fs.json`, or set `GBK_FS_ROOT` / `GBK_FS_CONFIG`.

## Configure

Create a `.gbk-fs.json` at your repo root. See [`.gbk-fs.example.json`](./.gbk-fs.example.json):

```jsonc
{
  "root": "/path/to/your/repo",      // sandbox boundary; all paths resolve under here
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

## How edits stay byte-faithful

```
read raw bytes ──▶ decode to a UTF-8 view (locate match) ──▶ map char offsets to byte offsets
      │                                                                   │
      └──────────────── splice raw bytes, re-encoding ONLY new_string ◀───┘
```

Only the replaced span's bytes change. The decode is used purely to find the match; the rest
of the file is copied verbatim, so a `git diff` shows only the line you actually changed.

## Recovering a corrupted file

If a file's working-tree bytes get mangled — e.g. another editor saved a GBK file as UTF-8,
turning every Chinese character into `�` (U+FFFD) — the clean source is git, not the working
tree. `read_git` decodes a file's bytes from any ref through the same pipeline as `read_file`:

* `read_git path=src/foo.c ref=HEAD` — the last committed version
* `read_git path=src/foo.c ref=:0:` — the staged/index version (`index` / `staged` also work)

Recover by reading the clean version and writing it back (GB18030 re-encodes GBK content
byte-for-byte), then re-applying any genuine changes with `edit_file`. `read_git` never marks
the working file "read", so it can't accidentally satisfy the unread-overwrite guard.

## Safety

* **Sandbox:** every path is resolved through symlinks/`..` and must land inside `root`.
* **Deny-list:** binaries / `.git` are never decoded or descended into.
* **Concurrency:** atomic writes (temp + rename); per-path locks serialize same-file edits;
  optimistic-concurrency via `expected_hash` rejects lost updates; `apply_edits` is
  all-or-nothing.
* **No silent loss:** an unrepresentable character fails loudly with its offset.
* **Corruption guard:** writes/edits carrying `U+FFFD` (the replacement character — the
  signature of bytes that were decoded lossily upstream) are refused, *even though* the
  default GB18030 codec could encode it. Pass `allow_replacement_chars=true` to override.

## Develop / test

```bash
.venv/Scripts/python -m pytest        # 57 tests: encoding, fidelity, search, sandbox, concurrency, corruption guard, git-ref reads
```

## License

MIT
