"""Entry point: ``python -m gbk_fs`` / ``gbk-fs-mcp`` (stdio MCP server).

Configuration precedence: CLI ``--config`` > ``--root`` (looks for ``<root>/.gbk-fs.json``).
Env vars ``GBK_FS_CONFIG`` / ``GBK_FS_ROOT`` are honoured as defaults so the server can be
registered in ``.mcp.json`` without command-line plumbing.
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import load_config
from .core import GbkFs
from .errors import GbkFsError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gbk-fs-mcp", description="Encoding-aware filesystem MCP server")
    parser.add_argument("--config", default=os.environ.get("GBK_FS_CONFIG"),
                        help="Path to a .gbk-fs.json config file")
    parser.add_argument("--root", default=os.environ.get("GBK_FS_ROOT"),
                        help="Sandbox root directory (overrides config 'root')")
    args = parser.parse_args(argv)

    try:
        config = load_config(config_path=args.config, root=args.root)
    except GbkFsError as exc:
        print(f"gbk-fs config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover
        print(f"gbk-fs failed to start: {exc}", file=sys.stderr)
        return 2

    core = GbkFs(config)

    # Imported lazily so the core package is usable/testable without the mcp dependency.
    from .server import build_server

    server = build_server(core)
    print(f"gbk-fs: serving root {config.root} (decode={config.default_encoding}, "
          f"encode={config.encode_codec})", file=sys.stderr)
    server.run()  # stdio transport
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
