"""Shared test fixtures.

Tests run against the package via the src layout (added to sys.path here so they work even
without an editable install). Each test gets a fresh sandbox root + GbkFs instance.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gbk_fs import GbkFs, load_config  # noqa: E402


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.fixture
def fs(root):
    return GbkFs(load_config(root=root))


# ---- helpers shared by tests -------------------------------------------------------

#: A line of Chinese comment + an ASCII code line, the canonical GBK-source shape.
CHINESE_COMMENT = "// 同步 VRF 路由信息（含中文注释）"
CODE = "int nsm_vrf_sync_gr_info(void) { return 0; }"


def write_gbk_c(path: pathlib.Path, *, eol: str = "\r\n", final_nl: bool = True) -> bytes:
    """Write a GBK-encoded C file with Chinese comments and return its raw bytes."""
    text = eol.join([CHINESE_COMMENT, CODE, "// end 结束"])
    if final_nl:
        text += eol
    raw = text.encode("gbk")
    path.write_bytes(raw)
    return raw


def write_raw(path: pathlib.Path, data: bytes) -> bytes:
    path.write_bytes(data)
    return data
