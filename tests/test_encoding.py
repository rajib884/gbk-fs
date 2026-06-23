"""Unit tests for the pure encoding core (§3, §3.1)."""

from __future__ import annotations

import pytest

from gbk_fs import encoding as enc
from gbk_fs.errors import AmbiguousMatch, LossyEncode, MatchNotFound


def test_gb18030_is_byte_identical_superset_of_gbk():
    # The core justification for encodeCodec=gb18030 (§3.1): identical bytes for GBK chars.
    s = "同步 VRF 路由信息 abc 123"
    assert s.encode("gbk") == s.encode("gb18030")


def test_detect_utf8_vs_gbk():
    gbk_bytes = "中文注释".encode("gbk")
    utf8_bytes = "中文注释".encode("utf-8")

    d_gbk = enc.detect_encoding(gbk_bytes, default_encoding="gbk")
    assert d_gbk.logical == "gbk"
    assert d_gbk.decode_codec == "gb18030"  # superset used for decoding
    assert d_gbk.bom == b""

    d_utf8 = enc.detect_encoding(utf8_bytes, default_encoding="gbk")
    assert d_utf8.logical == "utf-8"


def test_detect_bom_utf8_and_utf16():
    import codecs

    d = enc.detect_encoding(codecs.BOM_UTF8 + "x".encode("utf-8"), default_encoding="gbk")
    assert d.logical == "utf-8-sig" and d.has_bom

    d16 = enc.detect_encoding(codecs.BOM_UTF16_LE + "x".encode("utf-16-le"), default_encoding="gbk")
    assert d16.logical == "utf-16-le" and d16.has_bom


def test_explicit_and_rule_precedence_over_auto():
    gbk_bytes = "中文".encode("gbk")
    # explicit wins
    assert enc.detect_encoding(gbk_bytes, explicit="utf-8", default_encoding="gbk").logical == "utf-8"
    # rule wins over auto
    assert enc.detect_encoding(gbk_bytes, rule_encoding="gbk").logical == "gbk"


def test_char_byte_starts_maps_variable_width():
    body = "ab中c".encode("gb18030")  # a,b = 1 byte; 中 = 2 bytes; c = 1 byte
    text, starts = enc.char_byte_starts(body, "gb18030")
    assert text == "ab中c"
    assert starts == [0, 1, 2, 4, 5]  # last is sentinel == len(body)


def test_eol_detection_and_apply():
    assert enc.detect_eol("a\r\nb\r\n") == "crlf"
    assert enc.detect_eol("a\nb\n") == "lf"
    assert enc.detect_eol("no newline") is None
    assert enc.apply_eol("a\nb", "crlf") == "a\r\nb"
    assert enc.apply_eol("a\nb", "lf") == "a\nb"


def test_replace_preserves_surrounding_bytes_byte_for_byte():
    body = "// 中文注释\r\nTOKEN_OLD here\r\n// 更多中文\r\n".encode("gbk")
    res = enc.replace_in_body(
        body, decode_codec="gb18030", encode_codec="gb18030", eol="crlf",
        old_string="TOKEN_OLD", new_string="TOKEN_NEW", replace_all=False,
    )
    # ASCII token => 1:1 byte replacement; everything else identical.
    assert res.new_body == body.replace(b"TOKEN_OLD", b"TOKEN_NEW")
    assert res.replacements == 1


def test_replace_authors_new_cjk_with_gb18030():
    body = "int x; // anchor\r\n".encode("gbk")
    new_comment = "// anchor 新增的中文注释 𠀋"  # includes an Extension-B rare hanzi
    res = enc.replace_in_body(
        body, decode_codec="gb18030", encode_codec="gb18030", eol="crlf",
        old_string="// anchor", new_string=new_comment, replace_all=False,
    )
    # Round-trips back to the authored characters.
    assert "新增的中文注释 𠀋" in res.new_body.decode("gb18030")


def test_lossy_guard_under_strict_gbk():
    with pytest.raises(LossyEncode) as ei:
        enc.encode_text("emoji 😀", "gbk")
    assert ei.value.char == "😀"
    assert ei.value.char_index == len("emoji ")


def test_not_found_and_ambiguous():
    body = b"aaa bbb aaa"
    with pytest.raises(MatchNotFound):
        enc.replace_in_body(body, decode_codec="utf-8", encode_codec="utf-8", eol="lf",
                            old_string="zzz", new_string="q", replace_all=False)
    with pytest.raises(AmbiguousMatch):
        enc.replace_in_body(body, decode_codec="utf-8", encode_codec="utf-8", eol="lf",
                            old_string="aaa", new_string="q", replace_all=False)
    # replace_all resolves ambiguity
    res = enc.replace_in_body(body, decode_codec="utf-8", encode_codec="utf-8", eol="lf",
                              old_string="aaa", new_string="q", replace_all=True)
    assert res.replacements == 2 and res.new_body == b"q bbb q"
