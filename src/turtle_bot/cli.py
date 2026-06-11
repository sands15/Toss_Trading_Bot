from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__
from .config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turtle_bot",
        description="Turtle Trading bot (strategy core)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"turtle-bot {__version__}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Load a YAML config file",
        metavar="PATH",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate config file and exit",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check_config:
        if args.config is None:
            parser.error("--check-config requires --config")
        load_config(args.config)
        return 0

    if args.config is not None:
        parser.print_usage()
        return 1

    return 0

