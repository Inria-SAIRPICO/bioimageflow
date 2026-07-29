"""Command-line interface for BioImageFlow storage utilities."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from bioimageflow.storage import export_outputs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bioimageflow")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export-outputs",
        help="materialize cached workflow outputs for sharing or inspection",
    )
    export.add_argument("storage_path", type=Path)
    export.add_argument(
        "--destination",
        type=Path,
        help="install a complete output root at this external path",
    )
    export.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing explicit destination",
    )
    export.add_argument(
        "--mode",
        choices=("copy", "hardlink", "symlink", "pointer"),
        default="copy",
    )
    export.add_argument(
        "--scope",
        choices=("latest", "runs", "both"),
        default="latest",
    )
    export.add_argument(
        "--run-id",
        help="run to export for runs/both scope; defaults to latest successful run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the BioImageFlow command-line interface."""
    args = _parser().parse_args(argv)
    if args.command == "export-outputs":
        paths = export_outputs(
            args.storage_path,
            destination=args.destination,
            replace=args.replace,
            mode=args.mode,
            scope=args.scope,
            run_id=args.run_id,
        )
        for path in paths:
            print(path)
        return 0
    raise AssertionError(f"Unhandled command: {args.command}")
