"""Acceptance tests for the line-addressed editor `replace_lines`.

`replace_lines` covers replace / delete / insert by line number, single or batched, closing the
reported gap (exact-string `edit_file` is fragile for whitespace-heavy / large / non-contiguous
spans). Encoding: an inclusive range `start_line..end_line` + `new_string`; delete = empty text;
insert before `start_line` = `end_line == start_line - 1`.
"""

from __future__ import annotations

import pytest

from gbk_fs.errors import Conflict, InvalidArguments, LossyEncode, NotFound, ReplacementChar
from conftest import CHINESE_COMMENT, CODE, write_gbk_c, write_raw


def _content_lines(res):
    """The text of each numbered line from a read_file result."""
    return [l.split("\t", 1)[1] for l in res["content"].splitlines()]


# --------------------------------------------------------------------- single edit: replace
def test_replace_keeps_other_lines_byte_identical(fs, root):
    """Replacing line 2 leaves the Chinese lines 1 and 3 byte-for-byte unchanged."""
    write_gbk_c(root / "sync.c")  # 3 GBK lines, CRLF, final newline
    fs.read_file("sync.c")
    fs.replace_lines("sync.c", 2, 2, "int x;")

    new_raw = (root / "sync.c").read_bytes()
    parts = new_raw.split(b"\r\n")
    assert parts[0] == CHINESE_COMMENT.encode("gbk")          # untouched, exact bytes
    assert parts[1] == b"int x;"                              # replaced
    assert parts[2] == "// end 结束".encode("gbk")            # untouched, exact bytes
    assert parts[3] == b""                                    # final newline preserved
    assert "同步 VRF 路由信息" in new_raw.decode("gbk")        # still clean GBK


def test_replace_reports_counts(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    r = fs.replace_lines("sync.c", 2, 2, "int x;")
    assert r["num_edits"] == 1
    assert r["lines_removed"] == 1 and r["lines_added"] == 1
    assert r["old_line_count"] == 3 and r["new_line_count"] == 3
    assert r["encoding"] == "gbk" and r["eol"] == "crlf"


def test_replace_one_line_with_many(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    fs.replace_lines("sync.c", 2, 2, "lineA\nlineB")
    res = fs.read_file("sync.c")
    assert res["line_count"] == 4
    assert _content_lines(res) == [CHINESE_COMMENT, "lineA", "lineB", "// end 结束"]


def test_replace_authors_new_cjk(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    fs.replace_lines("sync.c", 1, 1, "// 新增 𠀋")  # Extension-B hanzi, not in plain GBK
    assert "新增 𠀋" in fs.read_file("sync.c")["content"]


# --------------------------------------------------------------------- single edit: delete
def test_delete_via_empty_new_string(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    r = fs.replace_lines("sync.c", 1, 1, "")
    assert r["lines_removed"] == 1 and r["lines_added"] == 0
    res = fs.read_file("sync.c")
    assert res["line_count"] == 2
    assert res["content"].splitlines()[0].endswith(CODE)


# --------------------------------------------------------------------- single edit: insert
def test_insert_before_line_via_zero_width_range(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    fs.replace_lines("sync.c", 2, 1, "// 插入")  # end_line = start_line - 1 -> insert before 2
    assert _content_lines(fs.read_file("sync.c")) == \
        [CHINESE_COMMENT, "// 插入", CODE, "// end 结束"]


def test_insert_append_at_eof(fs, root):
    write_gbk_c(root / "sync.c")  # 3 lines, final newline
    fs.read_file("sync.c")
    fs.replace_lines("sync.c", 4, 3, "// tail")  # start_line = line_count + 1 -> append
    res = fs.read_file("sync.c")
    assert res["line_count"] == 4
    assert res["content"].splitlines()[-1].endswith("// tail")
    assert (root / "sync.c").read_bytes().endswith(b"// tail\r\n")  # final newline kept


def test_insert_append_preserves_no_final_newline(fs, root):
    write_raw(root / "x.c", "a\r\nb\r\nc".encode("gbk"))  # no trailing newline
    fs.read_file("x.c")
    fs.replace_lines("x.c", 4, 3, "d")  # append after the unterminated last line
    assert (root / "x.c").read_bytes() == "a\r\nb\r\nc\r\nd".encode("gbk")


# --------------------------------------------------------------------- fidelity edge cases
def test_replace_last_line_preserves_no_final_newline(fs, root):
    write_raw(root / "x.c", "a\r\nb\r\nc".encode("gbk"))
    fs.read_file("x.c")
    fs.replace_lines("x.c", 3, 3, "C")
    assert (root / "x.c").read_bytes() == "a\r\nb\r\nC".encode("gbk")


def test_eol_preserved_on_replace(fs, root):
    write_gbk_c(root / "sync.c", eol="\r\n")
    fs.read_file("sync.c")
    fs.replace_lines("sync.c", 2, 2, "int y;")
    new_raw = (root / "sync.c").read_bytes()
    assert b"\r\n" in new_raw
    assert new_raw.replace(b"\r\n", b"").find(b"\n") == -1  # no lone LF introduced


def test_bom_preserved_on_replace(fs, root):
    import codecs

    raw = codecs.BOM_UTF8 + "hello\r\nworld\r\n".encode("utf-8")
    write_raw(root / "notes.md", raw)
    fs.read_file("notes.md")
    fs.replace_lines("notes.md", 2, 2, "there")
    new_raw = (root / "notes.md").read_bytes()
    assert new_raw.startswith(codecs.BOM_UTF8)
    assert new_raw[len(codecs.BOM_UTF8):].decode("utf-8") == "hello\r\nthere\r\n"


# --------------------------------------------------------------------- batch (multi-edit)
def _write_gbk_lines(path, lines):
    raw = ("\r\n".join(lines) + "\r\n").encode("gbk")
    write_raw(path, raw)
    return raw


def test_batch_non_contiguous_deletes_use_original_numbers(fs, root):
    """Headline use case: remove several non-contiguous spans in ONE call, line numbers all
    referring to the original file (no bottom-to-top juggling), with byte fidelity."""
    _write_gbk_lines(root / "m.c", ["// 第一行", "code1();", "// 第二行", "code2();", "// 第三行"])
    fs.read_file("m.c")
    r = fs.replace_lines("m.c", edits=[
        {"start_line": 2, "end_line": 2, "new_string": ""},   # delete code1
        {"start_line": 4, "end_line": 4, "new_string": ""},   # delete code2 (original number)
    ])
    assert r["num_edits"] == 2 and r["lines_removed"] == 2 and r["lines_added"] == 0

    new_raw = (root / "m.c").read_bytes()
    parts = new_raw.split(b"\r\n")
    assert parts[0] == "// 第一行".encode("gbk")   # untouched Chinese lines: exact bytes
    assert parts[1] == "// 第二行".encode("gbk")
    assert parts[2] == "// 第三行".encode("gbk")
    assert parts[3] == b""
    assert _content_lines(fs.read_file("m.c")) == ["// 第一行", "// 第二行", "// 第三行"]


def test_batch_mixed_replace_delete_insert(fs, root):
    write_gbk_c(root / "sync.c")  # [CHINESE_COMMENT, CODE, "// end 结束"]
    fs.read_file("sync.c")
    r = fs.replace_lines("sync.c", edits=[
        {"start_line": 1, "end_line": 0, "new_string": "// top"},  # insert before line 1
        {"start_line": 2, "end_line": 2, "new_string": "X"},       # replace line 2
        {"start_line": 3, "end_line": 3, "new_string": ""},        # delete line 3
    ])
    assert r["num_edits"] == 3
    assert _content_lines(fs.read_file("sync.c")) == ["// top", CHINESE_COMMENT, "X"]


def test_batch_is_atomic_on_bad_edit(fs, root):
    """One out-of-range edit rejects the whole batch; the file is left byte-for-byte unchanged."""
    original = write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(InvalidArguments):
        fs.replace_lines("sync.c", edits=[
            {"start_line": 1, "end_line": 1, "new_string": "ok"},
            {"start_line": 99, "end_line": 99, "new_string": "bad"},  # past EOF
        ])
    assert (root / "sync.c").read_bytes() == original


def test_batch_rejects_overlapping_ranges(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(InvalidArguments):
        fs.replace_lines("sync.c", edits=[
            {"start_line": 1, "end_line": 2, "new_string": "a"},
            {"start_line": 2, "end_line": 3, "new_string": "b"},  # shares line 2
        ])


def test_batch_rejects_insert_inside_removed_range(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(InvalidArguments):
        fs.replace_lines("sync.c", edits=[
            {"start_line": 1, "end_line": 3, "new_string": "R"},   # removes lines 1..3
            {"start_line": 2, "end_line": 1, "new_string": "I"},   # insert before line 2 (inside)
        ])


def test_batch_insert_before_a_replaced_line(fs, root):
    """An insert sharing the start boundary of a replace is allowed and lands first."""
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    fs.replace_lines("sync.c", edits=[
        {"start_line": 2, "end_line": 1, "new_string": "// before"},  # insert before line 2
        {"start_line": 2, "end_line": 2, "new_string": "X"},          # replace line 2
    ])
    assert _content_lines(fs.read_file("sync.c")) == \
        [CHINESE_COMMENT, "// before", "X", "// end 结束"]


# --------------------------------------------------------------------- argument validation
def test_range_validation(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(InvalidArguments):
        fs.replace_lines("sync.c", 2, 5, "x")     # end past EOF
    with pytest.raises(InvalidArguments):
        fs.replace_lines("sync.c", 0, 1, "x")     # start < 1
    with pytest.raises(InvalidArguments):
        fs.replace_lines("sync.c", 5, 3, "x")     # negative-width range (end < start-1)
    with pytest.raises(InvalidArguments):
        fs.replace_lines("sync.c", 5, 4, "x")     # insert position past line_count + 1


def test_positional_and_batch_are_mutually_exclusive(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(InvalidArguments):
        fs.replace_lines("sync.c", 1, 1, "x", edits=[{"start_line": 2, "end_line": 2, "new_string": "y"}])


def test_missing_args(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(InvalidArguments):
        fs.replace_lines("sync.c", 1, 1)  # no new_string, no edits


def test_missing_file(fs):
    with pytest.raises(NotFound):
        fs.replace_lines("nope.c", 1, 1, "x")


# --------------------------------------------------------------------- safety guards
def test_conflict_on_stale_baseline(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")                      # records session baseline hash
    write_raw(root / "sync.c", b"changed\r\n")  # mutate behind our back
    with pytest.raises(Conflict):
        fs.replace_lines("sync.c", 1, 1, "x")


def test_replacement_char_guard(fs, root):
    write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(ReplacementChar):
        fs.replace_lines("sync.c", 2, 2, "bad � char")
    # explicit override is honoured
    fs.replace_lines("sync.c", 2, 2, "ok � char", allow_replacement_chars=True)
    assert "�" in fs.read_file("sync.c")["content"]


def test_strict_gbk_lossy_guard(root):
    from gbk_fs import GbkFs, load_config

    fs = GbkFs(load_config(root=root, overrides={"encodeCodec": "gbk"}))
    original = write_gbk_c(root / "sync.c")
    fs.read_file("sync.c")
    with pytest.raises(LossyEncode):
        fs.replace_lines("sync.c", 2, 2, "// 😀")
    assert (root / "sync.c").read_bytes() == original  # untouched on failure
