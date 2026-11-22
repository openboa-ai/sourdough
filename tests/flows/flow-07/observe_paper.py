"""Observe one company-admitted virtual order against public data or a sealed replay."""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep


EMPLOYEE = """
import json, sys
request = json.load(sys.stdin)
if request["operation"] == "identify":
    reply = {"provider": "verification", "model": "one-paper-limit", "revision": "1"}
elif request["operation"] == "ready":
    reply = {"ready": True}
else:
    assert request["operation"] == "first_duty"
    ask = request["context"]["quote_minor_units"]
    reply = {
        "tool": "paper_buy",
        "call_id": "bounded-paper-verification",
        "quantity": 20,
        "limit_minor_units": ask + (ask // 400 // 10) * 10,
    }
print(json.dumps(reply))
"""


def cli(*args, timeout=40):
    run = subprocess.run(
        [sys.executable, "-IB", "-m", "sourdough.cli", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if run.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {run.returncode}: {run.stderr}")
    return json.loads(run.stdout)


def rows(store, query):
    with closing(sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)) as database:
        return database.execute(query).fetchall()


def wait_ready(worker, log, seconds=36, previous_pid=None):
    deadline = monotonic() + seconds
    while monotonic() < deadline and worker.poll() is None:
        for line in log.read_text().splitlines():
            if line.startswith('{"event": "worker_ready"'):
                ready = json.loads(line)
                if ready["pid"] != previous_pid:
                    return ready
        sleep(0.1)
    raise RuntimeError(f"Sandbox did not become ready: {log.read_text()}")


def observe(
    output: Path,
    wheelhouse: Path | None,
    tape: Path | None = None,
    *,
    restart_engine: bool = False,
    expected_lots: int = 20,
) -> dict:
    if restart_engine and tape is not None:
        raise ValueError("Engine restart observation requires a current-market binding")
    output.mkdir(mode=0o700)
    employee = output / "employee"
    employee.write_text(f"#!{sys.executable}\n" + EMPLOYEE)
    employee.chmod(0o700)
    store, engine = output / "company.sqlite", output / "engine"
    profile = cli("employee-profile", employee)["execution_profile"]
    company = cli(
        "found",
        store,
        "--setup-identity",
        "flow-07",
        "--charter-revision",
        "synthetic",
        "--founder-scope",
        "paper",
        "--execution-profile",
        profile,
    )
    scope = ["--company-id", company["company_id"]]
    wheel = [] if wheelhouse is None else ["--wheelhouse", wheelhouse]
    cli("prepare-trading-engine", store, engine, *scope, *wheel, timeout=420)
    market = None
    if tape is None:
        market = cli(
            "observe-trading-market", store, engine, *scope, "--symbol", "BTCUSDT"
        )
        assert market["completed"]
    source = [] if tape is None else ["--replay-tape", str(tape)]
    replay = [] if tape is None else ["--replay"]
    prepared = cli(
        "prepare-nautilus-paper",
        store,
        engine,
        *scope,
        "--account-id",
        "paper-check",
        "--starting-atoms",
        1000 * 10**8,
        *source,
    )
    venue = (
        engine
        / "nautilus-1.231.0"
        / ("sandbox.sqlite" if tape is None else "replay.sqlite")
    )
    runtime_log = output / "engine.stderr"
    restarted = None
    with (
        runtime_log.open("w") as events,
        (output / "engine.stdout").open("w") as stdout,
    ):
        worker = subprocess.Popen(
            [
                sys.executable,
                "-IB",
                "-m",
                "sourdough.cli",
                "serve-nautilus-paper",
                str(store),
                str(engine),
                *scope,
                "--seconds",
                "22",
                *source,
            ],
            stdout=stdout,
            stderr=events,
            start_new_session=True,
        )
        try:
            ready = wait_ready(worker, runtime_log)
            binding = cli(
                "bind-nautilus-paper",
                store,
                engine,
                *scope,
                "--revision",
                "verification",
                "--charter-revision",
                "synthetic",
                "--exposure-atoms",
                100 * 10**8,
                *replay,
            )
            assert binding["account"]["environment"] == "paper"
            assert binding["account"]["destination_id"] == prepared["destination_id"]
            result = cli(
                "operate-nautilus-paper",
                store,
                engine,
                *scope,
                "--employee",
                employee,
                *replay,
            )
            if tape is None:
                assert worker.poll() is None
            if restart_engine:
                deadline = monotonic() + 6
                while not rows(venue, "SELECT 1 FROM sandbox_account_observation"):
                    assert monotonic() < deadline, (
                        "No independent virtual balance to restore"
                    )
                    sleep(0.05)
                os.kill(ready["pid"], signal.SIGKILL)
                restarted = wait_ready(worker, runtime_log, previous_pid=ready["pid"])
            assert worker.wait(timeout=40) == 0
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=8)
    order = result["first_trade"]["order"]
    posting = order["posting"]
    assert result["first_trade"]["position_quantity"] == expected_lots
    (witnessed,) = rows(
        venue,
        "SELECT fill_id,order_id,quantity,price_atoms,fee_atoms FROM sandbox_fill_observation",
    )
    (book,) = rows(
        store, "SELECT fill_id,order_id,quantity,price_minor_units FROM paper_fill"
    )
    assert (
        witnessed[:4]
        == book
        == (
            posting["fill_id"],
            order["order_id"],
            expected_lots,
            posting["price_minor_units"],
        )
    )
    assert posting["fee_minor_units"] == witnessed[4] == 0
    position, cash = rows(
        venue, "SELECT position_lots,cash_atoms FROM sandbox_account_observation"
    )[0]
    assert (position, cash) == (
        expected_lots,
        1000 * 10**8 - expected_lots * posting["price_minor_units"],
    )
    assert rows(venue, "SELECT count(*) FROM sandbox_order") == [(1,)]
    assert rows(store, "SELECT count(*) FROM paper_order") == [(1,)]
    cancellation = None
    if expected_lots < 20:
        [(original, event_id, filled)] = rows(
            venue, "SELECT order_id,event_id,filled_quantity FROM sandbox_cancellation"
        )
        assert (original, filled) == (order["order_id"], expected_lots)
        assert rows(
            store,
            "SELECT order_id,event_id,filled_quantity,released FROM paper_cancellation",
        ) == [(original, event_id, filled, 1)]
        [(admitted, remaining_reservation)] = rows(
            store, "SELECT quantity,reserved_minor_units FROM paper_order"
        )
        assert (
            admitted == 20
            and remaining_reservation == filled * posting["price_minor_units"]
        )
        cancellation = {
            "original_order_id": original,
            "filled_lots": filled,
            "cancelled_lots": admitted - filled,
            "reserved_atoms": remaining_reservation,
        }
    if restarted is not None:
        exits = [
            json.loads(line)
            for line in runtime_log.read_text().splitlines()
            if line.startswith('{"event": "worker_exited"')
        ]
        assert [(event["pid"], event["returncode"]) for event in exits] == [
            (ready["pid"], -signal.SIGKILL),
            (restarted["pid"], 0),
        ]
        assert rows(
            venue,
            "SELECT position_lots,cash_atoms FROM sandbox_activation ORDER BY rowid",
        ) == [(0, 1000 * 10**8), (position, cash)]
    replay_cut = None
    if tape is not None:
        (cut,) = rows(
            venue,
            """SELECT cut.price_atoms_per_lot,cut.event_ns,cut.available_ns
               FROM replay_decision_cut AS cut
               JOIN sandbox_order AS request ON request.generation=cut.generation""",
        )
        (config,) = rows(venue, "SELECT plan FROM sandbox_config")
        revision = json.loads(config[0])["tape_revision"]
        assert revision == sha256(tape.read_bytes()).hexdigest()
        assert not (venue.parent / "market.sqlite").exists()
        replay_cut = {
            "tape_revision": revision,
            "price_atoms_per_lot": cut[0],
            "event_ns": cut[1],
            "available_ns": cut[2],
        }
    return {
        "market_activation": market["activation_id"] if market is not None else None,
        "instrument_revision": binding["account"]["instrument_rules_digest"],
        "connector": binding["account"]["connector_revision"],
        "replay_cut": replay_cut,
        "cancellation": cancellation,
        "engine_worker_pid": ready["pid"],
        "replacement_worker_pid": restarted["pid"] if restarted else None,
        "scope": "virtual cash; zero test fee; one full fill",
        "posting": posting,
        "sandbox_account": {"position_lots": position, "cash_atoms": cash},
        "verified_after_engine_exit": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--replay-tape", type=Path)
    parser.add_argument("--restart-engine", action="store_true")
    parser.add_argument("--expected-lots", type=int, default=20)
    args = parser.parse_args()
    os.umask(0o077)
    print(
        json.dumps(
            observe(
                args.output.absolute(),
                args.wheelhouse,
                args.replay_tape,
                restart_engine=args.restart_engine,
                expected_lots=args.expected_lots,
            )
        )
    )
