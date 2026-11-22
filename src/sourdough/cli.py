"""Local entry point for explicit setup and reopening."""

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from sourdough.company import FoundingRefused, FoundingTerms, found, reopen
from sourdough.employee_process import EmployeeProcess
from sourdough.first_duty import DutyRefused
from sourdough.first_trade import TradeRefused, run_first_trade
from sourdough.paper_account import BindingRefused, PaperAccount, PaperMandate, bind
from sourdough.paper_books import PostingRefused
from sourdough.paper_order import OrderRefused
from sourdough.paper_venue import FixedQuoteVenue, VenueRefused


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
    profile = commands.add_parser("employee-profile")
    profile.add_argument("executable", type=Path)
    paper = commands.add_parser("bind-paper")
    paper.add_argument("store", type=Path)
    paper.add_argument("journal", type=Path)
    for name in ("company-id", "charter-revision", "revision", "account-id", "rules"):
        paper.add_argument(f"--{name}", required=True)
    for name in ("starting-minor-units", "exposure-minor-units", "quote-minor-units"):
        paper.add_argument(f"--{name}", required=True, type=int)
    running = commands.add_parser("first-duty")
    running.add_argument("store", type=Path)
    running.add_argument("journal", type=Path)
    running.add_argument("--company-id", required=True)
    running.add_argument("--employee", type=Path, required=True)
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
        elif args.command == "reopen":
            result = reopen(args.store, args.company_id)
        elif args.command == "employee-profile":
            print(
                json.dumps(
                    {"execution_profile": EmployeeProcess(args.executable).profile}
                )
            )
            return 0
        elif args.command == "bind-paper":
            if args.store.resolve() == args.journal.resolve() or (
                args.journal.exists() and args.store.samefile(args.journal)
            ):
                raise TradeRefused("Company store and venue journal must be separate")
            reopen(args.store, args.company_id)
            venue = FixedQuoteVenue(args.journal)
            account = PaperAccount(
                args.account_id,
                "fixed-quote-v1",
                args.rules,
                "USD",
                args.starting_minor_units,
                "paper",
            )
            account = venue.provision(account, args.quote_minor_units)
            result = bind(
                args.store,
                PaperMandate(
                    args.revision,
                    args.company_id,
                    args.charter_revision,
                    account,
                    args.exposure_minor_units,
                ),
                venue,
            )
        else:
            result = run_first_trade(
                args.store,
                args.company_id,
                FixedQuoteVenue(args.journal),
                EmployeeProcess(args.employee),
            )
    except (
        FoundingRefused,
        BindingRefused,
        DutyRefused,
        TradeRefused,
        OrderRefused,
        PostingRefused,
        VenueRefused,
        sqlite3.Error,
        OSError,
    ) as error:
        print(f"Company command refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
