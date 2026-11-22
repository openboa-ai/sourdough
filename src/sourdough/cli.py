"""Local entry point for explicit setup and reopening."""

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from sourdough.assessment_run import run_assessment_company
from sourdough.company import FoundingRefused, FoundingTerms, found, reopen
from sourdough.company_assessment import SourceRecord, seal_assessment_source
from sourdough.company_run import run_paper_company
from sourdough.duty_run import count_company_runs
from sourdough.employee_process import EmployeeProcess
from sourdough.first_duty import DutyRefused
from sourdough.first_trade import TradeRefused, run_first_trade
from sourdough.hired_paper_run import run_hired_paper_company
from sourdough.hiring_run import run_hiring
from sourdough.market_runtime import observe_market
from sourdough.market_worker import MarketRefused
from sourdough.nautilus_runtime import bind_paper as bind_nautilus_paper
from sourdough.nautilus_runtime import (
    paper_venue,
    prepare_paper,
    supervise_paper,
    switch_paper,
)
from sourdough.paper_account import BindingRefused, PaperAccount, PaperMandate, bind
from sourdough.paper_books import PostingRefused
from sourdough.paper_execution import VenueRefused
from sourdough.paper_order import OrderRefused
from sourdough.paper_research import stage_research_inputs
from sourdough.paper_statement_store import (
    StatementRequest,
    read_retained_statement,
    retain_paper_statement,
)
from sourdough.paper_venue import FixedQuoteVenue
from sourdough.paper_valuation import ValuationRefused
from sourdough.paper_window import WindowPlan, WindowRefused
from sourdough.paper_window_statement import read_paper_statement
from sourdough.staffing import StaffingAuthority, StaffingNeed, stage_staffing
from sourdough.supervisor import SupervisionRefused, announce_ready, supervise_worker
from sourdough.trading_engine import EngineRefused, prepare_engine
from sourdough.window_sampler import WindowSampler, register_managed_window
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


def _agent_receipt(fields):
    receipt = dict(fields)
    if "founder_id" in receipt and "run_id" in receipt:
        receipt["agent_id"] = receipt.pop("founder_id")
    return receipt


def _start_supervisor(args):
    assessment = args.command == "supervise-assessment"
    worker_name = {
        "supervise-assessment": "_assessment-worker",
        "supervise-hired-paper": "_hired-paper-worker",
        "supervise-paper": "_paper-worker",
    }[args.command]
    command = [
        sys.executable,
        "-I",
        "-m",
        "sourdough.cli",
        worker_name,
        str(args.store),
        *([] if assessment else [str(args.journal)]),
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
    if getattr(args, "repeat_paper", False):
        command.append("--repeat-paper")
    if getattr(args, "managed_engine", False):
        command.append("--managed-engine")
    if args.command == "supervise-paper" and args.observe_window is not None:
        _paper_sampler(args, args.journal, managed=args.managed_engine)
        command.extend(("--observe-window", args.observe_window))
    return supervise_worker(
        command,
        args.store,
        args.company_id,
        max_restarts=args.max_restarts,
        startup_seconds=args.startup_seconds,
        max_seconds=args.max_seconds,
        emit=lambda event: print(json.dumps(event), file=sys.stderr, flush=True),
    )


def _paper_destination(args):
    if getattr(args, "managed_engine", False):
        return paper_venue(args.store, args.journal)
    return FixedQuoteVenue(args.journal)


def _paper_sampler(args, directory: Path, *, managed: bool) -> WindowSampler | None:
    if args.observe_window is None:
        return None
    if not managed:
        raise WindowRefused(
            "Only a managed current-market activation can sample a window"
        )
    return WindowSampler(directory, args.observe_window)


def _managed_destination_flag(command):
    command.add_argument(
        "--managed-engine",
        action="store_true",
        help="Use the original managed engine directory instead of a fixture journal",
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
    seal = commands.add_parser("seal-paper-window")
    seal.add_argument("store", type=Path)
    seal.add_argument("directory", type=Path)
    seal.add_argument("--company-id", required=True)
    seal.add_argument("--plan", required=True, type=Path)
    for command in ("inspect-paper-window", "close-paper-window"):
        statement = commands.add_parser(command)
        statement.add_argument("store", type=Path)
        statement.add_argument("directory", type=Path)
        statement.add_argument("--company-id", required=True)
        statement.add_argument("--window-id", required=True)
        if command == "inspect-paper-window":
            lookup = statement.add_mutually_exclusive_group()
            lookup.add_argument("--accounting-close")
            lookup.add_argument("--report-id")
        else:
            statement.add_argument("--accounting-close")
            statement.add_argument("--request-id", required=True)
            statement.add_argument("--supersedes")
    engine = commands.add_parser("prepare-trading-engine")
    engine.add_argument("store", type=Path)
    engine.add_argument("directory", type=Path)
    engine.add_argument("--company-id", required=True)
    engine.add_argument("--wheelhouse", type=Path)
    market = commands.add_parser("observe-trading-market")
    market.add_argument("store", type=Path)
    market.add_argument("directory", type=Path)
    market.add_argument("--company-id", required=True)
    market.add_argument("--symbol", required=True)
    market.add_argument("--max-age-seconds", type=int, default=15)
    market.add_argument("--samples", type=int, default=3)
    for command in (
        "prepare-nautilus-paper",
        "bind-nautilus-paper",
        "switch-nautilus-paper",
        "serve-nautilus-paper",
        "operate-nautilus-paper",
    ):
        sandbox = commands.add_parser(command)
        sandbox.add_argument("store", type=Path)
        sandbox.add_argument("directory", type=Path)
        sandbox.add_argument("--company-id", required=True)
        if command == "prepare-nautilus-paper":
            sandbox.add_argument("--account-id", required=True)
            sandbox.add_argument("--starting-atoms", required=True, type=int)
            sandbox.add_argument("--replay-tape", type=Path)
        elif command in ("bind-nautilus-paper", "switch-nautilus-paper"):
            sandbox.add_argument("--revision", required=True)
            sandbox.add_argument("--charter-revision", required=True)
            sandbox.add_argument("--exposure-atoms", required=True, type=int)
            sandbox.add_argument("--replay", action="store_true")
            if command == "switch-nautilus-paper":
                sandbox.add_argument("--previous-directory", type=Path, required=True)
                sandbox.add_argument("--previous-revision", required=True)
                sandbox.add_argument("--from-replay", action="store_true")
        elif command == "serve-nautilus-paper":
            sandbox.add_argument("--seconds", type=int, default=60)
            sandbox.add_argument("--replay-tape", type=Path)
        else:
            sandbox.add_argument("--employee", type=Path, required=True)
            sandbox.add_argument("--turn-budget", type=int, default=1)
            sandbox.add_argument("--idle-seconds", type=int, default=0)
            sandbox.add_argument("--replay", action="store_true")
            sandbox.add_argument("--observe-window")
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
    staffing = commands.add_parser("stage-staffing")
    staffing.add_argument("store", type=Path)
    staffing.add_argument("envelope", type=Path)
    staffing.add_argument("--company-id", required=True)
    source = commands.add_parser("stage-assessment-source")
    source.add_argument("store", type=Path)
    source.add_argument("records", type=Path)
    source.add_argument("--company-id", required=True)
    for command in (
        "supervise-assessment",
        "_assessment-worker",
        "supervise-hired-paper",
        "_hired-paper-worker",
    ):
        managed = commands.add_parser(command)
        managed.add_argument("store", type=Path)
        if "hired-paper" in command:
            managed.add_argument("journal", type=Path)
            _managed_destination_flag(managed)
        managed.add_argument("--company-id", required=True)
        managed.add_argument("--employee", type=Path, required=True)
        managed.add_argument("--turn-budget", type=int, default=3)
        managed.add_argument("--idle-seconds", type=int, default=140)
        if command.startswith("supervise-"):
            managed.add_argument("--max-restarts", type=int, default=1)
            managed.add_argument("--startup-seconds", type=int, default=35)
            managed.add_argument("--max-seconds", type=int, default=360)
        else:
            managed.add_argument("--ready-fd", type=int, required=True)
            managed.add_argument("--store-key", type=_store_key, required=True)
            managed.add_argument("--budget-from", type=int, required=True)
    hiring = commands.add_parser("operate-hiring")
    hiring.add_argument("store", type=Path)
    hiring.add_argument("--company-id", required=True)
    hiring.add_argument("--employee", type=Path, required=True)
    hiring.add_argument(
        "--first-purpose", required=True, choices=("assessment", "paper_execution")
    )
    for command in ("first-duty", "operate-paper", "supervise-paper", "_paper-worker"):
        running = commands.add_parser(command)
        running.add_argument("store", type=Path)
        running.add_argument("journal", type=Path)
        running.add_argument("--company-id", required=True)
        running.add_argument("--employee", type=Path, required=True)
        if command != "first-duty":
            _managed_destination_flag(running)
            running.add_argument("--repeat-paper", action="store_true")
            running.add_argument("--turn-budget", type=int, default=2)
            running.add_argument("--idle-seconds", type=int, default=0)
            running.add_argument("--observe-window")
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
        elif args.command == "inspect-paper-window":
            if args.report_id is not None:
                saved = read_retained_statement(
                    args.store, args.company_id, args.report_id
                )
                if saved is None or saved.request.window_id != args.window_id:
                    raise WindowRefused(
                        "No retained statement for the requested window"
                    )
                report = saved.as_receipt()
            else:
                report = read_paper_statement(
                    args.store,
                    args.company_id,
                    args.directory,
                    args.window_id,
                    close_id=args.accounting_close,
                ).as_report()
            print(json.dumps(report))
            return 0
        elif args.command == "close-paper-window":
            request = StatementRequest(
                args.request_id, args.window_id, args.accounting_close, args.supersedes
            )
            with hold_company_writer(args.store, args.company_id) as writer:
                saved = retain_paper_statement(writer, args.directory, request)
            print(json.dumps(saved.as_receipt()))
            return 0
        elif args.command == "seal-paper-window":
            with args.plan.open("rb") as source:
                payload = source.read(16_385)
            if len(payload) > 16_384:
                raise WindowRefused("Window plan exceeds its size limit")
            try:
                plan = WindowPlan(**json.loads(payload))
            except (ValueError, TypeError) as error:
                raise WindowRefused("Window plan is not a valid declaration") from error
            with hold_company_writer(args.store, args.company_id) as writer:
                result = register_managed_window(writer, args.directory, plan)
        elif args.command == "employee-profile":
            print(
                json.dumps(
                    {"execution_profile": EmployeeProcess(args.executable).profile}
                )
            )
            return 0
        elif args.command == "prepare-trading-engine":
            with hold_company_writer(args.store, args.company_id) as writer:
                result = prepare_engine(
                    writer, args.directory, wheelhouse=args.wheelhouse
                )
        elif args.command == "observe-trading-market":
            result = observe_market(
                args.store,
                args.company_id,
                args.directory,
                symbol=args.symbol,
                max_age_seconds=args.max_age_seconds,
                samples=args.samples,
                emit=lambda event: print(
                    json.dumps(event), file=sys.stderr, flush=True
                ),
            )
            print(json.dumps(asdict(result)))
            return 0 if result.completed else 1
        elif args.command == "prepare-nautilus-paper":
            with hold_company_writer(args.store, args.company_id) as writer:
                identity = prepare_paper(
                    writer,
                    args.directory,
                    args.account_id,
                    args.starting_atoms,
                    tape_path=args.replay_tape,
                )
            print(json.dumps({"destination_id": identity}))
            return 0
        elif args.command == "bind-nautilus-paper":
            with hold_company_writer(args.store, args.company_id) as writer:
                result = bind_nautilus_paper(
                    writer,
                    args.directory,
                    revision=args.revision,
                    charter_revision=args.charter_revision,
                    exposure_atoms=args.exposure_atoms,
                    replay=args.replay,
                )
        elif args.command == "switch-nautilus-paper":
            with hold_company_writer(args.store, args.company_id) as writer:
                result = switch_paper(
                    writer,
                    args.previous_directory,
                    args.directory,
                    previous_revision=args.previous_revision,
                    revision=args.revision,
                    charter_revision=args.charter_revision,
                    exposure_atoms=args.exposure_atoms,
                    from_replay=args.from_replay,
                    replay=args.replay,
                )
        elif args.command == "serve-nautilus-paper":
            result = supervise_paper(
                args.store,
                args.company_id,
                args.directory,
                seconds=args.seconds,
                tape_path=args.replay_tape,
                emit=lambda event: print(
                    json.dumps(event), file=sys.stderr, flush=True
                ),
            )
            print(json.dumps(asdict(result)))
            return 0 if result.completed else 1
        elif args.command == "operate-nautilus-paper":
            sampler = _paper_sampler(args, args.directory, managed=not args.replay)
            result = run_paper_company(
                args.store,
                args.company_id,
                paper_venue(args.store, args.directory, replay=args.replay),
                EmployeeProcess(args.employee),
                turn_budget=args.turn_budget,
                idle_seconds=args.idle_seconds,
                sampler=sampler,
            )
        elif args.command == "bind-paper":
            venue = FixedQuoteVenue(args.journal)
            venue.require_independent_from(args.store)
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
        elif args.command in (
            "supervise-paper",
            "supervise-assessment",
            "supervise-hired-paper",
        ):
            result = _start_supervisor(args)
            print(json.dumps(asdict(result)))
            return 0 if result.completed else 1
        elif args.command == "stage-staffing":
            with args.envelope.open("rb") as source:
                content = source.read(8193)
            if len(content) > 8192:
                raise DutyRefused("Staffing envelope exceeds its size limit")
            try:
                data = json.loads(content)
                authority = StaffingAuthority(**data["authority"])
                need = StaffingNeed(**data["need"])
            except (ValueError, TypeError, KeyError) as error:
                raise DutyRefused("Invalid staffing envelope") from error
            with hold_company_writer(args.store, args.company_id) as writer:
                revision = stage_staffing(writer, authority, need)
            print(json.dumps({"evidence_revision": revision}))
            return 0
        elif args.command == "stage-assessment-source":
            with args.records.open("rb") as source:
                content = source.read(64_001)
            if len(content) > 64_000:
                raise DutyRefused("Assessment source exceeds its size limit")
            try:
                records = tuple(SourceRecord(**item) for item in json.loads(content))
            except (ValueError, TypeError, KeyError) as error:
                raise DutyRefused("Invalid company assessment source") from error
            with hold_company_writer(args.store, args.company_id) as writer:
                revision = seal_assessment_source(writer, records)
            print(json.dumps({"source_revision": revision}))
            return 0
        elif args.command == "_assessment-worker":
            result = run_assessment_company(
                args.store,
                args.company_id,
                lambda: EmployeeProcess(args.employee),
                turn_budget=args.turn_budget,
                idle_seconds=args.idle_seconds,
                budget_from=args.budget_from,
                store_key=args.store_key,
                on_ready=lambda lease: announce_ready(args.ready_fd, lease),
            )
        elif args.command == "_hired-paper-worker":
            result = run_hired_paper_company(
                args.store,
                args.company_id,
                _paper_destination(args),
                lambda: EmployeeProcess(args.employee),
                turn_budget=args.turn_budget,
                idle_seconds=args.idle_seconds,
                budget_from=args.budget_from,
                store_key=args.store_key,
                on_ready=lambda lease: announce_ready(args.ready_fd, lease),
            )
        elif args.command == "operate-hiring":
            result = run_hiring(
                args.store,
                args.company_id,
                EmployeeProcess(args.employee),
                first_purpose=args.first_purpose,
            )
        else:
            inputs = (
                args.store,
                args.company_id,
                _paper_destination(args),
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
                    sampler=_paper_sampler(
                        args, args.journal, managed=args.managed_engine
                    ),
                )
            else:
                result = run_paper_company(
                    *inputs,
                    repeat_paper=args.repeat_paper,
                    turn_budget=args.turn_budget,
                    idle_seconds=args.idle_seconds,
                    sampler=_paper_sampler(
                        args, args.journal, managed=args.managed_engine
                    ),
                )
    except (
        FoundingRefused,
        BindingRefused,
        DutyRefused,
        TradeRefused,
        EngineRefused,
        MarketRefused,
        OrderRefused,
        PostingRefused,
        VenueRefused,
        SupervisionRefused,
        WriterRefused,
        ValuationRefused,
        WindowRefused,
        sqlite3.Error,
        OSError,
    ) as error:
        print(f"Company command refused: {error}", file=sys.stderr)
        return 1
    fields = (
        _agent_receipt
        if args.command
        in ("operate-hiring", "_assessment-worker", "_hired-paper-worker")
        else dict
    )
    print(json.dumps(asdict(result, dict_factory=fields)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
