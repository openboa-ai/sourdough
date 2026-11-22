"""Local entry point for explicit setup and reopening."""

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from sourdough.company import FoundingRefused, FoundingTerms, found, reopen


def main() -> int:
    parser = argparse.ArgumentParser(prog="sourdough")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("found", help="Persist the initial board mandate")
    setup.add_argument("store", type=Path)
    setup.add_argument("--setup-identity", required=True)
    setup.add_argument("--charter-revision", required=True)
    setup.add_argument("--founder-scope", required=True)
    setup.add_argument(
        "--execution-profile",
        required=True,
        help="Immutable execution profile reference",
    )
    opening = commands.add_parser(
        "reopen", help="Load an expected company and its due work"
    )
    opening.add_argument("store", type=Path)
    opening.add_argument("--company-id", required=True)
    args = parser.parse_args()

    try:
        if args.command == "found":
            result = found(
                args.store,
                FoundingTerms(
                    args.setup_identity,
                    args.charter_revision,
                    args.founder_scope,
                    args.execution_profile,
                ),
            )
        else:
            result = reopen(args.store, args.company_id)
    except (FoundingRefused, sqlite3.Error) as error:
        print(f"Company command refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
