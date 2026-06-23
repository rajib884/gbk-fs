"""Sandbox & deny-list tests (§9.6, §9.7, NFR4)."""

from __future__ import annotations

import pytest

from gbk_fs.errors import Denied, IsBinary, OutsideRoot
from conftest import write_raw


def test_reject_path_outside_root(fs, root):
    (root.parent / "secret.txt").write_text("top secret", encoding="utf-8")
    with pytest.raises(OutsideRoot):
        fs.read_file("../secret.txt")
    with pytest.raises(OutsideRoot):
        fs.read_file("../../etc/hosts")


def test_reject_absolute_path_outside_root(fs):
    with pytest.raises(OutsideRoot):
        fs.read_file("C:/Windows/System32/drivers/etc/hosts")


def test_binary_deny_glob(fs, root):
    (root / "lib").mkdir()
    write_raw(root / "lib" / "librtv6.a", b"!<arch>\n\x00\x01\x02binary")
    with pytest.raises(Denied):
        fs.read_file("lib/librtv6.a")


def test_git_dir_denied(fs, root):
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    with pytest.raises(Denied):
        fs.read_file(".git/config")


def test_binary_content_rejected_even_if_not_denied(fs, root):
    # .dat isn't in the deny-list, but NUL bytes trip the binary heuristic.
    write_raw(root / "blob.dat", b"abc\x00\x01\x02def")
    with pytest.raises(IsBinary):
        fs.read_file("blob.dat")


def test_list_skips_denied_and_git(fs, root):
    (root / "sys").mkdir()
    (root / "sys" / "a.c").write_bytes("int a;".encode("gbk"))
    (root / "sys" / "a.o").write_bytes(b"\x00\x01")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")

    res = fs.list_files(glob="**/*")
    paths = {e["path"] for e in res["files"]}
    assert "sys/a.c" in paths
    assert "sys/a.o" not in paths      # denied glob
    assert not any(p.startswith(".git/") for p in paths)
