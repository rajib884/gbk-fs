"""U+FFFD write guard (#1): refuse persisting the corruption signature.

The incident this guards against: a GBK file was read as UTF-8 and saved back, turning every
Chinese character into U+FFFD. The default gb18030 codec encodes U+FFFD happily (to bytes
84 31 a4 37), so the generic LossyEncode guard never catches it — these tests cover the
dedicated, codec-independent check.
"""

from __future__ import annotations

import pytest

from gbk_fs.errors import ReplacementChar
from conftest import write_gbk_c

FFFD = "�"  # U+FFFD REPLACEMENT CHARACTER


def test_write_refuses_replacement_char(fs, root):
    with pytest.raises(ReplacementChar) as ei:
        fs.write_file("new.c", f"int a; // {FFFD}{FFFD}\n")
    assert ei.value.count == 2
    assert ei.value.first_index >= 0
    assert not (root / "new.c").exists()  # nothing was written


def test_write_allows_replacement_char_with_flag(fs, root):
    r = fs.write_file("new.c", f"// {FFFD}\n", allow_replacement_chars=True)
    assert r["created"]
    assert FFFD in fs.read_file("new.c")["content"]  # escape hatch genuinely persists it


def test_edit_refuses_replacement_char_in_new_string(fs, root):
    original = write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(ReplacementChar):
        fs.edit_file("sync.c", "return 0;", f"return 0; // {FFFD}")
    assert (root / "sync.c").read_bytes() == original  # untouched


def test_edit_allows_replacement_char_with_flag(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    fs.edit_file("sync.c", "return 0;", f"return 0; // {FFFD}", allow_replacement_chars=True)
    assert FFFD in fs.read_file("sync.c")["content"]


def test_guard_checks_only_incoming_content_not_existing_file(fs, root):
    """Recovery case: an already-corrupted file can still be edited with clean text.

    The guard inspects only new_string, so editing an untouched ASCII region of a file that
    is full of U+FFFD must succeed (and must not disturb the pre-existing corruption).
    """
    corrupt = ("// " + FFFD * 3 + "\r\nint x; // keep\r\n").encode("gb18030")
    (root / "bad.c").write_bytes(corrupt)
    fs.read_file("bad.c")
    fs.edit_file("bad.c", "int x;", "int y;")
    out = fs.read_file("bad.c")["content"]
    assert "int y;" in out
    assert FFFD in out  # the pre-existing corruption is left exactly as it was


def test_apply_edits_atomic_aborts_on_replacement_char(fs, root):
    write_gbk_c(root / "a.c")
    write_gbk_c(root / "b.c")
    original_a = (root / "a.c").read_bytes()
    original_b = (root / "b.c").read_bytes()
    fs.read_file("a.c")
    fs.read_file("b.c")
    with pytest.raises(ReplacementChar):
        fs.apply_edits(
            [
                {"path": "a.c", "old_string": "return 0;", "new_string": "return 1;"},
                {"path": "b.c", "old_string": "return 0;", "new_string": f"return {FFFD}"},
            ],
            atomic=True,
        )
    # all-or-nothing: the guard fires during the no-write staging phase, so neither changed
    assert (root / "a.c").read_bytes() == original_a
    assert (root / "b.c").read_bytes() == original_b


def test_apply_edits_per_edit_flag_allows(fs, root):
    write_gbk_c(root / "a.c")
    fs.read_file("a.c")
    res = fs.apply_edits(
        [
            {"path": "a.c", "old_string": "return 0;", "new_string": f"return 0; // {FFFD}",
             "allow_replacement_chars": True},
        ],
        atomic=True,
    )
    assert res["ok"]
    assert FFFD in fs.read_file("a.c")["content"]
