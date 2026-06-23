"""Search tests (§9.4, FR3)."""

from __future__ import annotations

from conftest import write_gbk_c


def _seed(root):
    (root / "sys" / "rtv6").mkdir(parents=True)
    write_gbk_c(root / "sys" / "rtv6" / "nsm_vrf_sync.c")
    write_gbk_c(root / "sys" / "rtv6" / "other.c")
    (root / "README.md").write_text("# docs nsm_vrf_sync_gr_info reference\n", encoding="utf-8")


def test_search_content_clean_utf8_and_line_numbers(fs, root):
    _seed(root)
    res = fs.search_content(pattern="nsm_vrf_sync_gr_info", output_mode="content")
    assert res["mode"] == "content"
    assert res["files_matched"] >= 2
    # every emitted match line is on line 2 of the C files, content is clean UTF-8
    c_hits = [
        ln for fr in res["results"] if fr["path"].endswith(".c")
        for ln in fr["lines"] if ln.get("is_match")
    ]
    assert c_hits and all(h["line"] == 2 for h in c_hits)
    assert all("�" not in h["text"] for h in c_hits)  # no mojibake / replacement chars


def test_search_files_mode(fs, root):
    _seed(root)
    res = fs.search_content(pattern="同步", output_mode="files")
    assert res["mode"] == "files"
    assert all(p.endswith(".c") for p in res["results"])
    assert len(res["results"]) == 2


def test_search_count_mode(fs, root):
    _seed(root)
    res = fs.search_content(pattern="end 结束", output_mode="count")
    assert res["total_matches"] == 2
    assert {r["path"] for r in res["results"]} == {
        "sys/rtv6/nsm_vrf_sync.c", "sys/rtv6/other.c"
    }


def test_search_glob_and_type_filters(fs, root):
    _seed(root)
    only_c = fs.search_content(pattern="nsm_vrf_sync_gr_info", type="c", output_mode="files")
    assert all(p.endswith(".c") for p in only_c["results"])

    only_md = fs.search_content(pattern="nsm_vrf_sync_gr_info", glob="**/*.md", output_mode="files")
    assert only_md["results"] == ["README.md"]


def test_search_context_lines(fs, root):
    _seed(root)
    res = fs.search_content(pattern="nsm_vrf_sync_gr_info", path="sys/rtv6/nsm_vrf_sync.c",
                            before=1, after=0, output_mode="content")
    block = res["results"][0]["lines"]
    # context line 1 (the Chinese comment) precedes the match on line 2
    assert any(e.get("line") == 1 and not e["is_match"] for e in block)
    assert any(e.get("line") == 2 and e["is_match"] for e in block)


def test_search_multiple_patterns_or(fs, root):
    _seed(root)
    res = fs.search_content(patterns=["路由信息", "结束"], path="sys/rtv6/nsm_vrf_sync.c",
                            output_mode="count")
    assert res["total_matches"] == 2  # two distinct lines match
