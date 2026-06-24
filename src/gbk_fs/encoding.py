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

from .errors import AmbiguousMatch, DecodeError, InvalidArguments, LossyEncode, MatchNotFound

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


# --------------------------------------------------------------------------------------
# Line-addressed byte splice (used by replace_lines: replace / delete / insert, single or batch)
# --------------------------------------------------------------------------------------


def line_spans(text: str) -> list[tuple[int, int, int]]:
    """Per-line ``(content_start, content_end, term_end)`` char offsets in ``text``.

    Line boundaries follow the model's ``cat -n`` numbering: CRLF, a lone CR and a lone LF
    each count as exactly one terminator (matching :func:`normalize_to_lf`). A trailing line
    with no terminator is included; a final terminator does *not* create a phantom empty
    line. The number of spans therefore equals ``read_file``'s reported ``line_count``, so a
    1-based line number means the same thing here as it does to the model.
    """
    spans: list[tuple[int, int, int]] = []
    n = len(text)
    line_start = 0
    i = 0
    while i < n:
        ch = text[i]
        if ch == "\n":
            spans.append((line_start, i, i + 1))
            line_start = i + 1
            i += 1
        elif ch == "\r":
            term_end = i + 2 if (i + 1 < n and text[i + 1] == "\n") else i + 1
            spans.append((line_start, i, term_end))
            line_start = term_end
            i = term_end
        else:
            i += 1
    if line_start < n:  # trailing line with no terminator
        spans.append((line_start, n, n))
    return spans


@dataclass
class LineEditResult:
    new_body: bytes
    old_view_lf: str        # full pre-edit text, LF-normalized (for diffing)
    new_view_lf: str        # full post-edit text, LF-normalized (for diffing)
    old_line_count: int
    new_line_count: int
    lines_removed: int      # existing lines the edits replaced/deleted (summed)
    lines_added: int        # lines contributed by new_string(s) (summed)
    num_edits: int          # how many edits were applied in this call


def _compute_line_edit(
    text: str,
    spans: list[tuple[int, int, int]],
    line_count: int,
    text_len: int,
    final_nl: bool,
    eol_str: str,
    *,
    start_line: int,
    count: int,
    new_string: str,
) -> tuple[int, int, str, int]:
    """Resolve one line edit to ``(remove_start_char, remove_end_char, block, lines_added)``.

    ``[remove_start, remove_end)`` is the half-open char span this edit overwrites in the
    *original* text (``remove_end == remove_start`` for an insert). ``block`` is the EOL-adjusted
    replacement text. Pure / positional; raises InvalidArguments on an out-of-range address.
    """
    if count < 0:
        raise InvalidArguments("count must be >= 0")
    if count == 0:
        if not (1 <= start_line <= line_count + 1):
            raise InvalidArguments(
                f"insert position {start_line} is out of range; file has {line_count} "
                f"line(s) (valid insert positions are 1..{line_count + 1})"
            )
    else:
        end_line = start_line + count - 1
        if start_line < 1 or end_line > line_count:
            raise InvalidArguments(
                f"line range {start_line}..{end_line} is out of range; file has "
                f"{line_count} line(s)"
            )

    def content_start(line: int) -> int:
        # 1-based; line == line_count + 1 maps to end-of-text (the append point).
        return spans[line - 1][0] if line <= line_count else text_len

    remove_start = content_start(start_line)
    remove_end = content_start(start_line + count)

    prefix = text[:remove_start]
    suffix = text[remove_end:]

    payload_lf = normalize_to_lf(new_string)
    if payload_lf.endswith("\n"):
        payload_lf = payload_lf[:-1]  # one trailing newline is cosmetic; lines define count
    payload_lines = payload_lf.split("\n") if payload_lf != "" else []

    if payload_lines:
        block = eol_str.join(payload_lines)
        # Keep a trailing terminator when content follows, or when the file ended with one.
        if suffix or final_nl:
            block += eol_str
        # Appending after an unterminated final line needs a separator in front.
        if not suffix and prefix and not prefix.endswith(("\n", "\r")):
            block = eol_str + block
    else:
        block = ""

    return remove_start, remove_end, block, len(payload_lines)


def edit_lines_in_body(
    body: bytes,
    *,
    decode_codec: str,
    encode_codec: str,
    eol: str,
    start_line: int,
    count: int,
    new_string: str,
) -> LineEditResult:
    """Single-edit convenience wrapper over :func:`edit_lines_multi_in_body`.

    ``count >= 1`` replaces lines ``start_line .. start_line+count-1`` inclusive (empty
    ``new_string`` deletes them); ``count == 0`` inserts before ``start_line``.
    """
    return edit_lines_multi_in_body(
        body, decode_codec=decode_codec, encode_codec=encode_codec, eol=eol,
        edits=[(start_line, count, new_string)],
    )


def edit_lines_multi_in_body(
    body: bytes,
    *,
    decode_codec: str,
    encode_codec: str,
    eol: str,
    edits: list[tuple[int, int, str]],
) -> LineEditResult:
    """Apply several line edits to ``body`` in one byte-faithful splice.

    Each edit is ``(start_line, count, new_string)`` with 1-based line numbers addressing the
    **original** file — so callers never have to compensate for line-number drift between edits.
    ``count >= 1`` replaces an inclusive line range (empty ``new_string`` deletes it); ``count
    == 0`` inserts before ``start_line`` (use ``line_count + 1`` to append at EOF).

    Fidelity matches :func:`replace_in_body`: line offsets are located in the decoded view, then
    raw bytes are spliced, so every byte outside an edited span is preserved and only the
    ``new_string``s are re-encoded. EOL convention and the file's final-newline state are kept.

    Edits must not overlap (two replace/delete ranges sharing a line, or an insert landing
    *inside* a replaced range, is rejected). Adjacency and a shared boundary are allowed; edits
    that resolve to the same position are emitted in input order.

    Raises InvalidArguments (range / overlap), LossyEncode, DecodeError.
    """
    if not edits:
        raise InvalidArguments("edit_lines_multi_in_body needs at least one edit")

    text, starts = char_byte_starts(body, decode_codec)
    spans = line_spans(text)
    line_count = len(spans)
    text_len = len(text)
    final_nl = has_final_newline(text)
    eol_str = EOL_STR[eol]

    # Resolve every edit to a char span + replacement block (validates ranges).
    resolved: list[tuple[int, int, str, int, int]] = []  # rs, re, block, count, idx
    lines_removed = 0
    lines_added = 0
    for idx, (start_line, count, new_string) in enumerate(edits):
        rs, re, block, added = _compute_line_edit(
            text, spans, line_count, text_len, final_nl, eol_str,
            start_line=start_line, count=count, new_string=new_string,
        )
        resolved.append((rs, re, block, count, idx))
        lines_removed += count
        lines_added += added

    # Detect conflicts (order-independent). Two removed (positive-width) ranges may not overlap;
    # an insert point may not fall *strictly inside* a removed range. Sharing a boundary is fine.
    positives = [(rs, re, idx) for rs, re, _b, count, idx in resolved if count > 0]
    max_re = 0
    for rs, re, idx in sorted(positives):
        if rs < max_re:
            raise InvalidArguments(
                f"edit #{idx} overlaps another edit's line range; edits in one call "
                f"must not overlap"
            )
        max_re = max(max_re, re)
    for rs, _re, _b, count, idx in resolved:
        if count == 0 and any(p < rs < q for p, q, _i in positives):
            raise InvalidArguments(
                f"edit #{idx} inserts inside a range removed by another edit; edits in "
                f"one call must not overlap"
            )

    # Splice once, left-to-right, copying untouched bytes verbatim. Sorting by (rs, re, idx)
    # keeps prev_byte monotonic and places a zero-width insert before a removal that starts at
    # the same point (i.e. "insert before line L" precedes a replacement of line L).
    out = bytearray()
    prev_byte = 0
    for rs, re, block, _count, _idx in sorted(resolved, key=lambda r: (r[0], r[1], r[4])):
        out += body[prev_byte : starts[rs]]
        out += encode_text(block, encode_codec)
        prev_byte = starts[re]
    out += body[prev_byte:]
    out = bytes(out)

    new_text = decode_body(out, decode_codec)
    return LineEditResult(
        new_body=out,
        old_view_lf=normalize_to_lf(text),
        new_view_lf=normalize_to_lf(new_text),
        old_line_count=line_count,
        new_line_count=len(line_spans(new_text)),
        lines_removed=lines_removed,
        lines_added=lines_added,
        num_edits=len(edits),
    )
