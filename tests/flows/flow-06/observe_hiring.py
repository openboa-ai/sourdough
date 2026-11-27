"""Observe transport replay and workspace retry during an actual hire."""

import argparse
import json
import runpy
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

FIXTURE = runpy.run_path(str(Path(__file__).with_name("observe_assessment.py")))
prepare, rows, store_json = (
    FIXTURE[name] for name in ("prepare", "rows", "store_json")
)


WITHHELD_RESOURCES = r"""
import json, sys
from pathlib import Path
from sourdough import cli, employee_process, onboarding
deliveries, workspace = (Path(value) for value in sys.argv[1:3])
del sys.argv[1:3]
handle = employee_process._ToolRequest.handle
probe = onboarding.probe_desk_scratch
class WithheldFirstHire:
    def __init__(self, delegate):
        self.delegate = delegate
    def __getattr__(self, name):
        return getattr(self.delegate, name)
    def write(self, data):
        receipt = json.loads(data)
        if set(receipt) == {"request_id", "agent_id", "grant_id", "allocated_turns"}:
            first = not deliveries.exists()
            with deliveries.open("a") as record:
                record.write(json.dumps(receipt) + "\n")
            if first:
                return len(data)
        return self.delegate.write(data)
def cut_reply(connection):
    connection.wfile = WithheldFirstHire(connection.wfile)
    handle(connection)
def withhold_initial_workspace():
    if not workspace.exists():
        workspace.write_text("unavailable")
        return False
    return probe()
employee_process._ToolRequest.handle = cut_reply
onboarding.probe_desk_scratch = withhold_initial_workspace
raise SystemExit(cli.main())
"""


def observe(output, employee):
    compile(WITHHELD_RESOURCES, "withheld hire transport", "exec")
    output.mkdir(mode=0o700)
    company, identity, trial = prepare(output, employee, scenario="hiring-only")
    deliveries, workspace = (
        output / name for name in ("hire-deliveries.jsonl", "workspace-cut")
    )
    print(json.dumps(trial), flush=True)
    arguments = [
        sys.executable,
        "-IB",
        "-c",
        WITHHELD_RESOURCES,
        str(deliveries),
        str(workspace),
        "operate-hiring",
        str(company),
        *identity,
        "--employee",
        str(employee),
        "--first-purpose",
        "assessment",
    ]
    started = monotonic()
    blocked = None
    with (
        (output / "worker.stdout").open("w") as stdout,
        (output / "worker.stderr").open("w") as stderr,
        subprocess.Popen(arguments, stdout=stdout, stderr=stderr) as worker,
    ):
        try:
            while worker.poll() is None and monotonic() - started < 120:
                if workspace.exists():
                    statuses = rows(
                        company,
                        "SELECT agent_id,state,retry_at,reason,first_duty_id FROM hire_onboarding",
                    )
                    if statuses and statuses[0][1] == "blocked":
                        (blocked,) = statuses
                        assert blocked[-2:] == ("workspace_unavailable", None)
                        assert rows(company, "SELECT count(*) FROM private_desk") == [
                            (1,)
                        ]
                        assert rows(company, "SELECT count(*) FROM duty_run") == [(1,)]
                        break
                sleep(0.05)
            assert worker.wait(timeout=max(1, 240 - (monotonic() - started))) == 0
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=10)
    receipt = json.loads((output / "worker.stdout").read_text())
    if blocked is None or not receipt["accepted"]:
        print(json.dumps({"verdict": "INCOMPLETE", "result": receipt}), flush=True)
        return 2
    original, replay = [
        json.loads(line) for line in deliveries.read_text().splitlines()
    ]
    assert original == replay
    assert original["agent_id"] == blocked[0] == receipt["hire_claim"]["agent_id"]
    assert original["agent_id"] != trial["founding"]["founder_id"]
    assert receipt["hire_claim"]["execution_profile"] == trial["profile"]
    assert rows(company, "SELECT agent_id,allocated_turns FROM hire_receipt") == [
        (original["agent_id"], original["allocated_turns"])
    ]
    assert rows(company, "SELECT count(*) FROM standing_grant") == [(2,)]
    assert rows(
        company, "SELECT scope FROM standing_grant WHERE id=?", (original["grant_id"],)
    ) == [("assessment",)]
    assert rows(company, "SELECT count(*) FROM private_desk") == [(2,)]
    assert rows(company, "SELECT count(*) FROM duty_run") == [(2,)]
    assert rows(
        company,
        """SELECT duty_id,run_id FROM onboarding_acceptance""",
    ) == [(receipt["hire_claim"]["duty_id"], receipt["hire_claim"]["run_id"])]
    assert (
        rows(company, "SELECT name FROM sqlite_schema WHERE name LIKE 'paper_%'") == []
    )
    assert rows(company, "PRAGMA foreign_key_check") == []
    result = {
        "verdict": "OBSERVED",
        "trial": trial,
        "result": receipt,
        "duplicate_hire_receipts": [original, replay],
        "blocked": blocked,
        "elapsed_seconds": round(monotonic() - started, 3),
        "post_activation_operating_commands": 0,
    }
    store_json(output / "observer.json", result)
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--employee", required=True, type=Path)
    options = parser.parse_args()
    raise SystemExit(observe(options.output.absolute(), options.employee.absolute()))
