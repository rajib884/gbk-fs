"""Encoding core: detection, decode/encode, EOL/BOM handling, and the byte-splice edit.

This module is pure (no I/O, no config object) so it is trivially testable. It implements
the behaviour mandated by §3 / §3.1 of the requirements:

* Reads present clean UTF-8 to the model regardless of on-disk encoding (G1).
* Writes persist in the file's on-disk family with byte-level round-trip fidelity for
  unchanged regions (G2): edits decode only to *locate* the change, then splice raw bytes
  and re-encode only ``new_string`` (§8 "edit fidelity pattern").
* The default encode codec for the GBK family is **GB18030** — a strict superset of GBK
  that emits byte-identical sequences for every GBK character, so existing bytes never
  change while any CJK character can be authored (§3.1, FR6a).
* EOL, BOM and final-newline are preserved exactly (FR5).
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

from .errors import AmbiguousMatch, DecodeError, LossyEncode, MatchNotFound

# --------------------------------------------------------------------------------------
# Codec families
# --------------------------------------------------------------------------------------

#: Logical names that belong to the Chinese/"GBK" family. For these, writes use the
#: configured ``encodeCodec`` (default ``gb18030``) and reads are decoded with the
#: superset ``gb18030`` (per §3.1 this yields identical text to ``gbk`` for GBK content
#: while being robust to extended bytes).
GBK_FAMILY = frozenset({"gbk", "gb2312", "gb18030", "cp936", "ms936", "936"})

# BOM signatures, longest-first so UTF-32 is checked before UTF-16.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

EOL_STR = {"crlf": "\r\n", "lf": "\n", "cr": "\r"}


def normalize_codec(name: str) -> str:
    """Normalize a user/codec name to a canonical lowercase token."""
    return name.strip().lower().replace("_", "-")


def python_decode_codec(logical: str) -> str:
    """Python codec to *decode* a file of the given logical encoding.

    The GBK family decodes via ``gb18030`` (superset of GBK; §3.1: same text as GBK for
    GBK content, but tolerant of the full GB18030 byte space).
    """
    logical = normalize_codec(logical)
    if logical in GBK_FAMILY:
        return "gb18030"
    if logical == "utf-8-sig":
        return "utf-8"  # BOM is stripped separately; body is plain UTF-8
    return logical


def python_encode_codec(logical: str, encode_codec_for_gbk: str) -> str:
    """Python codec to *encode* new content for a file of the given logical encoding.

    GBK-family files encode with the configured codec (default ``gb18030``); everything
    else round-trips through its own codec.
    """
    logical = normalize_codec(logical)
    if logical in GBK_FAMILY:
        return normalize_codec(encode_codec_for_gbk)
    if logical == "utf-8-sig":
        return "utf-8"
    return logical


# --------------------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------------------


@dataclass
class Detected:
    """Result of resolving how to decode a file's bytes."""

    logical: str          # reported `detected_encoding` (e.g. "gbk", "utf-8")
    decode_codec: str     # python codec used on the body (e.g. "gb18030")
    bom: bytes            # the BOM bytes present at the start (b"" if none)

    @property
    def has_bom(self) -> bool:
        return bool(self.bom)


def _sniff_bom(raw: bytes) -> tuple[str | None, bytes]:
    for sig, name in _BOMS:
        if raw.startswith(sig):
            return name, sig
    return None, b""


def _looks_like_utf8(raw: bytes) -> bool:
    """True if the bytes decode cleanly as strict UTF-8.

    Valid UTF-8 with multibyte sequences is a strong signal; substantial GBK text almost
    never forms a fully valid UTF-8 stream. This is the "statistical GBK-vs-UTF-8" step.
    """
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def detect_encoding(
    raw: bytes,
    *,
    explicit: str | None = None,
    rule_encoding: str | None = None,
    default_encoding: str = "gbk",
) -> Detected:
    """Resolve the decode strategy for ``raw`` following the §3 precedence.

    Precedence: explicit arg > per-glob rule > auto (BOM sniff -> strict-UTF-8 -> default).
    A BOM is only *stripped* when the chosen logical encoding is the matching Unicode
    family (a GBK file whose bytes happen to start with 0xEF 0xBB 0xBF is **not** treated
    as having a BOM).
    """
    bom_name, bom_bytes = _sniff_bom(raw)

    if explicit:
        logical = normalize_codec(explicit)
    elif rule_encoding:
        logical = normalize_codec(rule_encoding)
    elif bom_name:
        logical = bom_name
    elif _looks_like_utf8(raw):
        logical = "utf-8"
    else:
        logical = normalize_codec(default_encoding)

    # Only honour a BOM when it is consistent with the chosen Unicode encoding.
    bom = b""
    if logical in ("utf-8-sig",) and bom_name == "utf-8-sig":
        bom = bom_bytes
    elif logical == "utf-8" and bom_name == "utf-8-sig":
        # explicit/rule said plain utf-8 but bytes carry a BOM -> treat as utf-8-sig
        logical = "utf-8-sig"
        bom = bom_bytes
    elif logical in ("utf-16-le", "utf-16-be") and bom_name == logical:
        bom = bom_bytes
    elif logical in ("utf-16",) and bom_name in ("utf-16-le", "utf-16-be"):
        logical = bom_name
        bom = bom_bytes
    elif logical in ("utf-32-le", "utf-32-be") and bom_name == logical:
        bom = bom_bytes

    return Detected(logical=logical, decode_codec=python_decode_codec(logical), bom=bom)


# --------------------------------------------------------------------------------------
# Decode / line-ending / final-newline helpers
# --------------------------------------------------------------------------------------


def decode_body(body: bytes, decode_codec: str, *, errors: str = "strict") -> str:
    """Decode the body (BOM already stripped) to text, mapping codec errors to DecodeError."""
    try:
        return body.decode(decode_codec, errors)
    except UnicodeDecodeError as exc:  # pragma: no cover - exercised via core/tests
        raise DecodeError(
            f"cannot decode bytes as {decode_codec!r} at byte {exc.start}: {exc.reason}. "
            f"Set an explicit `encoding`, add an encodingRule, or use onDecodeError=replace."
        ) from exc


def detect_eol(text: str) -> str | None:
    """Return the dominant EOL style of ``text`` ('crlf'|'lf'|'cr'), or None if no newline."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf      # lone LFs
    cr = text.count("\r") - crlf      # lone CRs
    if crlf == 0 and lf == 0 and cr == 0:
        return None
    best = max(("crlf", crlf), ("lf", lf), ("cr", cr), key=lambda kv: kv[1])
    return best[0]


def has_final_newline(text: str) -> bool:
    return text.endswith("\n") or text.endswith("\r")


def normalize_to_lf(text: str) -> str:
    """Collapse CRLF and lone CR to LF (the model-facing newline)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def apply_eol(text_lf: str, eol: str) -> str:
    """Expand an LF-only string to the given EOL style ('crlf'|'lf'|'cr')."""
    eol_str = EOL_STR[eol]
    if eol_str == "\n":
        return text_lf
    return text_lf.replace("\n", eol_str)


# --------------------------------------------------------------------------------------
# Char -> byte offset map (the basis for byte-faithful splicing)
# --------------------------------------------------------------------------------------


def char_byte_starts(body: bytes, decode_codec: str) -> tuple[str, list[int]]:
    """Decode ``body`` and return ``(text, starts)``.

    ``starts[i]`` is the byte offset in ``body`` where character ``i`` begins; the list has
    a trailing sentinel equal to ``len(body)`` so the byte span of character ``i`` is
    exactly ``body[starts[i]:starts[i+1]]``. Built with an incremental decoder so it works
    for any width-varying codec (GBK 1-2 bytes, GB18030 1-4, UTF-8 1-4, UTF-16 2/4).

    Decoding is strict here: an edit must not proceed on bytes we cannot losslessly map.
    """
    dec = codecs.getincrementaldecoder(decode_codec)("strict")
    starts: list[int] = []
    chars: list[str] = []
    cur = 0  # byte offset where the currently-accumulating character started
    try:
        for i in range(len(body)):
            out = dec.decode(body[i : i + 1])
            if out:
                for ch in out:
                    starts.append(cur)
                    chars.append(ch)
                cur = i + 1
        out = dec.decode(b"", True)
        for ch in out:
            starts.append(cur)
            chars.append(ch)
    except UnicodeDecodeError as exc:
        raise DecodeError(
            f"cannot decode bytes as {decode_codec!r} at byte {exc.start}: {exc.reason}"
        ) from exc
    starts.append(len(body))
    return "".join(chars), starts


# --------------------------------------------------------------------------------------
# Corruption guard helper (write-time replacement-character check, #1)
# --------------------------------------------------------------------------------------

#: U+FFFD. Appears in text when bytes were decoded lossily upstream (the incident's
#: corruption signature). Distinct from any encode failure: gb18030 encodes it happily.
REPLACEMENT_CHAR = "�"


def replacement_char_positions(text: str) -> list[int]:
    """Character indices of every U+FFFD in ``text`` (empty list if clean).

    The write guard uses this to refuse persisting content that carries the replacement
    character, and to report the first offset + total count so the failure is actionable.
    """
    return [i for i, ch in enumerate(text) if ch == REPLACEMENT_CHAR]


# --------------------------------------------------------------------------------------
# Encode with a loud lossy guard
# --------------------------------------------------------------------------------------


def encode_text(text: str, encode_codec: str) -> bytes:
    """Encode ``text`` strictly; on failure raise LossyEncode with the offending char (FR6).

    Never substitutes ``?`` or drops characters. Under the default GB18030 this effectively
    never fires for valid Unicode; under forced strict ``gbk`` it fires for non-GBK chars.
    """
    try:
        return text.encode(encode_codec, "strict")
    except UnicodeEncodeError as exc:
        ch = text[exc.start]
        # byte offset within the encodable prefix, for actionable reporting
        byte_offset = len(text[: exc.start].encode(encode_codec, "strict"))
        raise LossyEncode(
            f"codec {encode_codec!r} cannot represent character {ch!r} "
            f"(U+{ord(ch):04X}) at character index {exc.start} (byte offset {byte_offset}). "
            f"Use encodeCodec='gb18030' to author arbitrary CJK, or remove the character.",
            char=ch,
            char_index=exc.start,
            byte_offset=byte_offset,
        ) from exc


# --------------------------------------------------------------------------------------
# The byte-splice replacement (used by edit_file and apply_edits)
# --------------------------------------------------------------------------------------


def _find_all(haystack: str, needle: str) -> list[int]:
    """Non-overlapping occurrences of ``needle`` in ``haystack`` (start indices)."""
    if not needle:
        return []
    out: list[int] = []
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i == -1:
            return out
        out.append(i)
        start = i + len(needle)


@dataclass
class ReplaceResult:
    new_body: bytes
    replacements: int
    old_view_lf: str   # full pre-edit text, LF-normalized (for diffing)
    new_view_lf: str   # full post-edit text, LF-normalized (for diffing)


def replace_in_body(
    body: bytes,
    *,
    decode_codec: str,
    encode_codec: str,
    eol: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
) -> ReplaceResult:
    """Replace ``old_string`` with ``new_string`` inside ``body`` with byte fidelity.

    The match is performed against the decoded text view (model sees LF), but applied by
    splicing **raw bytes** so every byte outside the replaced span is identical to the
    input (§3 round-trip, FR2). ``new_string`` is EOL-adjusted to the file's convention and
    re-encoded with ``encode_codec``; nothing else is re-encoded.

    Raises MatchNotFound / AmbiguousMatch / LossyEncode / DecodeError.
    """
    text, starts = char_byte_starts(body, decode_codec)

    old_lf = normalize_to_lf(old_string)
    new_lf = normalize_to_lf(new_string)
    old_needle = apply_eol(old_lf, eol)
    new_repl_text = apply_eol(new_lf, eol)

    matches = _find_all(text, old_needle)
    # Fallback: if the EOL-adjusted needle is absent but the LF form is present (e.g. a
    # file with mixed/atypical endings), match on LF so the model isn't surprised.
    used_lf_fallback = False
    if not matches and old_needle != old_lf:
        matches = _find_all(text, old_lf)
        if matches:
            used_lf_fallback = True
            new_repl_text = new_lf

    if not matches:
        raise MatchNotFound(
            "old_string not found in file. Ensure it matches exactly (whitespace included); "
            "the file view uses LF newlines."
        )
    if len(matches) > 1 and not replace_all:
        raise AmbiguousMatch(
            f"old_string matched {len(matches)} times; pass replace_all=true to replace all, "
            f"or extend old_string to be unique.",
            count=len(matches),
        )

    repl_bytes = encode_text(new_repl_text, encode_codec)
    needle_for_view = old_lf if used_lf_fallback else old_needle

    # Splice raw bytes left-to-right.
    out = bytearray()
    prev_byte = 0
    n_chars = len(old_needle if not used_lf_fallback else old_lf)
    for a in matches:
        b = a + n_chars
        byte_start = starts[a]
        byte_end = starts[b]
        out += body[prev_byte:byte_start]
        out += repl_bytes
        prev_byte = byte_end
    out += body[prev_byte:]

    # LF views for diffing (decode the spliced body the same way we decoded the input).
    new_text = decode_body(bytes(out), decode_codec)
    return ReplaceResult(
        new_body=bytes(out),
        replacements=len(matches),
        old_view_lf=normalize_to_lf(text),
        new_view_lf=normalize_to_lf(new_text),
    )
