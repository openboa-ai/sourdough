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


OBSERVATION = runpy.run_path(str(Path(__file__).with_name("observe_paper.py")))
cli = OBSERVATION["cli"]
rows = OBSERVATION["rows"]
wait_ready = OBSERVATION["wait_ready"]


def run(*arguments):
    return [sys.executable, "-IB", "-m", "sourdough.cli", *map(str, arguments)]


def observe(output: Path, employee: Path, wheelhouse: Path | None) -> dict:
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
            with (
                (output / "company.stdout").open("w") as service_out,
                (output / "company.stderr").open("w") as service_err,
            ):
                service = subprocess.run(
                    run(
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
                        0,
                        "--max-seconds",
                        168,
                    ),
                    stdout=service_out,
                    stderr=service_err,
                    timeout=177,
                )
            if service.returncode != 0:
                raise RuntimeError((output / "company.stderr").read_text())
            result, supervision = [
                json.loads(line)
                for line in (output / "company.stdout").read_text().splitlines()
            ]
            assert supervision["completed"] and len(supervision["attempts"]) == 1
            assert worker.poll() is None, "Venue exited before the next duty"
            assert worker.wait(timeout=195) == 0, "Venue did not exit cleanly"
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=15)

    opening, review = result["first_trade"], result["review"]
    assert opening and opening["order"] and opening["order"]["posting"], (
        "No founder fill observed"
    )
    assert review, "No next duty decision observed"
    assert (
        opening["claim"]["founder_id"]
        == review["claim"]["founder_id"]
        == company["founder_id"]
    )
    assert opening["claim"]["run_id"] != review["claim"]["run_id"]
    posting = opening["order"]["posting"]
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
        "first_run_id": opening["claim"]["run_id"],
        "review_run_id": read_run,
        "decision": review["decision"],
        "next_duty": review["next_duty"]["duty_id"],
        "post_activation_control_commands": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--employee", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    options = parser.parse_args()
    os.umask(0o077)
    result = observe(
        options.output.absolute(), options.employee.absolute(), options.wheelhouse
    )
    (options.output / "observer.json").write_text(json.dumps(result))
    print(json.dumps(result))
