"""read_git (#2): decode a file's bytes from a git ref through the encoding-aware pipeline.

Recovery primitive: when the working tree is corrupt, the clean source is git (the index or
a commit). These tests build a throwaway repo and verify read_git decodes GBK correctly,
reaches both HEAD and the index, errors clearly, never marks the working file read, and
supports a byte-exact recovery round-trip.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess

import pytest

from gbk_fs import GbkFs, load_config
from gbk_fs.errors import GitError, UnreadOverwrite
from conftest import write_gbk_c

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


def _commit(repo, name="sync.c"):
    original = write_gbk_c(repo / name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", "add")
    return original


def test_read_git_head_decodes_gbk(repo):
    original = _commit(repo)
    fs = GbkFs(load_config(root=repo))
    res = fs.read_git("sync.c", "HEAD")
    assert res["detected_encoding"] == "gbk"
    assert "同步 VRF 路由信息" in res["content"]
    assert res["ref"] == "HEAD"
    assert res["source"] == "git:HEAD"
    assert res["mtime_ns"] is None
    # the reported sha256 is the hash of the raw stored blob bytes
    assert res["sha256"] == hashlib.sha256(original).hexdigest()


def test_read_git_index_sees_staged_version(repo):
    _commit(repo)
    # stage a different version than what's committed
    (repo / "sync.c").write_bytes("// 暂存版本\r\nint staged;\r\n".encode("gbk"))
    _git(repo, "add", "sync.c")
    fs = GbkFs(load_config(root=repo))
    res = fs.read_git("sync.c", ":0:")
    assert "暂存版本" in res["content"] and "int staged;" in res["content"]
    # the 'index' alias resolves to the same stage-0 entry
    assert "暂存版本" in fs.read_git("sync.c", "index")["content"]


def test_read_git_unknown_path_errors(repo):
    _commit(repo)
    fs = GbkFs(load_config(root=repo))
    with pytest.raises(GitError):
        fs.read_git("nope.c", "HEAD")


def test_read_git_not_a_repo_errors(root):
    fs = GbkFs(load_config(root=root))  # root is NOT a git repo
    with pytest.raises(GitError):
        fs.read_git("anything.c", "HEAD")


def test_read_git_does_not_mark_read(repo):
    _commit(repo)
    fs = GbkFs(load_config(root=repo))
    fs.read_git("sync.c", "HEAD")
    # reading a git blob is not seeing current disk state -> must NOT authorize overwrite
    with pytest.raises(UnreadOverwrite):
        fs.write_file("sync.c", "// 覆盖\n")


def test_read_git_recovery_round_trip(repo):
    """The incident in miniature: corrupt the working file, restore it byte-exactly from HEAD."""
    original = _commit(repo)
    # simulate the corruption: GBK bytes read as UTF-8 and saved back (Chinese -> U+FFFD)
    corrupted = original.decode("utf-8", "replace").encode("utf-8")
    (repo / "sync.c").write_bytes(corrupted)
    assert (repo / "sync.c").read_bytes() != original  # genuinely broken

    fs = GbkFs(load_config(root=repo))
    clean = fs.read_git("sync.c", "HEAD")
    # rebuild the source text from the cat -n view, then write it back as GBK
    text = "\n".join(line.split("\t", 1)[1] for line in clean["content"].splitlines())
    if clean["final_newline"]:
        text += "\n"
    fs.write_file("sync.c", text, encoding="gbk", eol=clean["eol"], allow_overwrite_unread=True)
    assert (repo / "sync.c").read_bytes() == original
