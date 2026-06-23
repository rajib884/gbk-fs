"""gbk-fs: an encoding-aware filesystem MCP server.

Presents UTF-8 to the model while persisting files in their on-disk encoding (GBK family by
default), with byte-level round-trip fidelity for unchanged regions. See the requirements
doc and ``README.md``.
"""

from __future__ import annotations

from .config import Config, load_config
from .core import GbkFs
from .errors import GbkFsError

__version__ = "0.1.0"

__all__ = ["GbkFs", "Config", "load_config", "GbkFsError", "__version__"]
