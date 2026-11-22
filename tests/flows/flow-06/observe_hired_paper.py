"""Opt-in installed observation of a hired Agent's scoped paper continuity."""

import argparse
import json
import os
import runpy
import signal
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep, time


FIXTURE = runpy.run_path(str(Path(__file__).with_name("observe_assessment.py")))
prepare, cli, rows, store_json = (
    FIXTURE[name] for name in ("prepare", "cli", "rows", "store_json")
)
VENUE_FIXTURE = runpy.run_path(
    str(Path(__file__).resolve().parent.parent / "flow-05/observe_founder_recovery.py")
)


def observe(output, employee):
    output.mkdir(mode=0o700)
    company, identity, trial = prepare(output, employee, scenario="paper")
    venue, barrier, observations = (
        output / name
        for name in ("venue.sqlite", "venue-cut.json", "paper-source.json")
    )
    trial["binding"] = cli(
        "bind-paper",
        company,
        venue,
        *identity,
        "--charter-revision",
        "local-record-integrity",
        "--revision",
        "hired-fixture",
        "--account-id",
        "hired-paper",
        "--rules",
        "synthetic-btc-001",
        "--starting-minor-units",
        50000,
        "--exposure-minor-units",
        25000,
        "--quote-minor-units",
        5000,
        "--fee-bps",
        4,
    )
    origin = int(time())
    samples = [
        {
            "observation_id": key,
            "price_minor_units": price,
            "observed_at": origin + offset,
            "available_at": origin + offset + 5,
        }
        for key, price, offset in (
            ("lookback", 4800, -180),
            ("middle", 4920, -120),
            ("latest", 5000, -60),
        )
    ]
    store_json(observations, samples)
    trial["source"] = cli("stage-paper-observations", company, observations, *identity)
    store_json(output / "trial.json", trial)
    print(json.dumps(trial), flush=True)
    command = [
        sys.executable,
        "-IB",
        "-c",
        VENUE_FIXTURE["SUPERVISED_FIXTURE"],
        str(barrier),
        VENUE_FIXTURE["WITHHELD_RECEIPT_WORKER"],
        "supervise-hired-paper",
        str(company),
        str(venue),
        *identity,
        "--employee",
        str(employee),
        "--turn-budget",
        "3",
        "--max-restarts",
        "1",
        "--idle-seconds",
        "140",
        "--max-seconds",
        "360",
    ]
    started = monotonic()
    with (
        (output / "worker.stdout").open("w") as stdout,
        (output / "supervision.stderr").open("w") as stderr,
        subprocess.Popen(command, stdout=stdout, stderr=stderr) as worker,
    ):
        try:
            while worker.poll() is None and monotonic() - started < 215:
                if barrier.exists():
                    try:
                        cut = json.loads(barrier.read_text())
                        break
                    except ValueError:
                        pass
                sleep(0.05)
            else:
                print(
                    json.dumps(
                        {"verdict": "INCOMPLETE", "reason": "No venue fill cut"}
                    ),
                    flush=True,
                )
                return 2
            (original,) = rows(
                venue,
                """SELECT order_id,fill_id,quantity,fill_price_minor_units FROM venue_execution""",
            )
            assert cut["order_id"] == original[0] and original[1]
            assert (
                rows(company, "SELECT name FROM sqlite_schema WHERE name='paper_fill'")
                == []
            )
            (accepted,) = rows(
                company, "SELECT duty_id,run_id FROM onboarding_acceptance"
            )
            assert rows(
                company, "SELECT state FROM duty WHERE id=?", (accepted[0],)
            ) == [("running",)]
            (original_request,) = rows(
                company,
                """SELECT r.duty_id,r.run_id,r.decision_id FROM hired_research_order_request r
                   JOIN paper_order o ON o.run_id=r.run_id AND o.action_id=r.decision_id
                   WHERE o.order_id=?""",
                (original[0],),
            )
            assert original_request[:2] == accepted
            os.kill(cut["pid"], signal.SIGKILL)
            assert worker.wait(timeout=max(1, 365 - (monotonic() - started))) == 0
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=10)

    result, supervision = [
        json.loads(line) for line in (output / "worker.stdout").read_text().splitlines()
    ]
    killed, replacement = supervision["attempts"]
    assert killed["pid"] == cut["pid"] and killed["returncode"] == -signal.SIGKILL
    assert killed["exited_at_ns"] < replacement["ready_at_ns"]
    assert replacement["pid"] != killed["pid"] and replacement["returncode"] == 0
    assert supervision["completed"]
    (author,) = rows(
        company,
        """SELECT d.agent_id,a.execution_profile,g.scope,h.allocated_turns,h.manager_grant_id
           FROM duty d JOIN agent a ON a.id=d.agent_id JOIN hire_receipt h ON h.agent_id=a.id
           JOIN standing_grant g ON g.id=h.grant_id WHERE d.id=?""",
        (accepted[0],),
    )
    agent_id, profile, scope, turns, manager_grant = author
    assert agent_id != trial["founding"]["founder_id"]
    assert profile == trial["profile"] and scope == "paper" and 2 <= turns <= 5
    assert manager_grant == trial["founding"]["grant_id"]
    (stored,) = rows(
        company,
        """SELECT f.content,f.revision,d.content,d.decided_at,d.decision_id,f.published_at
           FROM paper_research_finding f JOIN paper_research_decision d ON d.finding_id=f.finding_id
           WHERE f.run_id=? AND d.duty_id=?""",
        (accepted[1], accepted[0]),
    )
    finding, decision = json.loads(stored[0]), json.loads(stored[2])
    assert (
        stored[1] == decision["observation_revision"]
        and stored[4] == original_request[2]
    )
    assert stored[5] <= stored[3]
    prices = {
        sample["observation_id"]: sample["price_minor_units"] for sample in samples
    }
    begin, end = prices[finding["start_id"]], prices[finding["end_id"]]
    cost = sum((price * 4 + 5000) // 10000 for price in (begin, end))
    assert (
        finding["gross_change_minor_units"],
        finding["round_trip_fee_minor_units"],
        finding["net_change_minor_units"],
    ) == (
        end - begin,
        cost,
        end - begin - cost,
    )
    assert decision["action"] == "buy" and decision["quantity"] == original[2]
    fee = (original[2] * original[3] * 4 + 5000) // 10000
    assert rows(
        company, "SELECT order_id,fill_id,quantity,price_minor_units FROM paper_fill"
    ) == [original]
    assert rows(venue, "SELECT count(*) FROM venue_execution") == [(1,)]
    assert rows(company, "SELECT fee_minor_units FROM paper_fill_fee") == [(fee,)]
    (recovered,) = result["reconciliation"]["recovered"]
    assert recovered["sources"] == ["history", "account_sync"]
    assert recovered["original_claim"]["run_id"] == accepted[1]
    assert recovered["posting"]["fill_id"] == original[1]
    review = result["review"]
    assert review is not None
    claim = review["claim"]
    assert (claim["founder_id"], claim["execution_profile"]) == (agent_id, profile)
    assert claim["duty_id"] != accepted[0] and claim["run_id"] != accepted[1]
    (observed,) = rows(
        company,
        """SELECT r.result_revision,r.read_at,w.due_at,p.position_quantity,q.quote_minor_units
           FROM work_result_read r JOIN work_follow_up w ON w.duty_id=r.duty_id
           JOIN paper_position_observation p ON p.fill_id=w.result_id
           JOIN paper_review_quote q ON q.run_id=r.run_id WHERE r.run_id=?""",
        (claim["run_id"],),
    )
    assert observed[0] == review["decision"]["observation_revision"]
    assert observed[1] >= observed[2] == stored[3] + decision["next_check_seconds"]
    assert observed[3:] == (original[2], 5000)
    assert rows(company, "SELECT count(*) FROM hire_receipt") == [(1,)]
    assert rows(company, "SELECT count(*) FROM duty_run") == [(3,)]
    assert rows(company, "PRAGMA foreign_key_check") == []
    evidence = {
        "verdict": "OBSERVED",
        "trial": trial,
        "supervision": supervision,
        "finding": finding,
        "decision": decision,
        "requested": original_request,
        "confirmed_fill": original,
        "fee": fee,
        "review": review,
        "post_activation_operating_commands": 0,
        "elapsed_seconds": round(monotonic() - started, 3),
    }
    store_json(output / "observer.json", evidence)
    print(json.dumps(evidence), flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--employee", required=True, type=Path)
    options = parser.parse_args()
    raise SystemExit(observe(options.output.absolute(), options.employee.absolute()))
