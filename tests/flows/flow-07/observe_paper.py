"""Observe one Sourdough-admitted virtual order against current public spot data."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
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
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return json.loads(run.stdout)


def rows(store, query):
    with closing(sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)) as database:
        return database.execute(query).fetchall()


def wait_ready(worker, log, seconds=36):
    deadline = monotonic() + seconds
    while monotonic() < deadline and worker.poll() is None:
        for line in log.read_text().splitlines():
            if line.startswith('{"event": "worker_ready"'):
                return json.loads(line)
        sleep(0.1)
    raise RuntimeError(f"Sandbox did not become ready: {log.read_text()}")


def observe(output: Path, wheelhouse: Path | None) -> dict:
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
    market = cli("observe-trading-market", store, engine, *scope, "--symbol", "BTCUSDT")
    assert market["completed"]
    prepared = cli(
        "prepare-nautilus-paper",
        store,
        engine,
        *scope,
        "--account-id",
        "paper-check",
        "--starting-atoms",
        1000 * 10**8,
    )
    venue = engine / "nautilus-1.231.0" / "sandbox.sqlite"
    runtime_log = output / "engine.stderr"
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
            ],
            stdout=stdout,
            stderr=events,
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
            )
            assert binding["account"]["environment"] == "paper"
            assert binding["account"]["destination_id"] == prepared["destination_id"]
            result = cli(
                "operate-nautilus-paper", store, engine, *scope, "--employee", employee
            )
            assert worker.poll() is None
            assert worker.wait(timeout=40) == 0
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=8)
    order = result["first_trade"]["order"]
    posting = order["posting"]
    assert result["first_trade"]["position_quantity"] == 20
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
        == (posting["fill_id"], order["order_id"], 20, posting["price_minor_units"])
    )
    assert posting["fee_minor_units"] == witnessed[4] == 0
    position, cash = rows(
        venue, "SELECT position_lots,cash_atoms FROM sandbox_account_observation"
    )[0]
    assert (position, cash) == (20, 1000 * 10**8 - 20 * posting["price_minor_units"])
    assert rows(venue, "SELECT count(*) FROM sandbox_order") == [(1,)]
    assert rows(store, "SELECT count(*) FROM paper_order") == [(1,)]
    return {
        "market_activation": market["activation_id"],
        "instrument_revision": market["instrument_revision"],
        "engine_worker_pid": ready["pid"],
        "scope": "virtual cash; zero test fee; one full fill",
        "posting": posting,
        "sandbox_account": {"position_lots": position, "cash_atoms": cash},
        "verified_after_engine_exit": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(observe(args.output.absolute(), args.wheelhouse)))
