"""Independently observe two actual founder duties on the managed paper venue."""

import argparse
import json
import os
import runpy
import signal
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path
from time import monotonic, sleep


OBSERVATION = runpy.run_path(str(Path(__file__).with_name("observe_paper.py")))
cli = OBSERVATION["cli"]
rows = OBSERVATION["rows"]
wait_ready = OBSERVATION["wait_ready"]


WITHHELD = """
import json, os, signal, sys
from pathlib import Path
from sourdough import cli
from sourdough.nautilus_paper import NautilusPaperVenue

barrier = Path(sys.argv.pop(1))
read_fills = NautilusPaperVenue.read_fills
def withhold(self, account_id, order_id, *, account_sync=False):
    fills = read_fills(self, account_id, order_id, account_sync=account_sync)
    if fills and not barrier.exists():
        payload = barrier.with_suffix(".new")
        with payload.open("x") as stream:
            json.dump({"pid": os.getpid(), "order_id": order_id}, stream)
        payload.rename(barrier)
        signal.pause()
    return fills
NautilusPaperVenue.read_fills = withhold
raise SystemExit(cli.main())
"""

FAULT_SUPERVISOR = """
import sys
from sourdough import cli
marker, child = sys.argv[1:3]
del sys.argv[1:3]
supervise = cli.supervise_worker
def inject(command, store, company_id, **options):
    instrumented = [sys.executable, "-IB", "-c", child, marker, *command[4:]]
    return supervise(instrumented, store, company_id, **options)
cli.supervise_worker = inject
raise SystemExit(cli.main())
"""


def run(*arguments):
    return [sys.executable, "-IB", "-m", "sourdough.cli", *map(str, arguments)]


def cut_original_worker(service, marker, store, journal, events_path):
    deadline = monotonic() + 75
    while service.poll() is None and monotonic() < deadline:
        if marker.exists():
            cut = json.loads(marker.read_text())
            break
        sleep(0.05)
    else:
        raise RuntimeError("Company never reached the independent fill barrier")
    events = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.startswith('{"event": "worker_ready"')
    ]
    assert len(events) == 1 and events[0]["pid"] == cut["pid"]
    [(original, fill)] = rows(
        journal, "SELECT order_id,fill_id FROM sandbox_fill_observation"
    )
    assert cut["order_id"] == original
    if rows(store, "SELECT name FROM sqlite_schema WHERE name='paper_fill'"):
        assert not rows(store, "SELECT fill_id FROM paper_fill")
    os.kill(cut["pid"], signal.SIGKILL)
    return {**cut, "fill_id": fill}


def observe(
    output: Path,
    employee: Path,
    wheelhouse: Path | None,
    *,
    company_cut: bool = False,
) -> dict:
    checkout = Path(__file__).resolve().parents[3]
    installed = Path(find_spec("sourdough").origin).parent
    for source in (checkout / "src/sourdough").iterdir():
        if source.suffix in {".py", ".lock"}:
            assert (installed / source.name).read_bytes() == source.read_bytes(), source
    assert employee.read_bytes() == (checkout / "tools/codex_employee.py").read_bytes()
    revision, tree = subprocess.check_output(
        ["git", "-C", checkout, "rev-parse", "HEAD", "HEAD^{tree}"], text=True
    ).splitlines()
    output.mkdir(mode=0o700)
    store, engine = output / "company.sqlite", output / "engine"
    profile = cli("employee-profile", employee)["execution_profile"]
    company = cli(
        "found",
        store,
        "--setup-identity",
        "managed-founder",
        "--charter-revision",
        "paper-operation",
        "--founder-scope",
        "paper",
        "--execution-profile",
        profile,
    )
    scope = ("--company-id", company["company_id"])
    wheel = () if wheelhouse is None else ("--wheelhouse", wheelhouse)
    installation = cli(
        "prepare-trading-engine", store, engine, *scope, *wheel, timeout=420
    )
    market = cli("observe-trading-market", store, engine, *scope, "--symbol", "BTCUSDT")
    assert market["completed"]
    plan = cli(
        "prepare-nautilus-paper",
        store,
        engine,
        *scope,
        "--account-id",
        "founder-paper",
        "--starting-atoms",
        100_000_000_000,
    )
    journal = (
        engine / ("nautilus-" + installation["distribution_version"]) / "sandbox.sqlite"
    )
    engine_log = output / "engine.stderr"
    cut = None
    with engine_log.open("w") as events, (output / "engine.stdout").open("w") as stdout:
        worker = subprocess.Popen(
            run("serve-nautilus-paper", store, engine, *scope, "--seconds", 180),
            stdout=stdout,
            stderr=events,
            start_new_session=True,
        )
        try:
            ready = wait_ready(worker, engine_log)
            binding = cli(
                "bind-nautilus-paper",
                store,
                engine,
                *scope,
                "--revision",
                "founder-paper-1",
                "--charter-revision",
                "paper-operation",
                "--exposure-atoms",
                10_000_000_000,
            )
            account = binding["account"]
            assert account["destination_id"] == plan["destination_id"]
            assert account["environment"] == "paper"
            assert account["connector_revision"] == "nautilus-spot-sandbox-v1"
            service_events = output / "company.stderr"
            command = run(
                "supervise-paper",
                store,
                engine,
                *scope,
                "--employee",
                employee,
                "--managed-engine",
                "--turn-budget",
                2,
                "--idle-seconds",
                140,
                "--max-restarts",
                int(company_cut),
                "--max-seconds",
                168,
            )
            marker = output / "before-company-receipt.json"
            if company_cut:
                command = [
                    sys.executable,
                    "-IB",
                    "-c",
                    FAULT_SUPERVISOR,
                    str(marker),
                    WITHHELD,
                    *command[4:],
                ]
            with (
                (output / "company.stdout").open("w") as service_out,
                service_events.open("w") as service_err,
            ):
                service = subprocess.Popen(
                    command,
                    stdout=service_out,
                    stderr=service_err,
                    start_new_session=True,
                )
                try:
                    if company_cut:
                        cut = cut_original_worker(
                            service, marker, store, journal, service_events
                        )
                    service.wait(timeout=177)
                finally:
                    if service.poll() is None:
                        os.killpg(service.pid, signal.SIGTERM)
                        service.wait(timeout=15)
            if service.returncode != 0:
                raise RuntimeError((output / "company.stderr").read_text())
            result, supervision = [
                json.loads(line)
                for line in (output / "company.stdout").read_text().splitlines()
            ]
            assert supervision["completed"]
            attempts = supervision["attempts"]
            assert len(attempts) == 1 + int(company_cut)
            if cut is not None:
                assert attempts[0]["pid"] == cut["pid"]
                assert attempts[0]["returncode"] == -signal.SIGKILL
                assert (
                    attempts[1]["pid"] != cut["pid"] and attempts[1]["returncode"] == 0
                )
            assert worker.poll() is None, "Venue exited before the next duty"
            assert worker.wait(timeout=195) == 0, "Venue did not exit cleanly"
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=15)

    opening, review = result["first_trade"], result["review"]
    assert review, "No next duty decision observed"
    if cut is not None:
        [recovered] = result["reconciliation"]["recovered"]
        claim = recovered["original_claim"]
        posting = recovered["posting"]
        assert recovered["sources"] == ["history", "account_sync"]
        assert (
            posting["order_id"] == cut["order_id"]
            and posting["fill_id"] == cut["fill_id"]
        )
        assert not result["reconciliation"]["unresolved"]
    else:
        assert opening and opening["order"], "No founder order observed"
        claim, posting = opening["claim"], opening["order"]["posting"]
        if posting is None:
            [recovered] = result["reconciliation"]["recovered"]
            assert recovered["original_claim"] == claim
            posting = recovered["posting"]
            assert posting["order_id"] == opening["order"]["order_id"]
        assert not result["reconciliation"]["unresolved"]
    assert claim["founder_id"] == review["claim"]["founder_id"] == company["founder_id"]
    assert claim["run_id"] != review["claim"]["run_id"]
    (observed,) = rows(
        journal,
        "SELECT order_id,fill_id,quantity,price_atoms,fee_atoms FROM sandbox_fill_observation",
    )
    (booked,) = rows(
        store,
        """SELECT f.order_id,f.fill_id,f.quantity,f.price_minor_units,c.fee_minor_units
           FROM paper_fill f JOIN paper_fill_fee c ON f.fill_id=c.fill_id""",
    )
    assert (
        observed
        == booked
        == (
            posting["order_id"],
            posting["fill_id"],
            posting["quantity"],
            posting["price_minor_units"],
            posting["fee_minor_units"],
        )
    )
    ((position, cash),) = rows(
        journal, "SELECT position_lots,cash_atoms FROM sandbox_account_observation"
    )
    assert (
        position == posting["quantity"]
        and cash == 100_000_000_000 - position * observed[3] - observed[4]
    )
    [(read_run, read_revision, prior_fill, projected_position)] = rows(
        store,
        """SELECT r.run_id,r.result_revision,p.fill_id,p.position_quantity
           FROM work_result_read r JOIN work_follow_up w ON w.duty_id=r.duty_id
           JOIN paper_position_observation p ON p.fill_id=w.result_id""",
    )
    assert (read_run, read_revision, prior_fill, projected_position) == (
        review["claim"]["run_id"],
        review["decision"]["observation_revision"],
        posting["fill_id"],
        position,
    )
    assert rows(store, "SELECT count(*) FROM work_review_decision") == [(1,)]
    assert rows(store, "PRAGMA foreign_key_check") == []
    return {
        "revision": revision,
        "tree": tree,
        "employee_profile": profile,
        "founder": company["founder_id"],
        "venue_pid": ready["pid"],
        "market_activation": market["activation_id"],
        "posting": posting,
        "account": {"position_lots": position, "cash_atoms": cash},
        "first_run_id": claim["run_id"],
        "review_run_id": read_run,
        "decision": review["decision"],
        "next_duty": review["next_duty"]["duty_id"],
        "post_activation_control_commands": 0,
        "company_cut": cut,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--employee", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--cut-company-after-fill", action="store_true")
    options = parser.parse_args()
    os.umask(0o077)
    result = observe(
        options.output.absolute(),
        options.employee.absolute(),
        options.wheelhouse,
        company_cut=options.cut_company_after_fill,
    )
    (options.output / "observer.json").write_text(json.dumps(result))
    print(json.dumps(result))
