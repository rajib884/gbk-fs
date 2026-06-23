"""Acceptance tests for read/write/edit (§9 tests 1-3a, 5, FR1, FR2, FR6, FR6a, FR8)."""

from __future__ import annotations

import codecs

import pytest

from gbk_fs.errors import LossyEncode, NotFound, UnreadOverwrite
from conftest import CHINESE_COMMENT, write_gbk_c, write_raw


def test_read_fidelity_chinese_comments(fs, root):
    write_gbk_c(root / "sync.c")
    res = fs.read_file("sync.c")
    assert res["detected_encoding"] == "gbk"
    assert res["eol"] == "crlf"
    assert "同步 VRF 路由信息" in res["content"]
    # cat -n style line numbers, parity with native Read
    assert res["content"].splitlines()[0].startswith("     1\t")
    assert res["line_count"] == 3


def test_read_offset_limit(fs, root):
    write_gbk_c(root / "sync.c")
    res = fs.read_file("sync.c", offset=2, limit=1)
    lines = res["content"].splitlines()
    assert len(lines) == 1 and lines[0].startswith("     2\t")
    assert "nsm_vrf_sync_gr_info" in lines[0]


def test_edit_round_trip_only_changed_bytes(fs, root):
    """§9.2: edit one English token; the rest of the file is byte-identical (no churn)."""
    original = write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")  # mark read (also realistic)
    fs.edit_file("sync.c", "nsm_vrf_sync_gr_info", "nsm_vrf_sync_gr_info_v2")

    new_raw = (root / "sync.c").read_bytes()
    # ASCII token => exact 1:1 byte replacement; Chinese comment bytes untouched.
    assert new_raw == original.replace(b"nsm_vrf_sync_gr_info", b"nsm_vrf_sync_gr_info_v2")
    # And the file still decodes cleanly with an external GBK reader (no corruption).
    assert "同步 VRF 路由信息" in new_raw.decode("gbk")


def test_cjk_authoring_default_gb18030(fs, root):
    """§9.3: insert a brand-new Chinese comment with rare hanzi; existing bytes stay identical."""
    original = write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    new_text = CHINESE_COMMENT + " 新增注释 𠀋"  # adds Extension-B hanzi (not in plain GBK)
    fs.edit_file("sync.c", CHINESE_COMMENT, new_text)

    new_raw = (root / "sync.c").read_bytes()
    # Re-read returns exactly the authored characters.
    assert "新增注释 𠀋" in fs.read_file("sync.c")["content"]
    # Untouched region (everything after the first line) is byte-identical.
    tail_original = original.split(b"\r\n", 1)[1]
    tail_new = new_raw.split(b"\r\n", 1)[1]
    assert tail_original == tail_new


def test_strict_gbk_lossy_guard(root):
    """§9.3a: with encodeCodec=gbk, authoring a non-GBK char fails loudly; file unchanged."""
    from gbk_fs import GbkFs, load_config

    fs = GbkFs(load_config(root=root, overrides={"encodeCodec": "gbk"}))
    original = write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(LossyEncode) as ei:
        fs.edit_file("sync.c", "return 0;", "return 0; // 😀")
    assert ei.value.char == "😀"
    assert (root / "sync.c").read_bytes() == original  # untouched


def test_eol_preserved_on_edit(fs, root):
    original = write_gbk_c(root / "sync.c", eol="\r\n")
    fs.read_file("sync.c")
    fs.edit_file("sync.c", "return 0;", "return 1;")
    new_raw = (root / "sync.c").read_bytes()
    assert b"\r\n" in new_raw
    # no lone LF was introduced
    assert new_raw.replace(b"\r\n", b"").find(b"\n") == -1


def test_bom_preserved_on_edit(fs, root):
    raw = codecs.BOM_UTF8 + "hello world\r\n".encode("utf-8")
    write_raw(root / "notes.md", raw)
    fs.read_file("notes.md")
    fs.edit_file("notes.md", "world", "there")
    new_raw = (root / "notes.md").read_bytes()
    assert new_raw.startswith(codecs.BOM_UTF8)
    assert new_raw[len(codecs.BOM_UTF8):].decode("utf-8") == "hello there\r\n"


def test_final_newline_preserved(fs, root):
    # File without trailing newline stays without one after an edit.
    raw = "abc def".encode("gbk")
    write_raw(root / "x.c", raw)
    fs.read_file("x.c")
    fs.edit_file("x.c", "def", "ghi")
    assert (root / "x.c").read_bytes() == b"abc ghi"


def test_write_creates_with_configured_encoding(fs, root):
    # New .c file -> rule says gbk; content with Chinese encodes via gb18030 (superset).
    r = fs.write_file("new.c", "// 新文件\nint a;\n")
    assert r["encoding"] == "gbk"
    assert r["eol"] == "crlf"  # default
    raw = (root / "new.c").read_bytes()
    assert raw == "// 新文件\r\nint a;\r\n".encode("gb18030")


def test_write_refuses_unread_overwrite(fs, root):
    write_gbk_c(root / "sync.c")
    with pytest.raises(UnreadOverwrite):
        fs.write_file("sync.c", "overwrite")
    # allowed after reading
    fs.read_file("sync.c")
    fs.write_file("sync.c", "// 覆盖\n")
    # or with the explicit flag on a fresh instance
    from gbk_fs import GbkFs, load_config

    fs2 = GbkFs(load_config(root=root))
    fs2.write_file("sync.c", "// 再次覆盖\n", allow_overwrite_unread=True)


def test_read_missing_file(fs):
    with pytest.raises(NotFound):
        fs.read_file("nope.c")
