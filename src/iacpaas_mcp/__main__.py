"""Запуск MCP-сервера: python -m iacpaas_mcp или iacpaas-mcp."""

import argparse

from . import __version__
from .server import mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="iacpaas-mcp", description="MCP-сервер IACPaaS")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    args = parser.parse_args()

    if args.transport == "http":
        mcp.run(transport="http", host="127.0.0.1", port=8000)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
