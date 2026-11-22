"""Observe a subscribed hire, original managed paper fill, and recovered duty."""

import argparse
import json
import os
import runpy
import signal
import subprocess
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from time import sleep, time

from sourdough.market_worker import SpotInstrumentTerms
from sourdough.spot_cash import SpotCashUnits

HERE = Path(__file__).resolve().parent
FOUNDER = runpy.run_path(str(HERE / "observe_managed_founder.py"))
FAULT_SUPERVISOR, WITHHELD, cut_original_worker, run, wait_ready = (
    FOUNDER[name]
    for name in (
        "FAULT_SUPERVISOR",
        "WITHHELD",
        "cut_original_worker",
        "run",
        "wait_ready",
    )
)
PAPER = runpy.run_path(str(HERE / "observe_paper.py"))
cli, rows = PAPER["cli"], PAPER["rows"]
PREPARE = runpy.run_path(str(HERE.parent / "flow-06/observe_assessment.py"))["prepare"]


def stage_market(store, engine, scope, output):
    observations = {}
    activations = []
    for _attempt in range(3):
        receipt = cli(
            "observe-trading-market",
            store,
            engine,
            *scope,
            "--symbol",
            "BTCUSDT",
            "--samples",
            8,
            timeout=110,
        )
        activations.append(receipt["activation_id"])
        units = SpotCashUnits(SpotInstrumentTerms(**receipt["instrument"]))
        for item in sorted(
            receipt["observations"], key=lambda sample: sample["event_ns"]
        ):
            event = item["event_ns"] // 10**9
            observations.setdefault(
                event,
                {
                    "observation_id": item["trade_id"],
                    "price_minor_units": units.price_per_lot(Decimal(item["price"])),
                    "observed_at": event,
                    "available_at": (item["received_ns"] + 10**9 - 1) // 10**9,
                },
            )
        if len(observations) >= 2:
            break
    assert len(observations) >= 2, (
        "Public market did not provide two observation seconds"
    )
    samples = list(observations.values())[:16]
    source = output / "public-market.json"
    source.write_text(json.dumps(samples))
    sleep(max(0, max(item["available_at"] for item in samples) - time()))
    return activations, samples, source, units


def verify(store, journal, market, cut, result, trial, samples, units):
    [booking] = rows(
        store, "SELECT order_id,fill_id,quantity,price_minor_units FROM paper_fill"
    )
    [witness] = rows(
        journal,
        "SELECT order_id,fill_id,quantity,price_atoms FROM sandbox_fill_observation",
    )
    assert booking == witness and booking[:2] == (cut["order_id"], cut["fill_id"])
    [(position, cash)] = rows(
        journal, "SELECT position_lots,cash_atoms FROM sandbox_account_observation"
    )
    [(fee,)] = rows(journal, "SELECT fee_atoms FROM sandbox_fill_observation")
    assert (position, cash) == (
        booking[2],
        100_000_000_000 - booking[2] * booking[3] - fee,
    )
    assert rows(journal, "SELECT count(*) FROM sandbox_order") == [(1,)]
    assert rows(store, "SELECT fee_minor_units FROM paper_fill_fee") == [(fee,)]
    [recovered] = result["reconciliation"]["recovered"]
    assert recovered["sources"] == ["history", "account_sync"]
    assert not result["reconciliation"]["unresolved"]
    [(agent, profile, first_duty, first_run)] = rows(
        store,
        """SELECT hire.agent_id,agent.execution_profile,receipt.duty_id,receipt.run_id
           FROM hired_research_order_request request
           JOIN onboarding_acceptance receipt ON receipt.duty_id=request.duty_id
           JOIN hire_onboarding onboarding ON onboarding.first_duty_id=receipt.duty_id
           JOIN hire_receipt hire ON hire.agent_id=onboarding.agent_id
           JOIN agent ON agent.id=hire.agent_id
           JOIN paper_order paper ON paper.action_id=request.decision_id AND paper.run_id=request.run_id""",
    )
    assert agent != trial["founding"]["founder_id"] and profile == trial["profile"]
    assert recovered["original_claim"]["run_id"] == first_run
    [
        (
            finding_json,
            decision_json,
            decision_time,
            published,
            finding_revision,
            snapshot_json,
            snapshot_revision,
            limit,
        )
    ] = rows(
        store,
        """SELECT finding.content,decision.content,decision.decided_at,finding.published_at,
                  finding.revision,snapshot.inputs,snapshot.revision,paper.limit_minor_units
           FROM paper_research_finding finding JOIN paper_research_decision decision
           ON decision.finding_id=finding.finding_id
           JOIN paper_research_snapshot snapshot ON snapshot.run_id=finding.run_id
           JOIN paper_order paper ON paper.action_id=decision.decision_id AND paper.run_id=decision.run_id""",
    )
    finding, decision = json.loads(finding_json), json.loads(decision_json)
    snapshot = json.loads(snapshot_json)
    assert (
        snapshot["cut_at"] <= published <= decision_time
        and sha256(snapshot_json.encode()).hexdigest()
        == snapshot_revision
        == finding["snapshot_revision"]
        and sha256(finding_json.encode()).hexdigest()
        == finding_revision
        == decision["observation_revision"]
        and decision["action"] == "buy"
        and decision["quantity"] == booking[2]
        and decision["limit_minor_units"] == limit
        and booking[3] <= limit
    )
    increment = Decimal(units.terms.quantity_increment)
    original = {
        identity: (Decimal(price) * increment * 100_000_000, event, received)
        for identity, price, event, received in rows(
            market, "SELECT trade_id,price,event_ns,received_ns FROM market_trade"
        )
    }
    normalized = {sample["observation_id"]: sample for sample in samples}
    visible = {sample["observation_id"]: sample for sample in snapshot["observations"]}
    for label in (finding["start_id"], finding["end_id"]):
        price, event, received = original[label]
        sample = normalized[label]
        assert visible[label] == sample
        assert sample["price_minor_units"] == price
        assert sample["observed_at"] == event // 10**9
        assert (
            sample["available_at"]
            == (received + 10**9 - 1) // 10**9
            <= snapshot["cut_at"]
        )
    gross = original[finding["end_id"]][0] - original[finding["start_id"]][0]
    assert (
        finding["gross_change_minor_units"],
        finding["round_trip_fee_minor_units"],
        finding["net_change_minor_units"],
    ) == (gross, 0, gross)
    review = result["review"]
    claim = review["claim"]
    assert (claim["agent_id"], claim["execution_profile"]) == (agent, profile)
    assert claim["duty_id"] != first_duty and claim["run_id"] != first_run
    [(revision, read_at, due_at, units_read, quote)] = rows(
        store,
        """SELECT seen.result_revision,seen.read_at,work.due_at,position.position_quantity,market.quote_minor_units
           FROM work_result_read seen JOIN work_follow_up work ON work.duty_id=seen.duty_id
           JOIN paper_position_observation position ON position.fill_id=work.result_id
           JOIN paper_review_quote market ON market.run_id=seen.run_id""",
    )
    assert revision == review["decision"]["observation_revision"]
    assert read_at >= due_at == decision_time + decision["next_check_seconds"]
    assert units_read == position and quote > 0 and review["next_duty"]
    assert rows(store, "SELECT count(*) FROM hire_receipt") == [(1,)]
    assert rows(store, "SELECT count(*) FROM duty_run") == [(3,)]
    assert rows(store, "PRAGMA foreign_key_check") == []
    return {
        "agent": agent,
        "fill": booking,
        "venue_cash_atoms": cash,
        "review": review["decision"],
    }


def observe(output, employee):
    output.mkdir(mode=0o700)
    store, scope, trial = PREPARE(output, employee, scenario="paper")
    engine = output / "engine"
    installation = cli("prepare-trading-engine", store, engine, *scope, timeout=420)
    activations, samples, source, units = stage_market(store, engine, scope, output)
    plan = cli(
        "prepare-nautilus-paper",
        store,
        engine,
        *scope,
        "--account-id",
        "hired-paper",
        "--starting-atoms",
        100_000_000_000,
    )
    original = engine / ("nautilus-" + installation["distribution_version"])
    journal = original / "sandbox.sqlite"
    venue_log = output / "venue.log"
    with venue_log.open("w") as errors, (output / "venue.stdout").open("w") as stdout:
        venue = subprocess.Popen(
            run("serve-nautilus-paper", store, engine, *scope, "--seconds", 350),
            stdout=stdout,
            stderr=errors,
            start_new_session=True,
        )
        try:
            ready = wait_ready(venue, venue_log)
            binding = cli(
                "bind-nautilus-paper",
                store,
                engine,
                *scope,
                "--revision",
                "hired-paper-1",
                "--charter-revision",
                "local-record-integrity",
                "--exposure-atoms",
                10_000_000_000,
            )
            assert binding["account"]["destination_id"] == plan["destination_id"]
            trial.update(
                market_activations=activations,
                binding=binding,
                source=cli("stage-paper-observations", store, source, *scope),
            )
            (output / "trial.json").write_text(json.dumps(trial))
            marker, events = (
                output / "before-company-receipt.json",
                output / "company.log",
            )
            command = run(
                "supervise-hired-paper",
                store,
                engine,
                *scope,
                "--employee",
                employee,
                "--managed-engine",
                "--idle-seconds",
                270,
                "--max-seconds",
                325,
            )
            with (
                events.open("w") as errors,
                (output / "company.stdout").open("w") as stdout,
            ):
                service = subprocess.Popen(
                    [
                        *command[:2],
                        "-c",
                        FAULT_SUPERVISOR,
                        str(marker),
                        WITHHELD,
                        *command[4:],
                    ],
                    stdout=stdout,
                    stderr=errors,
                    start_new_session=True,
                )
                try:
                    cut = cut_original_worker(
                        service, marker, store, journal, events, timeout=155
                    )
                    assert service.wait(timeout=330) == 0, events.read_text()
                finally:
                    if service.poll() is None:
                        os.killpg(service.pid, signal.SIGTERM)
                        service.wait(timeout=15)
            assert venue.poll() is None
            assert venue.wait(timeout=370) == 0, venue_log.read_text()
        finally:
            if venue.poll() is None:
                os.killpg(venue.pid, signal.SIGTERM)
                venue.wait(timeout=15)
    result, supervision = map(
        json.loads, (output / "company.stdout").read_text().splitlines()
    )
    assert supervision["completed"]
    killed, replacement = supervision["attempts"]
    assert (killed["pid"], killed["returncode"]) == (cut["pid"], -signal.SIGKILL)
    assert (
        killed["exited_at_ns"] < replacement["ready_at_ns"]
        and replacement["returncode"] == 0
    )
    receipt = verify(
        store, journal, original / "market.sqlite", cut, result, trial, samples, units
    )
    return {
        "trial": trial,
        "venue_pid": ready["pid"],
        "cut": cut,
        **receipt,
        "post_activation_control_commands": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--employee", required=True, type=Path)
    options = parser.parse_args()
    os.umask(0o077)
    observed = observe(options.output.absolute(), options.employee.absolute())
    (options.output / "observer.json").write_text(json.dumps(observed))
    print(json.dumps(observed))
