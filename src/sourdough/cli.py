"""Local entry point for explicit setup and reopening."""

import argparse
import json
import os
import signal
import sqlite3
import stat
import sys
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from time import sleep

from sourdough.accounting_close import CloseRefused
from sourdough.agent_runtime import (
    AgentProfileBinding,
    AgentRuntimeRefused,
    build_default_adapter_registry,
)
from sourdough.assessment_run import run_assessment_company
from sourdough.board_control import (
    BoardControlRefused,
    bind_board_principal,
    read_local_command_receipt_status,
)
from sourdough.company import FoundingRefused, FoundingTerms, found, reopen
from sourdough.company_assessment import SourceRecord, seal_assessment_source
from sourdough.company_checkpoint import (
    CheckpointRefused,
    checkpoint_company,
    inspect_company_checkpoint,
)
from sourdough.duty_run import DutyRefused, count_company_runs
from sourdough.employee_process import EmployeeProcess, EmployeeProcessRefused
from sourdough.general_ledger import JournalRefused
from sourdough.hiring_run import run_hiring
from sourdough.ipc import ClientWebBridge, CompanyDaemon, ControlPlaneClient, IpcRefused
from sourdough.runtime import (
    DomainCliRefused,
    ResidentCompanyService,
    build_production_duty_registry,
    build_store_profile_lookup,
    load_domain_plugins,
    merge_work_policies,
)
from sourdough.runtime.registry import RuntimeRefused
from sourdough.runtime.work_kernel import WorkKernelRefused
from sourdough.staffing import StaffingAuthority, StaffingNeed, stage_staffing
from sourdough.storage import migrate_v1_store_to_canonical
from sourdough.storage.db import StorageRefused
from sourdough.supervisor import SupervisionRefused, announce_ready, supervise_worker
from sourdough.work_projection import WORK_TIMELINE_REVISION, read_local_board_timeline
from sourdough.writer_lease import WriterRefused, hold_company_writer


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


def _validate_ready_fd(ready_fd: int | None) -> None:
    if ready_fd is None:
        return
    if ready_fd < 3:
        raise IpcRefused("ready-fd must be an inherited descriptor >= 3")
    try:
        fd_info = os.fstat(ready_fd)
    except OSError as error:
        raise IpcRefused("ready-fd is not an open file descriptor") from error
    if not (stat.S_ISFIFO(fd_info.st_mode) or stat.S_ISREG(fd_info.st_mode)):
        raise IpcRefused("ready-fd must refer to a pipe or regular file")


def _emit_event(event: dict) -> None:
    print(json.dumps(event), file=sys.stderr, flush=True)


def _start_supervisor(args, *, emit=_emit_event):
    if args.command != "supervise-assessment":
        raise DutyRefused(f"Unsupported supervision command: {args.command}")
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


def _start_company_supervisor(args, *, emit=_emit_event):
    if args.command != "supervise-company":
        raise DutyRefused(f"Unsupported supervision command: {args.command}")
    command = [
        sys.executable,
        "-I",
        "-m",
        "sourdough.cli",
        "run-company",
        str(args.store),
        "--company-id",
        args.company_id,
        "--tick-interval",
        str(args.tick_interval),
    ]
    if args.profiles_json is not None:
        command.extend(["--profiles-json", str(args.profiles_json)])
    if args.socket_dir is not None:
        command.extend(["--socket-dir", str(args.socket_dir)])
    if args.socket is not None:
        command.extend(["--socket", str(args.socket)])
    if args.lease_dir is not None:
        command.extend(["--lease-dir", str(args.lease_dir)])
    if args.engine_dir is not None:
        command.extend(["--engine-dir", str(args.engine_dir)])
    if args.evaluator_store is not None:
        command.extend(["--evaluator-store", str(args.evaluator_store)])
    if args.web_port is not None:
        command.extend(["--web-port", str(args.web_port)])
    if args.static_dir is not None:
        command.extend(["--static-dir", str(args.static_dir)])
    if args.principal is not None:
        command.extend(["--principal", args.principal])
    if args.once:
        command.append("--once")
    return supervise_worker(
        command,
        args.store,
        args.company_id,
        max_restarts=args.max_restarts,
        startup_seconds=args.startup_seconds,
        max_seconds=args.max_seconds,
        emit=emit,
    )


def main() -> int:
    plugins = load_domain_plugins()
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
    migrate_cmd = commands.add_parser(
        "migrate-store",
        help="Atomically upgrade a pre-cleanup v1 company store to canonical v1 schema",
    )
    migrate_cmd.add_argument("store", type=Path)
    checkpoint = commands.add_parser("checkpoint-company")
    checkpoint.add_argument("store", type=Path)
    checkpoint.add_argument("archive", type=Path)
    checkpoint.add_argument("--company-id", required=True)
    checkpoint_read = commands.add_parser("inspect-company-checkpoint")
    checkpoint_read.add_argument("archive", type=Path)
    checkpoint_read.add_argument("--company-id", required=True)
    inspection = commands.add_parser(
        "inspect-work", help="Inspect the local read-only company timeline"
    )
    inspection.add_argument("store", type=Path)
    inspection.add_argument("--company-id", required=True)
    inspection.add_argument("--contract-revision", default=WORK_TIMELINE_REVISION)
    receipt_read = commands.add_parser(
        "read-command-receipt",
        help="Read the local status of a previously submitted board command receipt",
    )
    receipt_read.add_argument("store", type=Path)
    receipt_read.add_argument("--company-id", required=True)
    receipt_read.add_argument("--command-id", required=True)
    receipt_read.add_argument("--operation")
    daemon_cmd = commands.add_parser(
        "serve-company-daemon",
        help="Run the resident single-writer CompanyDaemon over a local Unix socket",
    )
    daemon_cmd.add_argument("store", type=Path)
    daemon_cmd.add_argument("--company-id", required=True)
    daemon_cmd.add_argument("--socket-dir", type=Path)
    daemon_cmd.add_argument("--socket", type=Path)
    daemon_cmd.add_argument("--lease-dir", type=Path)
    daemon_cmd.add_argument("--engine-dir", type=Path)
    daemon_cmd.add_argument("--evaluator-store", type=Path)
    daemon_cmd.add_argument("--store-key", type=_store_key)
    daemon_cmd.add_argument("--ready-fd", type=int)

    run_company_cmd = commands.add_parser(
        "run-company",
        help="Run the unified resident Sourdough company (UDS daemon + autonomous scheduler + optional web UI)",
    )
    run_company_cmd.add_argument("store", type=Path)
    run_company_cmd.add_argument("--company-id", required=True)
    run_company_cmd.add_argument("--profiles-json", type=Path)
    run_company_cmd.add_argument("--socket-dir", type=Path)
    run_company_cmd.add_argument("--socket", type=Path)
    run_company_cmd.add_argument("--lease-dir", type=Path)
    run_company_cmd.add_argument("--engine-dir", type=Path)
    run_company_cmd.add_argument("--evaluator-store", type=Path)
    run_company_cmd.add_argument("--store-key", type=_store_key)
    run_company_cmd.add_argument("--tick-interval", type=float, default=1.0)
    run_company_cmd.add_argument("--web-port", type=int)
    run_company_cmd.add_argument("--static-dir", type=Path)
    run_company_cmd.add_argument("--principal")
    run_company_cmd.add_argument("--ready-fd", type=int)
    run_company_cmd.add_argument("--once", action="store_true")

    supervise_company_cmd = commands.add_parser(
        "supervise-company",
        help="Supervise the resident Sourdough company worker with bounded restarts",
    )
    supervise_company_cmd.add_argument("store", type=Path)
    supervise_company_cmd.add_argument("--company-id", required=True)
    supervise_company_cmd.add_argument("--profiles-json", type=Path)
    supervise_company_cmd.add_argument("--socket-dir", type=Path)
    supervise_company_cmd.add_argument("--socket", type=Path)
    supervise_company_cmd.add_argument("--lease-dir", type=Path)
    supervise_company_cmd.add_argument("--engine-dir", type=Path)
    supervise_company_cmd.add_argument("--evaluator-store", type=Path)
    supervise_company_cmd.add_argument("--tick-interval", type=float, default=1.0)
    supervise_company_cmd.add_argument("--web-port", type=int)
    supervise_company_cmd.add_argument("--static-dir", type=Path)
    supervise_company_cmd.add_argument("--principal")
    supervise_company_cmd.add_argument("--once", action="store_true")
    supervise_company_cmd.add_argument("--max-restarts", type=int, default=1)
    supervise_company_cmd.add_argument("--startup-seconds", type=int, default=35)
    supervise_company_cmd.add_argument("--max-seconds", type=int, default=360)

    web_cmd = commands.add_parser(
        "client-web",
        help="Serve the authenticated loopback Board Client UI over the local CompanyDaemon UDS",
    )
    web_cmd.add_argument("--socket", required=True, type=Path)
    web_cmd.add_argument("--port", type=int, default=0)
    web_cmd.add_argument("--static-dir", type=Path)
    web_cmd.add_argument("--pending-dir", type=Path)
    web_cmd.add_argument("--principal")
    web_cmd.add_argument("--timeout-seconds", type=float, default=10.0)

    bind_board_cmd = commands.add_parser(
        "bind-board",
        help="Bind a Board principal role (board_viewer or board_control) in the company store",
    )
    bind_board_cmd.add_argument("store", type=Path)
    bind_board_cmd.add_argument("--company-id", required=True)
    bind_board_cmd.add_argument("--principal-id", required=True)
    bind_board_cmd.add_argument(
        "--role", choices=("board_viewer", "board_control"), default="board_control"
    )
    bind_board_cmd.add_argument("--scope-ref", default="company:all")

    bridge_cmd = commands.add_parser(
        "client-bridge",
        help="Talk to the local CompanyDaemon over its UDS control plane",
    )
    bridge_cmd.add_argument("--socket", required=True, type=Path)
    bridge_cmd.add_argument("--timeout-seconds", type=float, default=10.0)
    bridge_ops = bridge_cmd.add_subparsers(dest="bridge_command", required=True)
    bridge_ops.add_parser("meta")
    bridge_work = bridge_ops.add_parser("inspect-work")
    bridge_work.add_argument("--company-id", required=True)
    bridge_work.add_argument("--contract-revision", default=WORK_TIMELINE_REVISION)
    bridge_work.add_argument("--if-none-match")
    bridge_receipt = bridge_ops.add_parser("read-command-receipt")
    bridge_receipt.add_argument("--company-id", required=True)
    bridge_receipt.add_argument("--command-id", required=True)
    bridge_receipt.add_argument("--operation")
    bridge_events = bridge_ops.add_parser("events")
    bridge_events.add_argument("--company-id")
    bridge_events.add_argument("--after-seq", type=int, default=0)
    bridge_events.add_argument("--limit", type=int, default=100)
    bridge_events.add_argument("--wait-ms", type=int, default=0)
    profile = commands.add_parser("employee-profile")
    profile.add_argument("executable", type=Path)
    profile.add_argument("--sandbox", help="User-selected sandbox backend")
    staffing = commands.add_parser("stage-staffing")
    staffing.add_argument("store", type=Path)
    staffing.add_argument("envelope", type=Path)
    staffing.add_argument("--company-id", required=True)
    source = commands.add_parser("stage-assessment-source")
    source.add_argument("store", type=Path)
    source.add_argument("records", type=Path)
    source.add_argument("--company-id", required=True)
    for command in ("supervise-assessment", "_assessment-worker"):
        managed = commands.add_parser(command)
        managed.add_argument("store", type=Path)
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
    hiring.add_argument("--first-purpose", required=True)
    for plugin in plugins:
        plugin.register_cli_subcommands(commands)
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
        elif args.command == "migrate-store":
            domains = migrate_v1_store_to_canonical(args.store)
            print(json.dumps({"store": str(args.store), "domains": sorted(domains)}))
            return 0
        elif args.command == "checkpoint-company":
            result = checkpoint_company(args.store, args.company_id, args.archive)
        elif args.command == "inspect-company-checkpoint":
            result = inspect_company_checkpoint(args.archive, args.company_id)
        elif args.command == "inspect-work":
            print(
                json.dumps(
                    read_local_board_timeline(
                        args.store,
                        args.company_id,
                        contract_revision=args.contract_revision,
                        domain_plugins=plugins,
                    )
                )
            )
            return 0
        elif args.command == "read-command-receipt":
            print(
                json.dumps(
                    read_local_command_receipt_status(
                        args.store,
                        args.company_id,
                        args.command_id,
                        operation=args.operation,
                    )
                )
            )
            return 0
        elif args.command == "serve-company-daemon":
            _validate_ready_fd(args.ready_fd)

            stop_requested = False

            def _request_stop(_signum, _frame):
                nonlocal stop_requested
                stop_requested = True

            previous_sigint = signal.signal(signal.SIGINT, _request_stop)
            previous_sigterm = signal.signal(signal.SIGTERM, _request_stop)
            previous_sighup = signal.signal(signal.SIGHUP, _request_stop)
            try:
                with CompanyDaemon(
                    args.store,
                    args.company_id,
                    socket_dir=args.socket_dir,
                    socket_path=args.socket,
                    lease_directory=args.lease_dir,
                    expected_store_key=args.store_key,
                    engine_directory=args.engine_dir,
                    evaluator_store=args.evaluator_store,
                    domain_plugins=plugins,
                ) as daemon:
                    announcement = json.dumps(asdict(daemon.info), sort_keys=True)
                    if args.ready_fd is not None:
                        with os.fdopen(args.ready_fd, "w", encoding="utf-8") as ready:
                            ready.write(announcement + "\n")
                            ready.flush()
                    else:
                        print(announcement, flush=True)
                    while not stop_requested:
                        sleep(0.25)
            finally:
                signal.signal(signal.SIGINT, previous_sigint)
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGHUP, previous_sighup)
            return 0
        elif args.command == "run-company":
            _validate_ready_fd(args.ready_fd)

            profile_bindings: dict[str, AgentProfileBinding] = {}
            if args.profiles_json is not None:
                try:
                    raw_profiles = json.loads(
                        args.profiles_json.read_text(encoding="utf-8")
                    )
                    if not isinstance(raw_profiles, dict):
                        raise RuntimeRefused("profiles-json must be a JSON object")
                    for agent_id, spec in raw_profiles.items():
                        if not isinstance(spec, dict):
                            raise RuntimeRefused(
                                f"Invalid profile binding for agent '{agent_id}'"
                            )
                        profile_bindings[str(agent_id)] = AgentProfileBinding(**spec)
                except (ValueError, TypeError) as err:
                    raise RuntimeRefused(f"Invalid profiles-json: {err}") from err

            adapter_registry = build_default_adapter_registry()
            profile_lookup = build_store_profile_lookup(
                args.store, args.company_id, overrides=profile_bindings
            )
            duty_registry = build_production_duty_registry(
                adapter_registry=adapter_registry,
                profile_bindings=profile_lookup,
                domain_plugins=plugins,
            )

            stop_requested = False

            def _request_resident_stop(_signum, _frame):
                nonlocal stop_requested
                stop_requested = True

            previous_sigint = signal.signal(signal.SIGINT, _request_resident_stop)
            previous_sigterm = signal.signal(signal.SIGTERM, _request_resident_stop)
            previous_sighup = signal.signal(signal.SIGHUP, _request_resident_stop)
            try:
                with ExitStack() as stack:
                    service = stack.enter_context(
                        ResidentCompanyService(
                            args.store,
                            args.company_id,
                            duty_registry,
                            socket_dir=args.socket_dir,
                            socket_path=args.socket,
                            lease_directory=args.lease_dir,
                            expected_store_key=args.store_key,
                            engine_directory=args.engine_dir,
                            evaluator_store=args.evaluator_store,
                            tick_interval_seconds=args.tick_interval,
                            domain_plugins=plugins,
                        )
                    )
                    payload = asdict(service.info)
                    if args.web_port is not None:
                        bridge = stack.enter_context(
                            ClientWebBridge(
                                service.socket_path,
                                port=args.web_port,
                                static_dir=args.static_dir,
                                principal=args.principal,
                            )
                        )
                        payload["web_url"] = bridge.info.url
                    announcement = json.dumps(payload, sort_keys=True)
                    if args.ready_fd is not None:
                        announce_ready(args.ready_fd, service.daemon.writer_lease)
                    else:
                        print(announcement, flush=True)
                    if args.once:
                        if not stop_requested:
                            single_tick_timeout = max(960.0, args.tick_interval + 960.0)
                            service.wait_for(
                                lambda h: stop_requested
                                or h.ticks_completed >= 1
                                or not h.running,
                                timeout_seconds=single_tick_timeout,
                            )
                    else:
                        while not stop_requested:
                            sleep(0.25)
            finally:
                signal.signal(signal.SIGINT, previous_sigint)
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGHUP, previous_sighup)
            return 0
        elif args.command == "client-web":
            stop_requested = False

            def _request_web_stop(_signum, _frame):
                nonlocal stop_requested
                stop_requested = True

            previous_sigint = signal.signal(signal.SIGINT, _request_web_stop)
            previous_sigterm = signal.signal(signal.SIGTERM, _request_web_stop)
            previous_sighup = signal.signal(signal.SIGHUP, _request_web_stop)
            try:
                with ClientWebBridge(
                    args.socket,
                    port=args.port,
                    static_dir=args.static_dir,
                    pending_dir=args.pending_dir,
                    principal=args.principal,
                    timeout_seconds=args.timeout_seconds,
                ) as bridge:
                    info = bridge.info
                    print(
                        json.dumps(
                            {
                                "host": info.host,
                                "port": info.port,
                                "url": info.url,
                                "socket_path": info.socket_path,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    while not stop_requested:
                        sleep(0.25)
            finally:
                signal.signal(signal.SIGINT, previous_sigint)
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGHUP, previous_sighup)
            return 0
        elif args.command == "bind-board":
            with hold_company_writer(args.store, args.company_id) as writer:
                result = bind_board_principal(
                    writer,
                    principal_id=args.principal_id,
                    role_name=args.role,
                    scope_ref=args.scope_ref,
                )
        elif args.command == "client-bridge":
            client = ControlPlaneClient(
                args.socket, timeout_seconds=args.timeout_seconds
            )
            if args.bridge_command == "meta":
                print(json.dumps(client.meta()))
                return 0
            if args.bridge_command == "inspect-work":
                status_code, etag, payload = client.work_timeline(
                    contract_revision=args.contract_revision,
                    if_none_match=args.if_none_match,
                    expected_company_id=args.company_id,
                )
                if status_code == 304:
                    print(
                        json.dumps(
                            {
                                "not_modified": True,
                                "company_id": args.company_id,
                                "etag": etag,
                            }
                        )
                    )
                    return 0
                assert payload is not None
                print(json.dumps(payload))
                return 0
            if args.bridge_command == "read-command-receipt":
                payload = client.command_receipt(
                    args.command_id, operation=args.operation
                )
                if payload.get("company_id") != args.company_id:
                    raise IpcRefused(
                        "HTTP 403: Control plane company identity mismatch"
                    )
                print(json.dumps(payload))
                return 0
            if args.bridge_command == "events":
                events = client.events(
                    after_seq=args.after_seq,
                    limit=args.limit,
                    wait_ms=args.wait_ms,
                    expected_company_id=args.company_id,
                )
                for event in events:
                    print(json.dumps(event, sort_keys=True))
                return 0
            raise IpcRefused("Unsupported client-bridge operation")
        elif args.command == "employee-profile":
            employee = EmployeeProcess(args.executable)
            if args.sandbox != employee.sandbox_backend:
                raise EmployeeProcessRefused(
                    "Select the sandbox backend declared by this employee adapter"
                )
            selection = {"execution_profile": employee.profile}
            if employee.sandbox_backend is not None:
                selection["sandbox_backend"] = employee.sandbox_backend
            print(json.dumps(selection))
            return 0
        elif args.command == "supervise-assessment":
            result = _start_supervisor(args)
            print(json.dumps(asdict(result)))
            return 0 if result.completed else 1
        elif args.command == "supervise-company":
            result = _start_company_supervisor(args)
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
        elif args.command == "operate-hiring":
            policies = [
                policy
                for plugin in plugins
                if (policy := plugin.work_policy()) is not None
            ]
            merged_policy = merge_work_policies(*policies)
            result = run_hiring(
                args.store,
                args.company_id,
                EmployeeProcess(args.employee),
                first_purpose=args.first_purpose,
                purpose_scopes=merged_policy.purpose_scopes,
            )
        else:
            for plugin in plugins:
                handled = plugin.handle_cli_command(args)
                if handled is not None:
                    return handled
            raise DutyRefused(f"Unsupported command: {args.command}")
    except (
        AgentRuntimeRefused,
        BoardControlRefused,
        CheckpointRefused,
        CloseRefused,
        DomainCliRefused,
        DutyRefused,
        EmployeeProcessRefused,
        FoundingRefused,
        IpcRefused,
        JournalRefused,
        RuntimeRefused,
        StorageRefused,
        SupervisionRefused,
        WorkKernelRefused,
        WriterRefused,
        sqlite3.Error,
        OSError,
    ) as error:
        print(f"Company command refused: {error}", file=sys.stderr)
        return 1
    fields = (
        _agent_receipt
        if args.command in ("operate-hiring", "_assessment-worker")
        else dict
    )
    print(json.dumps(asdict(result, dict_factory=fields)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
