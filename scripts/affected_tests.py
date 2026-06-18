"""Command-line wrapper for the affected-test helper."""

from pathlib import Path
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))

    from tests.support.affected_tests import main as support_main

    return support_main()


if __name__ == "__main__":
    raise SystemExit(main())
