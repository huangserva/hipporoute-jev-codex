from __future__ import annotations

import argparse
import os

from .config import load_config
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="HippoRoute-Jev-Codex boundary router")
    parser.add_argument(
        "--config",
        default=os.environ.get("CODEX_JEV_ROUTER_CONFIG"),
        help="TOML configuration path (defaults to built-in settings)",
    )
    args = parser.parse_args()
    serve(load_config(args.config))


if __name__ == "__main__":
    main()
