"""Concurrency & batch tests (§9.8-12, CR1-CR8)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from gbk_fs.errors import Conflict, MatchNotFound
from conftest import write_gbk_c


def test_parallel_reads_all_succeed(fs, root):
    for i in range(20):
        write_gbk_c(root / f"f{i}.c")
    res = fs.read_files([{"path": f"f{i}.c"} for i in range(20)])
    assert res["count"] == 20
    assert all(r["ok"] for r in res["results"])
    assert all("同步 VRF 路由信息" in r["content"] for r in res["results"])


def test_read_files_partial_failure_does_not_abort(fs, root):
    write_gbk_c(root / "ok.c")
    res = fs.read_files([{"path": "ok.c"}, {"path": "missing.c"}])
    by_ok = {r["ok"] for r in res["results"]}
    assert by_ok == {True, False}
    bad = [r for r in res["results"] if not r["ok"]][0]
    assert bad["code"] == "NOT_FOUND"


def test_independent_writes_concurrent(fs, root):
    for i in range(8):
        write_gbk_c(root / f"f{i}.c")
        fs.read_file(f"f{i}.c")

    def do(i):
        return fs.edit_file(f"f{i}.c", "return 0;", f"return {i + 100};")

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(do, range(8)))

    for i in range(8):
        assert f"return {i + 100};" in (root / f"f{i}.c").read_bytes().decode("gb18030")
        assert "同步 VRF 路由信息" in (root / f"f{i}.c").read_bytes().decode("gb18030")


def test_same_file_serialization(fs, root):
    # Two non-overlapping edits to the same file from two threads: both apply, no corruption.
    raw = "// 中文\r\nAAA BBB\r\n".encode("gbk")
    (root / "x.c").write_bytes(raw)
    fs.read_file("x.c")

    def edit_a():
        return fs.edit_file("x.c", "AAA", "A1")

    def edit_b():
        return fs.edit_file("x.c", "BBB", "B1")

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(edit_a)
        f2 = ex.submit(edit_b)
        f1.result(); f2.result()

    text = (root / "x.c").read_bytes().decode("gb18030")
    assert "A1" in text and "B1" in text
    assert "中文" in text  # comment intact, file not torn


def test_conflict_detection_with_stale_hash(fs, root):
    write_gbk_c(root / "x.c")
    snap = fs.read_file("x.c")
    stale_hash = snap["sha256"]
    # someone changes the file underneath us
    (root / "x.c").write_bytes("// 改动\r\nreturn 0;\r\n".encode("gbk"))
    before = (root / "x.c").read_bytes()
    with pytest.raises(Conflict):
        fs.edit_file("x.c", "return 0;", "return 9;", expected_hash=stale_hash)
    assert (root / "x.c").read_bytes() == before  # unchanged


def test_atomic_batch_rolls_back_on_failure(fs, root):
    a = write_gbk_c(root / "a.c")
    b = write_gbk_c(root / "b.c")
    fs.read_file("a.c"); fs.read_file("b.c")
    edits = [
        {"path": "a.c", "old_string": "return 0;", "new_string": "return 1;"},
        {"path": "b.c", "old_string": "return 0;", "new_string": "return 2;"},
        {"path": "a.c", "old_string": "DOES_NOT_EXIST", "new_string": "x"},  # fails
    ]
    with pytest.raises(MatchNotFound):
        fs.apply_edits(edits, atomic=True)
    # Nothing written: both files byte-identical to originals.
    assert (root / "a.c").read_bytes() == a
    assert (root / "b.c").read_bytes() == b


def test_atomic_batch_commits_all_on_success(fs, root):
    write_gbk_c(root / "a.c")
    write_gbk_c(root / "b.c")
    fs.read_file("a.c"); fs.read_file("b.c")
    edits = [
        {"path": "a.c", "old_string": "return 0;", "new_string": "return 1;"},
        {"path": "b.c", "old_string": "return 0;", "new_string": "return 2;"},
    ]
    res = fs.apply_edits(edits, atomic=True)
    assert res["ok"] and res["files"] == 2
    assert "return 1;" in (root / "a.c").read_bytes().decode("gb18030")
    assert "return 2;" in (root / "b.c").read_bytes().decode("gb18030")


def test_intra_batch_ordering_same_file(fs, root):
    # Two edits to the same file in one atomic call apply in order (CR7).
    (root / "x.c").write_bytes("val = ONE;\r\n".encode("gbk"))
    fs.read_file("x.c")
    edits = [
        {"path": "x.c", "old_string": "ONE", "new_string": "TWO"},
        {"path": "x.c", "old_string": "TWO", "new_string": "THREE"},
    ]
    res = fs.apply_edits(edits, atomic=True)
    assert res["ok"]
    assert "val = THREE;" in (root / "x.c").read_bytes().decode("gb18030")


def test_nonatomic_reports_per_edit_status(fs, root):
    write_gbk_c(root / "a.c")
    fs.read_file("a.c")
    edits = [
        {"path": "a.c", "old_string": "return 0;", "new_string": "return 1;"},
        {"path": "a.c", "old_string": "NOPE", "new_string": "x"},
    ]
    res = fs.apply_edits(edits, atomic=False)
    assert res["ok"] is False
    assert res["results"][0]["ok"] is True
    assert res["results"][1]["ok"] is False and res["results"][1]["code"] == "MATCH_NOT_FOUND"
