"""Report local and PyPI status for every publishable workspace package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_support import (  # noqa: E402
    ReleaseError,
    classify_package_release,
    discover_packages,
    pypi_version,
)


def collect_status(root: Path = ROOT) -> list[dict[str, Any]]:
    rows = []
    for package in discover_packages(root):
        try:
            remote = pypi_version(package.name)
            classification = classify_package_release(package, remote, root)
            status = classification.state
            detail = classification.detail
        except Exception as error:  # Keep reporting the remaining packages.
            remote = None
            status = "error"
            detail = str(error)
        rows.append(
            {
                "package": package.name,
                "local": package.version,
                "pypi": remote or "-",
                "status": status,
                "detail": detail,
            }
        )
    return rows


def _print_table(rows: list[dict[str, Any]]) -> None:
    headers = {"package": "PACKAGE", "local": "LOCAL", "pypi": "PYPI", "status": "STATUS"}
    widths = {
        key: max(len(label), *(len(str(row[key])) for row in rows))
        for key, label in headers.items()
    }
    print("  ".join(label.ljust(widths[key]) for key, label in headers.items()))
    for row in rows:
        print("  ".join(str(row[key]).ljust(widths[key]) for key in headers))
        if row["status"] not in {"up-to-date", "pending", "unpublished"}:
            print(f"  {row['detail']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero unless every package is up to date",
    )
    args = parser.parse_args(argv)

    try:
        rows = collect_status()
    except ReleaseError as error:
        parser.error(str(error))

    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        _print_table(rows)
    if args.check and any(row["status"] != "up-to-date" for row in rows):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
