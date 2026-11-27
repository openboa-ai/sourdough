"""Trading domain CLI subcommands contributed to Core sourdough.cli."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from time import time_ns

from ..duty_run import DutyRefused, count_company_runs
from ..runtime.domains import DomainCliRefused
from ..supervisor import SupervisionRefused, supervise_worker
from ..writer_lease import hold_company_writer
from .domain import ExecutionTierRefused
from .engine import EngineRefused, prepare_engine
from .execution import BindingRefused, VenueRefused
from .fund_returns import ReturnRefused
from .market_runtime import observe_market
from .market_worker import MarketRefused
from .nautilus_runtime import prepare_paper, supervise_paper
from .quotes import QuoteRefused, build_quote_provider_from_spec
from .reconcile import reconcile_trading_books
from .statement_store import (
    StatementRequest,
    read_retained_statement,
    retain_paper_statement,
)
from .valuation import ValuationRefused
from .window import WindowPlan, WindowRefused
from .window_run import run_window
from .window_sampler import WindowSampler, register_managed_window
from .window_statement import read_paper_statement

TRADING_CLI_COMMANDS = frozenset(
    {
        "seal-paper-window",
        "run-paper-window",
        "inspect-paper-window",
        "close-paper-window",
        "prepare-trading-engine",
        "observe-trading-market",
        "prepare-nautilus-paper",
        "serve-nautilus-paper",
        "reconcile-trading-books",
    }
)


def _emit_event(event: dict) -> None:
    print(json.dumps(event), file=sys.stderr, flush=True)


def _paper_sampler(args, directory: Path, *, managed: bool) -> WindowSampler | None:
    through = getattr(args, "observe_through_ns", None)
    if args.observe_window is None:
        if through is not None:
            raise WindowRefused("Observation cutoff requires a sealed window")
        return None
    if not managed:
        raise WindowRefused(
            "Only a managed current-market activation can sample a window"
        )
    return WindowSampler(directory, args.observe_window, through)


def _start_supervisor(args, *, emit=_emit_event):
    command = [
        sys.executable,
        "-I",
        "-m",
        "sourdough.cli",
        "_assessment-worker",
        str(args.store),
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
    return supervise_worker(
        command,
        args.store,
        args.company_id,
        max_restarts=args.max_restarts,
        startup_seconds=args.startup_seconds,
        max_seconds=args.max_seconds,
        emit=emit,
    )


def _run_paper_window(args):
    def company(through_ns, work_before_ns, max_seconds):
        options = argparse.Namespace(
            **(
                vars(args)
                | {
                    "command": "supervise-paper",
                    "journal": args.directory,
                    "managed_engine": True,
                    "observe_window": args.window_id,
                    "observe_through_ns": through_ns,
                    "start_work_before_ns": work_before_ns,
                    "idle_seconds": max(0, min(900, (through_ns - time_ns()) // 10**9)),
                    "startup_seconds": min(35, max_seconds),
                    "max_seconds": max_seconds,
                    "max_restarts": 1,
                }
            )
        )
        return _start_supervisor(
            options, emit=lambda event: _emit_event(event | {"actor": "company"})
        )

    return run_window(
        args.store,
        args.company_id,
        args.directory,
        args.window_id,
        company,
        segment_seconds=args.segment_seconds,
        emit=_emit_event,
    )


def register_trading_subcommands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    seal = commands.add_parser("seal-paper-window")
    seal.add_argument("store", type=Path)
    seal.add_argument("directory", type=Path)
    seal.add_argument("--company-id", required=True)
    seal.add_argument("--plan", required=True, type=Path)
    operating = commands.add_parser("run-paper-window")
    operating.add_argument("store", type=Path)
    operating.add_argument("directory", type=Path)
    operating.add_argument("--company-id", required=True)
    operating.add_argument("--employee", required=True, type=Path)
    operating.add_argument("--window-id", required=True)
    operating.add_argument("--segment-seconds", type=int, default=600)
    operating.add_argument("--turn-budget", type=int, choices=range(1, 9), default=8)
    operating.add_argument("--repeat-paper", action="store_true")
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
    for command in ("prepare-nautilus-paper", "serve-nautilus-paper"):
        sandbox = commands.add_parser(command)
        sandbox.add_argument("store", type=Path)
        sandbox.add_argument("directory", type=Path)
        sandbox.add_argument("--company-id", required=True)
        if command == "prepare-nautilus-paper":
            sandbox.add_argument("--account-id", required=True)
            sandbox.add_argument("--starting-atoms", required=True, type=int)
            sandbox.add_argument("--replay-tape", type=Path)
        else:
            sandbox.add_argument("--seconds", type=int, default=60)
            sandbox.add_argument("--replay-tape", type=Path)
    rec_cmd = commands.add_parser(
        "reconcile-trading-books",
        help="Run mark-to-market valuations and live external reconciliation on active trading books",
    )
    rec_cmd.add_argument("store", type=Path)
    rec_cmd.add_argument("--company-id", required=True)
    rec_cmd.add_argument(
        "--quote-source",
        help="Optional read-only quote source: binance-spot, binance-futures, or journal:<path>",
    )
    rec_cmd.add_argument("--max-quote-age-seconds", type=int, default=60)


def handle_trading_cli_command(args: argparse.Namespace) -> int | None:
    if args.command not in TRADING_CLI_COMMANDS:
        return None
    try:
        return _dispatch_trading_command(args)
    except (
        BindingRefused,
        DutyRefused,
        EngineRefused,
        ExecutionTierRefused,
        MarketRefused,
        QuoteRefused,
        ReturnRefused,
        SupervisionRefused,
        ValuationRefused,
        VenueRefused,
        WindowRefused,
    ) as error:
        raise DomainCliRefused(str(error)) from error


def _dispatch_trading_command(args: argparse.Namespace) -> int:
    if args.command == "run-paper-window":
        result = _run_paper_window(args)
        print(json.dumps(asdict(result)))
        return 0 if result.completed else 1
    if args.command == "inspect-paper-window":
        if args.report_id is not None:
            saved = read_retained_statement(args.store, args.company_id, args.report_id)
            if saved is None or saved.request.window_id != args.window_id:
                raise WindowRefused("No retained statement for the requested window")
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
    if args.command == "close-paper-window":
        request = StatementRequest(
            args.request_id, args.window_id, args.accounting_close, args.supersedes
        )
        with hold_company_writer(args.store, args.company_id) as writer:
            saved = retain_paper_statement(writer, args.directory, request)
        print(json.dumps(saved.as_receipt()))
        return 0
    if args.command == "seal-paper-window":
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
    elif args.command == "prepare-trading-engine":
        with hold_company_writer(args.store, args.company_id) as writer:
            result = prepare_engine(writer, args.directory, wheelhouse=args.wheelhouse)
    elif args.command == "observe-trading-market":
        result = observe_market(
            args.store,
            args.company_id,
            args.directory,
            symbol=args.symbol,
            max_age_seconds=args.max_age_seconds,
            samples=args.samples,
            emit=lambda event: print(json.dumps(event), file=sys.stderr, flush=True),
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
    elif args.command == "serve-nautilus-paper":
        result = supervise_paper(
            args.store,
            args.company_id,
            args.directory,
            seconds=args.seconds,
            tape_path=args.replay_tape,
            emit=lambda event: print(json.dumps(event), file=sys.stderr, flush=True),
        )
        print(json.dumps(asdict(result)))
        return 0 if result.completed else 1
    elif args.command == "reconcile-trading-books":
        if args.max_quote_age_seconds <= 0:
            raise QuoteRefused("--max-quote-age-seconds must be positive")
        provider = (
            build_quote_provider_from_spec(args.quote_source)
            if args.quote_source
            else None
        )
        with hold_company_writer(args.store, args.company_id) as writer:
            summary = reconcile_trading_books(
                writer,
                quote_provider=provider,
                max_quote_age_ns=args.max_quote_age_seconds * 10**9,
            )
        print(json.dumps(summary, sort_keys=True))
        return 0
    else:
        raise DomainCliRefused(f"Unsupported trading CLI command: {args.command}")
    print(json.dumps(asdict(result)))
    return 0
