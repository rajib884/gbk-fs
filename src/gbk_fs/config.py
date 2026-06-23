"""Configuration loading (§5).

Config comes from a per-repo ``.gbk-fs.json`` (JSONC: ``//`` and ``/* */`` comments and
trailing commas are tolerated) and/or explicit overrides. It defines the sandbox root, how
existing files are decoded/detected, how new content is encoded, per-glob encoding rules,
default EOL, a read-size guard rail, and the binary deny-list.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import InvalidArguments
from .paths import compile_globs, match_any

DEFAULT_DENY = [
    "**/*.a",
    "**/*.o",
    "**/*.so",
    "**/*.lib",
    "**/*.exe",
    "**/*.dll",
    "**/*.bin",
    "**/*.doc",
    "**/*.docx",
    "**/*.pdf",
    "**/*.zip",
    "**/*.png",
    "**/*.jpg",
    "**/*.jpeg",
    "**/*.gif",
    "**/.git/**",
]

DEFAULT_RULES = [
    {"glob": "**/*.{c,h,cpp,cc,cxx,hpp,inc}", "encoding": "gbk"},
    {"glob": "**/*.{md,json,jsonc,txt,py,yml,yaml,toml,xml,html,js,ts}", "encoding": "utf-8"},
]


@dataclass
class EncodingRule:
    glob: str
    encoding: str
    _regexes: list = field(default_factory=list, repr=False)


@dataclass
class Config:
    root: Path
    default_encoding: str = "gbk"
    encode_codec: str = "gb18030"
    default_eol: str = "crlf"
    max_read_bytes: int = 5_000_000
    on_decode_error: str = "strict"  # "strict" | "replace" (NFR6)
    rules: list[EncodingRule] = field(default_factory=list)
    deny_globs: list[str] = field(default_factory=lambda: list(DEFAULT_DENY))
    _deny_re: list = field(default_factory=list, repr=False)

    def encoding_for(self, rel_path: str) -> str | None:
        """First matching per-glob encoding rule for ``rel_path`` (or None)."""
        for rule in self.rules:
            if match_any(rel_path, rule._regexes):
                return rule.encoding
        return None

    def is_denied(self, rel_path: str) -> bool:
        return match_any(rel_path, self._deny_re)


def _strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments and trailing commas, ignoring string contents."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    cleaned = "".join(out)
    # drop trailing commas before } or ]
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


def _build(raw: dict, root: Path) -> Config:
    rules_in = raw.get("encodingRules", DEFAULT_RULES)
    rules = []
    for r in rules_in:
        if "glob" not in r or "encoding" not in r:
            raise InvalidArguments(f"encodingRule needs 'glob' and 'encoding': {r!r}")
        rule = EncodingRule(glob=r["glob"], encoding=r["encoding"])
        rule._regexes = compile_globs([r["glob"]])
        rules.append(rule)

    deny = raw.get("denyGlobs", DEFAULT_DENY)
    cfg = Config(
        root=Path(root),
        default_encoding=str(raw.get("defaultEncoding", "gbk")).lower(),
        encode_codec=str(raw.get("encodeCodec", "gb18030")).lower(),
        default_eol=str(raw.get("defaultEol", "crlf")).lower(),
        max_read_bytes=int(raw.get("maxReadBytes", 5_000_000)),
        on_decode_error=str(raw.get("onDecodeError", "strict")).lower(),
        rules=rules,
        deny_globs=list(deny),
    )
    if cfg.on_decode_error not in ("strict", "replace"):
        raise InvalidArguments("onDecodeError must be 'strict' or 'replace'")
    if cfg.default_eol not in ("crlf", "lf", "cr"):
        raise InvalidArguments("defaultEol must be 'crlf', 'lf' or 'cr'")
    cfg._deny_re = compile_globs(deny)
    return cfg


def load_config(
    *,
    config_path: str | Path | None = None,
    root: str | Path | None = None,
    overrides: dict | None = None,
) -> Config:
    """Load configuration.

    Resolution: read ``config_path`` if given; else look for ``<root>/.gbk-fs.json``. The
    ``root`` argument (or ``--root``) wins over a ``root`` field in the file so the server
    can be pointed at a tree without editing config. ``overrides`` (dict) is applied last.
    """
    raw: dict = {}
    cfg_file: Path | None = None

    if config_path:
        cfg_file = Path(config_path)
    elif root:
        candidate = Path(root) / ".gbk-fs.json"
        if candidate.is_file():
            cfg_file = candidate

    if cfg_file is not None:
        if not cfg_file.is_file():
            raise InvalidArguments(f"config file not found: {cfg_file}")
        raw = json.loads(_strip_jsonc(cfg_file.read_text(encoding="utf-8")))

    if overrides:
        raw = {**raw, **overrides}

    # Determine root: explicit arg > config "root" field > config file's directory > cwd.
    if root is not None:
        root_path = Path(root)
    elif "root" in raw:
        root_path = Path(raw["root"])
    elif cfg_file is not None:
        root_path = cfg_file.resolve().parent
    else:
        root_path = Path.cwd()

    root_path = root_path.expanduser()
    if not root_path.exists():
        raise InvalidArguments(f"root does not exist: {root_path}")

    return _build(raw, root_path.resolve())
