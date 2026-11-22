"""Opt-in installed-runtime founder observation; no provider calls in CI."""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from importlib.util import find_spec
from pathlib import Path
from time import monotonic, sleep, time


WITHHELD_RECEIPT_WORKER = """
import json, os, sys
from pathlib import Path
from time import sleep
from sourdough import cli
from sourdough.paper_venue import FixedQuoteVenue

barrier = Path(sys.argv.pop(1))
class WithheldReceipt(FixedQuoteVenue):
    def submit(self, order_id, account_id, intent):
        super().submit(order_id, account_id, intent)
        with barrier.open("x") as stream:
            json.dump({"pid":os.getpid(),"order_id":order_id}, stream)
        sleep(120)
cli.FixedQuoteVenue = WithheldReceipt
raise SystemExit(cli.main())
"""
SUPERVISED_FIXTURE = """
import sys
from sourdough import cli
marker, worker = sys.argv[1:3]
del sys.argv[1:3]
supervise = cli.supervise_worker
def trace_transport(command, store, company_id, **options):
    candidate = [sys.executable, "-IB", "-c", worker, marker, *command[4:]]
    return supervise(candidate, store, company_id, **options)
cli.supervise_worker = trace_transport
raise SystemExit(cli.main())
"""


def cli(*arguments):
    return json.loads(
        subprocess.run(
            [sys.executable, "-IB", "-m", "sourdough.cli", *map(str, arguments)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    )


def read(path, sql, params=()):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(sql, params).fetchall()


def one(path, sql, params=()):
    (row,) = read(path, sql, params)
    return row


def log(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def observe(output: Path, employee: Path):
    output.mkdir(mode=0o700)
    checkout = Path(__file__).resolve().parents[3]
    installed = Path(find_spec("sourdough").origin).parent
    for source in (checkout / "src/sourdough").glob("*.py"):
        assert (installed / source.name).read_bytes() == source.read_bytes(), source
    assert employee.read_bytes() == (checkout / "tools/codex_employee.py").read_bytes()
    revision, tree = subprocess.check_output(
        ["git", "-C", checkout, "rev-parse", "HEAD", "HEAD^{tree}"],
        text=True,
    ).splitlines()
    store, venue, barrier, evidence = (
        output / name
        for name in ("company.sqlite", "venue.sqlite", "cut.json", "observations.json")
    )
    profile = cli("employee-profile", employee)["execution_profile"]
    employee_selection = json.loads(
        subprocess.run(
            [str(employee)],
            input='{"operation":"identify"}',
            capture_output=True,
            text=True,
            timeout=25,
            check=True,
        ).stdout
    )
    setup = cli(
        "found",
        store,
        "--setup-identity",
        "founder-recovery",
        "--charter-revision",
        "synthetic",
        "--founder-scope",
        "paper",
        "--execution-profile",
        profile,
    )
    identity = ("--company-id", setup["company_id"])
    binding = cli(
        "bind-paper",
        store,
        venue,
        *identity,
        "--charter-revision",
        "synthetic",
        "--revision",
        "trial",
        "--account-id",
        "founder-paper",
        "--rules",
        "synthetic-btc-001",
        "--starting-minor-units",
        50_000,
        "--exposure-minor-units",
        25_000,
        "--quote-minor-units",
        5000,
        "--fee-bps",
        4,
    )
    origin = int(time())
    observations = [
        ("lookback", 4800, -180, -175),
        ("middle", 4920, -120, -115),
        ("latest", 5000, -60, -55),
        ("withheld", 5100, -20, 900),
    ]
    evidence.write_text(
        json.dumps(
            [
                {
                    "observation_id": key,
                    "price_minor_units": price,
                    "observed_at": origin + observed,
                    "available_at": origin + available,
                }
                for key, price, observed, available in observations
            ]
        )
    )
    trial = {
        "revision": revision,
        "tree": tree,
        "founding": setup,
        "binding": binding,
        "profile": profile,
        "employee": employee_selection,
        "turn_budget": 2,
        "wall_deadline_seconds": 360,
        "restart_deadline_seconds": 60,
        "startup_deadline_seconds": 35,
        "max_restarts": 1,
        "paper_price": 5000,
        "fee_bps": 4,
        "client": False,
        "source": cli("stage-paper-observations", store, evidence, *identity),
    }
    (output / "trial.json").write_text(json.dumps(trial))
    log(trial)
    launch = [
        sys.executable,
        "-IB",
        "-c",
        SUPERVISED_FIXTURE,
        str(barrier),
        WITHHELD_RECEIPT_WORKER,
        "supervise-paper",
        str(store),
        str(venue),
        *identity,
        "--employee",
        str(employee),
        "--max-restarts",
        "1",
        "--turn-budget",
        "2",
        "--idle-seconds",
        "240",
        "--startup-seconds",
        "35",
        "--max-seconds",
        "360",
    ]
    started = monotonic()
    with (
        (output / "supervision.stdout").open("w") as stdout,
        (output / "supervision.stderr").open("w") as stderr,
    ):
        with subprocess.Popen(launch, stdout=stdout, stderr=stderr) as service:
            cut = await_cut(
                service,
                barrier,
                min(started + 120, started + trial["wall_deadline_seconds"]),
            )
            if cut is None:
                service.wait(245)
                log(
                    {
                        "verdict": "INCOMPLETE",
                        "reason": "No independently confirmed pre-receipt cut",
                        "exit": service.returncode,
                    }
                )
                return 2
            event_log = output / "supervision.stderr"
            events = [
                json.loads(line)
                for line in event_log.read_text().splitlines()
                if line.startswith("{") and line.endswith("}")
            ]
            assert any(
                event.get("event") == "worker_started" and event["pid"] == cut["pid"]
                for event in events
            )
            known = one(
                venue,
                "SELECT order_id, fill_id, quantity, fill_price_minor_units FROM venue_execution",
            )
            assert known[0] == cut["order_id"] and known[1] is not None
            assert (
                read(store, "SELECT name FROM sqlite_schema WHERE name = 'paper_fill'")
                == []
            )
            original, finding, decision, research_receipt = check_research(
                store, setup, known
            )
            cut_at = monotonic()
            os.kill(cut["pid"], signal.SIGKILL)
            log(
                {"cut": cut, "fill": known[1], "finding": finding, "decision": decision}
            )
            book_observed_at = await_booking(service, store, known[1], cut_at + 120)
            assert book_observed_at is not None, (
                "Original fill was not observed in the recovery window"
            )
            service.wait(max(1, started + 365 - monotonic()))
    if service.returncode != 0:
        log(
            {
                "verdict": "INCOMPLETE",
                "reason": "Supervision did not complete",
                "exit": service.returncode,
            }
        )
        return service.returncode
    worker, supervisory = (
        json.loads(line)
        for line in (output / "supervision.stdout").read_text().splitlines()
    )
    older, replacement = supervisory["attempts"]
    assert (
        supervisory["completed"]
        and older["pid"] == cut["pid"]
        and older["returncode"] == -signal.SIGKILL
    )
    assert replacement["pid"] != cut["pid"] and replacement["returncode"] == 0
    assert (
        0
        < replacement["ready_at_ns"] - older["exited_at_ns"]
        <= trial["restart_deadline_seconds"] * 10**9
    )
    order_id, fill_id, quantity, price = known
    fee = (quantity * price * 4 + 5000) // 10000
    for path, table in ((venue, "venue_execution"), (store, "paper_fill")):
        assert read(path, f"SELECT order_id, fill_id, quantity FROM {table}") == [
            (order_id, fill_id, quantity)
        ]
    assert read(venue, "SELECT fee_minor_units FROM venue_fill_fee") == [(fee,)]
    assert read(store, "SELECT fee_minor_units FROM paper_fill_fee") == [(fee,)]
    assert read(store, "SELECT reserved_minor_units FROM paper_order") == [
        (price * quantity,)
    ]
    (recovered,) = worker["reconciliation"]["recovered"]
    assert (
        recovered["sources"] == ["history", "account_sync"]
        and recovered["posting"]["fill_id"] == fill_id
    )
    assert recovered["original_claim"]["run_id"] == original[1]
    review = worker["review"]
    if review is None:
        log(
            {
                "verdict": "INCOMPLETE",
                "reason": "Recovery completed but no new decision was observed",
            }
        )
        return 2
    claim = review["claim"]
    assert (claim["founder_id"], claim["execution_profile"]) == (
        setup["founder_id"],
        profile,
    )
    assert claim["duty_id"] != original[0] and claim["run_id"] != original[1]
    observed = one(
        store,
        """
        SELECT w.duty_id, r.run_id, r.result_revision, r.read_at, w.due_at, p.position_quantity
        FROM work_follow_up w JOIN work_result_read r ON r.duty_id = w.duty_id
        JOIN paper_position_observation p ON p.fill_id = w.result_id
        WHERE w.previous_duty_id = ?
    """,
        (original[0],),
    )
    assert observed[:3] == (
        claim["duty_id"],
        claim["run_id"],
        review["decision"]["observation_revision"],
    )
    assert (
        observed[3]
        >= observed[4]
        == research_receipt["decided_at"] + decision["next_check_seconds"]
    )
    assert observed[5] == quantity
    assert read(store, "SELECT count(*) FROM duty_run") == [(2,)]
    assert read(store, "SELECT count(*) FROM work_review_decision") == [(1,)]
    assert read(store, "PRAGMA foreign_key_check") == []
    before_inspection = store.read_bytes()
    projection = cli("inspect-work", store, *identity)
    orders = projection["paper"]["orders"]
    assert projection["paper"]["booked_position"] == quantity
    assert (
        len(orders) == 1
        and orders[0]["fill_id"] == fill_id
        and orders[0]["fee_minor_units"] == fee
    )
    assert orders[0]["action_id"] == research_receipt["decision_id"]
    assert (
        orders[0]["remaining_quantity"]
        == orders[0]["open_reservation_minor_units"]
        == 0
    )
    rows = {item["duty_id"]: item for item in projection["duties"]}
    assert rows[original[0]]["decision"]["finding_id"] == research_receipt["finding_id"]
    assert (
        rows[claim["duty_id"]]["decision"]["decision_id"]
        == review["decision"]["call_id"]
    )
    assert (
        rows[review["next_duty"]["duty_id"]]["due_at"] == review["next_duty"]["due_at"]
    )
    assert store.read_bytes() == before_inspection
    (output / "board-projection.json").write_text(json.dumps(projection))
    receipt = {
        "verdict": "OBSERVED",
        "candidate": revision,
        "trial": trial,
        "research": research_receipt,
        "finding": finding,
        "decision": decision,
        "review": review,
        "supervisor": supervisory,
        "order": order_id,
        "fill": fill_id,
        "quantity": quantity,
        "fee": fee,
        "sources": recovered["sources"],
        "elapsed_seconds": round(monotonic() - started, 3),
        "cut_to_book_seconds": round(book_observed_at - cut_at, 3),
        "post_activation_operating_commands": 0,
        "driver_actions": [
            "independent read-only observations",
            "one SIGKILL at the committed venue cut",
        ],
        "board_projection_revision": projection["revision"],
    }
    (output / "observer.json").write_text(json.dumps(receipt))
    log(receipt)
    return 0


def await_cut(service, barrier, deadline):
    while service.poll() is None and monotonic() < deadline:
        if barrier.is_file():
            try:
                return json.loads(barrier.read_text())
            except ValueError:
                pass
        sleep(0.03)
    return None


def await_booking(service, store, fill_id, deadline):
    while monotonic() < deadline:
        if read(store, "SELECT 1 FROM sqlite_schema WHERE name='paper_fill'") and read(
            store, "SELECT 1 FROM paper_fill WHERE fill_id=?", (fill_id,)
        ):
            return monotonic()
        if service.poll() is not None:
            break
        sleep(0.03)
    return None


def check_research(store, setup, known):
    row = one(
        store,
        """
        SELECT f.finding_id,f.revision,f.content,f.published_at,d.decision_id,d.content,d.decided_at,
               s.inputs,o.action_id,r.duty_id,r.run_id
        FROM paper_research_finding f JOIN paper_research_decision d ON d.finding_id=f.finding_id AND d.run_id=f.run_id
        JOIN paper_research_snapshot s ON s.run_id=f.run_id JOIN paper_order o ON o.action_id=d.decision_id
        JOIN duty_run r ON r.run_id=o.run_id WHERE o.order_id=?
    """,
        (known[0],),
    )
    (
        finding_id,
        revision,
        finding_json,
        published,
        decision_id,
        decision_json,
        decided,
        inputs_json,
        action,
        duty,
        run,
    ) = row
    finding, decision, inputs = map(
        json.loads, (finding_json, decision_json, inputs_json)
    )
    assert (
        duty == setup["due_work_id"] and published <= decided and action == decision_id
    )
    assert (
        decision["observation_revision"] == revision
        and decision["action"] == "buy"
        and decision["quantity"] == known[2]
    )
    observed = {item["observation_id"]: item for item in inputs["observations"]}
    assert "withheld" not in observed and all(
        item["available_at"] <= inputs["cut_at"] for item in observed.values()
    )
    start, end = (
        observed[finding[key]]["price_minor_units"] for key in ("start_id", "end_id")
    )
    fee = sum((price * 4 + 5000) // 10000 for price in (start, end))
    gross = end - start
    assert (
        finding["gross_change_minor_units"],
        finding["round_trip_fee_minor_units"],
        finding["net_change_minor_units"],
    ) == (gross, fee, gross - fee)
    return (
        (duty, run),
        finding,
        decision,
        {
            "finding_id": finding_id,
            "finding_revision": revision,
            "decision_id": decision_id,
            "decided_at": decided,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--employee", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(
        observe(arguments.output.absolute(), arguments.employee.absolute())
    )
