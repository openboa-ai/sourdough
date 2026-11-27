"""Trading domain CLI subcommands contributed to Core sourdough.cli."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from time import time_ns
from uuid import uuid4

from ..duty_run import DutyRefused, count_company_runs
from ..runtime.domains import DomainCliRefused
from ..runtime.work_kernel import (
    WorkKernelRefused,
    create_work_item,
    register_responsibility,
    set_condition,
)
from ..storage.db import open_writer, write_tx
from ..storage.schema import enable_domain_v1
from ..supervisor import SupervisionRefused, supervise_worker
from ..writer_lease import hold_company_writer
from .accounts import open_company_paper_book, register_live_candidate_binding
from .binance_emulator import BinanceWireEmulator
from .binance_venue import (
    MARKET_SPOT,
    MARKET_USDM_FUTURES,
    BinanceVenueAdapter,
    BinanceVenueConfig,
)
from .credentials import (
    CredentialRefused,
    InMemoryTestCredentialProvider,
)
from .domain import ENV_EXTERNAL_TEST, ExecutionTierRefused
from .engine import EngineRefused, prepare_engine
from .execution import BindingRefused, VenueRefused
from .external_venue import (
    ExternalEnvironmentMismatch,
    ExternalOrderReplyUnknown,
    ExternalVenueRefused,
)
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
from .venue_qualification import run_binance_testnet_qualification
from .venue_resolver import (
    CompositeCredentialProvider,
    build_default_live_venue_resolver,
    run_live_binance_testnet_qualification,
)
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
        "enable-trading-domain",
        "open-paper-book",
        "register-live-candidate",
        "qualify-live-venue",
        "arm-trading-schedule",
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
    rec_cmd.add_argument("--lease-dir", type=Path)
    rec_cmd.add_argument(
        "--quote-source",
        help="Optional read-only quote source: binance-spot, binance-futures, or journal:<path>",
    )
    rec_cmd.add_argument("--max-quote-age-seconds", type=int, default=60)
    rec_cmd.add_argument(
        "--live-reconcile",
        action="store_true",
        help="Resolve active company_live bindings via host credentials to reconcile external orders and drift",
    )

    enable_cmd = commands.add_parser(
        "enable-trading-domain",
        help="Idempotently install the canonical v1 trading domain schema pack on a Core Company store",
    )
    enable_cmd.add_argument("store", type=Path)
    enable_cmd.add_argument("--company-id", required=True)
    enable_cmd.add_argument("--lease-dir", type=Path)

    paper_cmd = commands.add_parser(
        "open-paper-book",
        help="Open a new isolated Company Paper Book for concurrent strategy soak testing",
    )
    paper_cmd.add_argument("store", type=Path)
    paper_cmd.add_argument("--company-id", required=True)
    paper_cmd.add_argument("--lease-dir", type=Path)
    paper_cmd.add_argument("--account-id", required=True)
    paper_cmd.add_argument("--book-name", required=True)
    paper_cmd.add_argument("--starting-minor", required=True, type=int)
    paper_cmd.add_argument("--exposure-limit-minor", required=True, type=int)
    paper_cmd.add_argument("--currency", default="USD")
    paper_cmd.add_argument("--strategy-release")
    paper_cmd.add_argument("--instrument-rules-digest", default="btc-usdt-spot-v1")
    paper_cmd.add_argument("--fee-bps", type=int, default=4)
    paper_cmd.add_argument("--slippage-bps", type=int, default=2)

    live_cand_cmd = commands.add_parser(
        "register-live-candidate",
        help="Register a dormant Tier 3 company_live candidate binding with an opaque host credential handle",
    )
    live_cand_cmd.add_argument("store", type=Path)
    live_cand_cmd.add_argument("--company-id", required=True)
    live_cand_cmd.add_argument("--lease-dir", type=Path)
    live_cand_cmd.add_argument("--binding-id", required=True)
    live_cand_cmd.add_argument("--account-id", required=True)
    live_cand_cmd.add_argument("--destination-id", required=True)
    live_cand_cmd.add_argument("--connector-revision", default="binance-rest-v1")
    live_cand_cmd.add_argument("--instrument-rules-digest", required=True)
    live_cand_cmd.add_argument("--starting-minor", required=True, type=int)
    live_cand_cmd.add_argument("--exposure-limit-minor", required=True, type=int)
    live_cand_cmd.add_argument("--credential-handle", required=True)
    live_cand_cmd.add_argument("--currency", default="USD")
    live_cand_cmd.add_argument("--fee-bps", type=int, default=4)

    qual_cmd = commands.add_parser(
        "qualify-live-venue",
        help="Run external_test venue qualification (emulator or live Binance Testnet) and seal a receipt",
    )
    qual_cmd.add_argument("store", type=Path)
    qual_cmd.add_argument("--company-id", required=True)
    qual_cmd.add_argument("--lease-dir", type=Path)
    qual_cmd.add_argument("--instrument-rules-digest", required=True)
    qual_cmd.add_argument(
        "--mode", choices=("emulator", "binance-testnet"), default="emulator"
    )
    qual_cmd.add_argument(
        "--market", choices=(MARKET_SPOT, MARKET_USDM_FUTURES), default=MARKET_SPOT
    )
    qual_cmd.add_argument("--symbol", default="BTCUSDT")
    qual_cmd.add_argument("--account-id", default="binance-testnet-qual")
    qual_cmd.add_argument("--destination-id", default="binance-spot-testnet")
    qual_cmd.add_argument("--credential-handle", default="envref:BINANCE_TESTNET")
    qual_cmd.add_argument("--receipt-id")

    sched_cmd = commands.add_parser(
        "arm-trading-schedule",
        help="Register standing autonomous trading responsibilities and arm initial wake timers",
    )
    sched_cmd.add_argument("store", type=Path)
    sched_cmd.add_argument("--company-id", required=True)
    sched_cmd.add_argument("--lease-dir", type=Path)
    sched_cmd.add_argument("--agent-id", required=True)
    sched_cmd.add_argument("--decision-delay", type=int, default=300)
    sched_cmd.add_argument("--reconcile-delay", type=int, default=60)
    sched_cmd.add_argument(
        "--enqueue-now",
        action="store_true",
        help="Also enqueue an immediate due quant.paper_decision shift",
    )


def handle_trading_cli_command(args: argparse.Namespace) -> int | None:
    if args.command not in TRADING_CLI_COMMANDS:
        return None
    try:
        return _dispatch_trading_command(args)
    except (
        BindingRefused,
        CredentialRefused,
        DutyRefused,
        EngineRefused,
        ExecutionTierRefused,
        ExternalEnvironmentMismatch,
        ExternalOrderReplyUnknown,
        ExternalVenueRefused,
        MarketRefused,
        QuoteRefused,
        ReturnRefused,
        SupervisionRefused,
        ValuationRefused,
        VenueRefused,
        WindowRefused,
        WorkKernelRefused,
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
        resolver = (
            build_default_live_venue_resolver(args.store, args.company_id)
            if args.live_reconcile
            else None
        )
        with hold_company_writer(
            args.store, args.company_id, lease_directory=args.lease_dir
        ) as writer:
            summary = reconcile_trading_books(
                writer,
                quote_provider=provider,
                live_venue_resolver=resolver,
                max_quote_age_ns=args.max_quote_age_seconds * 10**9,
            )
        print(json.dumps(summary, sort_keys=True))
        return 0
    elif args.command == "enable-trading-domain":
        with hold_company_writer(
            args.store, args.company_id, lease_directory=args.lease_dir
        ) as writer:
            with closing(open_writer(args.store, lease=writer)) as db, write_tx(db):
                installed_now = enable_domain_v1(
                    db,
                    "trading",
                    company_id=args.company_id,
                    actor_id="operator_cli",
                )
        print(
            json.dumps(
                {
                    "company_id": args.company_id,
                    "domain": "trading",
                    "installed_now": installed_now,
                },
                sort_keys=True,
            )
        )
        return 0
    elif args.command == "open-paper-book":
        with hold_company_writer(
            args.store, args.company_id, lease_directory=args.lease_dir
        ) as writer:
            binding = open_company_paper_book(
                writer,
                account_id=args.account_id,
                book_name=args.book_name,
                strategy_release_id=args.strategy_release,
                instrument_rules_digest=args.instrument_rules_digest,
                currency=args.currency,
                starting_minor_units=args.starting_minor,
                exposure_limit_minor_units=args.exposure_limit_minor,
                fee_bps=args.fee_bps,
                slippage_bps=args.slippage_bps,
            )
        print(json.dumps(asdict(binding), sort_keys=True))
        return 0
    elif args.command == "register-live-candidate":
        with hold_company_writer(
            args.store, args.company_id, lease_directory=args.lease_dir
        ) as writer:
            binding = register_live_candidate_binding(
                writer,
                binding_id=args.binding_id,
                account_id=args.account_id,
                destination_id=args.destination_id,
                connector_revision=args.connector_revision,
                instrument_rules_digest=args.instrument_rules_digest,
                currency=args.currency,
                starting_minor_units=args.starting_minor,
                exposure_limit_minor_units=args.exposure_limit_minor,
                credential_handle_ref=args.credential_handle,
                fee_bps=args.fee_bps,
            )
        print(json.dumps(asdict(binding), sort_keys=True))
        return 0
    elif args.command == "qualify-live-venue":
        with hold_company_writer(
            args.store, args.company_id, lease_directory=args.lease_dir
        ) as writer:
            if args.mode == "emulator":
                emu_key = f"qual-key-{uuid4().hex[:8]}"
                emu_secret = f"qual-sec-{uuid4().hex[:12]}"
                emu_handle = "envref:BINANCE_TESTNET_EMU"
                emulator = BinanceWireEmulator(
                    api_key=emu_key,
                    api_secret=emu_secret,
                    starting_usdt=Decimal("500000"),
                    fee_bps=4,
                    max_order_notional=Decimal("100000"),
                )
                emulator.start()
                try:
                    adapter = BinanceVenueAdapter(
                        BinanceVenueConfig(
                            environment=ENV_EXTERNAL_TEST,
                            market_kind=args.market,
                            account_id=args.account_id,
                            destination_id=args.destination_id,
                            credential_handle_ref=emu_handle,
                            base_url_override=emulator.base_url,
                            allow_loopback_override=True,
                        ),
                        InMemoryTestCredentialProvider(
                            {emu_handle: (emu_key, emu_secret)}
                        ),
                    )
                    report = run_binance_testnet_qualification(
                        writer,
                        adapter=adapter,
                        emulator=emulator,
                        instrument_rules_digest=args.instrument_rules_digest,
                        symbol=args.symbol,
                        receipt_id=args.receipt_id,
                    )
                finally:
                    emulator.stop()
            else:
                adapter = BinanceVenueAdapter(
                    BinanceVenueConfig(
                        environment=ENV_EXTERNAL_TEST,
                        market_kind=args.market,
                        account_id=args.account_id,
                        destination_id=args.destination_id,
                        credential_handle_ref=args.credential_handle,
                    ),
                    CompositeCredentialProvider(),
                )
                report = run_live_binance_testnet_qualification(
                    writer,
                    adapter=adapter,
                    instrument_rules_digest=args.instrument_rules_digest,
                    symbol=args.symbol,
                    receipt_id=args.receipt_id,
                )
        payload = {
            "mode": args.mode,
            "receipt": asdict(report.receipt),
            "cases": [asdict(c) for c in report.cases],
        }
        print(json.dumps(payload, sort_keys=True))
        return 0 if report.receipt.verdict == "PASS" else 1
    elif args.command == "arm-trading-schedule":
        if not 60 <= args.decision_delay <= 86400:
            raise ExecutionTierRefused(
                "--decision-delay must be between 60 and 86400 seconds"
            )
        if not 15 <= args.reconcile_delay <= 86400:
            raise ExecutionTierRefused(
                "--reconcile-delay must be between 15 and 86400 seconds"
            )
        now = time_ns()
        with hold_company_writer(
            args.store, args.company_id, lease_directory=args.lease_dir
        ) as writer:
            with closing(open_writer(args.store, lease=writer)) as db, write_tx(db):
                enable_domain_v1(
                    db,
                    "trading",
                    company_id=args.company_id,
                    actor_id="operator_cli",
                )
                armed: list[dict[str, object]] = []
                for duty_kind, delay_sec in (
                    ("quant.paper_decision", args.decision_delay),
                    ("quant.reconcile", args.reconcile_delay),
                ):
                    row = db.execute(
                        """SELECT responsibility_id FROM work_responsibilities
                           WHERE company_id = ? AND owner_agent_id = ?
                             AND duty_kind = ? AND status = 'active'
                           LIMIT 1""",
                        (args.company_id, args.agent_id, duty_kind),
                    ).fetchone()
                    if row is not None:
                        resp_id = str(row[0])
                    else:
                        resp = register_responsibility(
                            db,
                            company_id=args.company_id,
                            owner_agent_id=args.agent_id,
                            duty_kind=duty_kind,
                            remit=f"Standing operator trading shift ({duty_kind})",
                            readable_scope=["trading_books", "market_quotes"],
                            now_ns=now,
                        )
                        resp_id = resp.responsibility_id
                    cond = set_condition(
                        db,
                        company_id=args.company_id,
                        owner_agent_id=args.agent_id,
                        responsibility_id=resp_id,
                        trigger_kind="timer",
                        due_at_ns=now + delay_sec * 1_000_000_000,
                        now_ns=now,
                    )
                    armed.append(
                        {
                            "duty_kind": duty_kind,
                            "responsibility_id": resp_id,
                            "condition_id": cond.condition_id,
                            "due_at_ns": cond.due_at_ns,
                        }
                    )
                immediate_work_id: str | None = None
                if args.enqueue_now:
                    item, _created = create_work_item(
                        db,
                        company_id=args.company_id,
                        owner_agent_id=args.agent_id,
                        duty_kind="quant.paper_decision",
                        source_kind="schedule",
                        source_id="operator-arm-now",
                        dedupe_key=f"operator-arm-{uuid4().hex[:12]}",
                        now_ns=now,
                    )
                    immediate_work_id = item.work_item_id
        print(
            json.dumps(
                {
                    "company_id": args.company_id,
                    "agent_id": args.agent_id,
                    "armed_conditions": armed,
                    "immediate_work_item_id": immediate_work_id,
                },
                sort_keys=True,
            )
        )
        return 0
    else:
        raise DomainCliRefused(f"Unsupported trading CLI command: {args.command}")
    print(json.dumps(asdict(result)))
    return 0
