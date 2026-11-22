"""Local entry point for explicit setup and reopening."""

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from sourdough.company import FoundingRefused, FoundingTerms, found, reopen
from sourdough.company_run import run_paper_company
from sourdough.duty_run import count_company_runs
from sourdough.employee_process import EmployeeProcess
from sourdough.first_duty import DutyRefused
from sourdough.first_trade import TradeRefused, run_first_trade
from sourdough.paper_account import BindingRefused, PaperAccount, PaperMandate, bind
from sourdough.paper_books import PostingRefused
from sourdough.paper_order import OrderRefused
from sourdough.paper_research import stage_research_inputs
from sourdough.paper_venue import FixedQuoteVenue, VenueRefused
from sourdough.supervisor import SupervisionRefused, announce_ready, supervise_worker
from sourdough.writer_lease import WriterRefused, hold_company_writer
from sourdough.work_projection import read_local_board_timeline


def _store_key(value: str) -> tuple[int, int]:
    try:
        device, inode = (int(part) for part in value.split(":"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected device:inode") from error
    if device < 0 or inode <= 0:
        raise argparse.ArgumentTypeError("Invalid device:inode")
    return device, inode


def _start_supervisor(args):
    command = [
        sys.executable,
        "-I",
        "-m",
        "sourdough.cli",
        "_paper-worker",
        str(args.store),
        str(args.journal),
        "--company-id",
        args.company_id,
        "--employee",
        str(args.employee),
        "--turn-budget",
        str(args.turn_budget),
        "--idle-seconds",
        str(args.idle_seconds),
        "--budget-from",
        str(count_company_runs(args.store, args.company_id)),
    ]
    if args.repeat_paper:
        command.append("--repeat-paper")
    return supervise_worker(
        command,
        args.store,
        args.company_id,
        max_restarts=args.max_restarts,
        startup_seconds=args.startup_seconds,
        max_seconds=args.max_seconds,
        emit=lambda event: print(json.dumps(event), file=sys.stderr, flush=True),
    )


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
    inspection = commands.add_parser(
        "inspect-work", help="Inspect the local read-only company timeline"
    )
    inspection.add_argument("store", type=Path)
    inspection.add_argument("--company-id", required=True)
    profile = commands.add_parser("employee-profile")
    profile.add_argument("executable", type=Path)
    paper = commands.add_parser("bind-paper")
    paper.add_argument("store", type=Path)
    paper.add_argument("journal", type=Path)
    for name in ("company-id", "charter-revision", "revision", "account-id", "rules"):
        paper.add_argument(f"--{name}", required=True)
    for name in ("starting-minor-units", "exposure-minor-units", "quote-minor-units"):
        paper.add_argument(f"--{name}", required=True, type=int)
    paper.add_argument("--fee-bps", type=int, default=0)
    inputs = commands.add_parser("stage-paper-observations")
    inputs.add_argument("store", type=Path)
    inputs.add_argument("observations", type=Path)
    inputs.add_argument("--company-id", required=True)
    for command in ("first-duty", "operate-paper", "supervise-paper", "_paper-worker"):
        running = commands.add_parser(command)
        running.add_argument("store", type=Path)
        running.add_argument("journal", type=Path)
        running.add_argument("--company-id", required=True)
        running.add_argument("--employee", type=Path, required=True)
        if command != "first-duty":
            running.add_argument("--repeat-paper", action="store_true")
            running.add_argument("--turn-budget", type=int, default=2)
            running.add_argument("--idle-seconds", type=int, default=0)
        if command == "supervise-paper":
            running.add_argument("--max-restarts", type=int, default=2)
            running.add_argument("--startup-seconds", type=int, default=60)
            running.add_argument("--max-seconds", type=int, default=180)
        if command == "_paper-worker":
            running.add_argument("--ready-fd", type=int, required=True)
            running.add_argument("--store-key", type=_store_key, required=True)
            running.add_argument("--budget-from", type=int, required=True)
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
        elif args.command == "inspect-work":
            print(json.dumps(read_local_board_timeline(args.store, args.company_id)))
            return 0
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
            venue = FixedQuoteVenue(args.journal)
            account = PaperAccount(
                args.account_id,
                "fixed-quote-v1",
                args.rules,
                "USD",
                args.starting_minor_units,
                "paper",
            )
            with hold_company_writer(args.store, args.company_id) as writer:
                writer.verify()
                account = venue.provision(
                    account, args.quote_minor_units, fee_bps=args.fee_bps
                )
                writer.verify()
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
        elif args.command == "stage-paper-observations":
            with hold_company_writer(args.store, args.company_id) as writer:
                writer.verify()
                with args.observations.open("rb") as source:
                    content = source.read(64_001)
                if len(content) > 64_000:
                    raise DutyRefused(
                        "Research observation input exceeds its size limit"
                    )
                try:
                    observations = json.loads(content)
                except (UnicodeError, ValueError) as error:
                    raise DutyRefused("Invalid research observation input") from error
                result = stage_research_inputs(
                    args.store, args.company_id, observations
                )
            print(json.dumps(result))
            return 0
        elif args.command == "supervise-paper":
            result = _start_supervisor(args)
            print(json.dumps(asdict(result)))
            return 0 if result.completed else 1
        else:
            inputs = (
                args.store,
                args.company_id,
                FixedQuoteVenue(args.journal),
                EmployeeProcess(args.employee),
            )
            if args.command == "first-duty":
                with hold_company_writer(args.store, args.company_id) as writer:
                    result = run_first_trade(*inputs, writer=writer)
            elif args.command == "_paper-worker":
                result = run_paper_company(
                    *inputs,
                    repeat_paper=args.repeat_paper,
                    turn_budget=args.turn_budget,
                    idle_seconds=args.idle_seconds,
                    expected_store_key=args.store_key,
                    budget_from=args.budget_from,
                    on_ready=lambda lease: announce_ready(args.ready_fd, lease),
                )
            else:
                result = run_paper_company(
                    *inputs,
                    repeat_paper=args.repeat_paper,
                    turn_budget=args.turn_budget,
                    idle_seconds=args.idle_seconds,
                )
    except (
        FoundingRefused,
        BindingRefused,
        DutyRefused,
        TradeRefused,
        OrderRefused,
        PostingRefused,
        VenueRefused,
        SupervisionRefused,
        WriterRefused,
        sqlite3.Error,
        OSError,
    ) as error:
        print(f"Company command refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
