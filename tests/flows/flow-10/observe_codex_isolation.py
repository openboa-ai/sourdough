"""Qualify the app's local Codex command profile without starting a model turn."""

import json
import runpy
import select
import socket
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


ADAPTER = Path(__file__).resolve().parents[3] / "tools/codex_employee.py"
CODEX = "/opt/homebrew/bin/codex"


def effective_profile(workspace: Path, arguments: list[str], environment: dict) -> dict:
    with subprocess.Popen(
        [CODEX, "app-server", "--strict-config", *arguments],
        cwd=workspace,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=0,
    ) as worker:

        def send(message):
            worker.stdin.write(json.dumps(message).encode() + b"\n")

        def receipt(identity):
            for _ in range(40):
                if not select.select([worker.stdout], [], [], 8)[0]:
                    raise RuntimeError("Local sandbox profile receipt timed out")
                response = json.loads(worker.stdout.readline())
                if response.get("id") == identity:
                    return response["result"]
            raise RuntimeError("Local sandbox profile receipt was absent")

        try:
            send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "sourdough-isolation-observer",
                            "version": "1",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
            receipt(1)
            send({"method": "initialized"})
            send(
                {
                    "id": 2,
                    "method": "thread/start",
                    "params": {"cwd": str(workspace), "ephemeral": True},
                }
            )
            result = receipt(2)
            return {
                "profile": result["activePermissionProfile"]["id"],
                "network": result["sandbox"]["networkAccess"],
                "mode": result["sandbox"]["type"],
            }
        finally:
            worker.terminate()
            worker.wait(timeout=5)


def observe() -> dict:
    adapter = runpy.run_path(str(ADAPTER))
    arguments = adapter["confinement_arguments"]()
    profile = adapter["ISOLATION_PROFILE"]
    with TemporaryDirectory(prefix="sd-isolation-") as scratch:
        root = Path(scratch)
        company, home, config, alice, bob = (
            root / name for name in ("company", "login", "codex-config", "alice", "bob")
        )
        for directory in (company, home, config, alice, bob):
            directory.mkdir(mode=0o700)
        for file, value in (
            (company / "book", "company-canary"),
            (home / "auth", "credential-canary"),
            (alice / "draft", "alice-canary"),
            (bob / "draft", "bob-canary"),
        ):
            file.write_text(value)
        environment = {
            "HOME": str(home),
            "CODEX_HOME": str(config),
            "PATH": "/usr/bin:/bin",
        }

        def command(workspace, *operands, selected=profile):
            return [
                CODEX,
                "sandbox",
                "-C",
                str(workspace),
                "-P",
                selected,
                *arguments,
                "--",
                *map(str, operands),
            ]

        def sandbox(workspace, *operands, selected=profile):
            return subprocess.run(
                command(workspace, *operands, selected=selected),
                capture_output=True,
                text=True,
                env=environment,
                timeout=10,
            )

        def denied(workspace, *operands):
            result = sandbox(workspace, *operands)
            assert result.returncode != 0 and not result.stdout, result
            assert (
                "Operation not permitted" in result.stderr
                or "Permission denied" in result.stderr
            ), result.stderr

        baseline = sandbox(alice, "/bin/cat", bob / "draft", selected=":read-only")
        assert baseline.returncode == 0 and baseline.stdout == "bob-canary"
        with (
            socket.socket() as listener,
            subprocess.Popen(
                command(bob, "/bin/sleep", "30"),
                env=environment,
            ) as peer_process,
        ):
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            host, port = listener.getsockname()
            with socket.create_connection((host, port), timeout=1):
                connection, _ = listener.accept()
                connection.close()
            try:
                for owner, peer in ((alice, bob), (bob, alice)):
                    assert effective_profile(owner, arguments, environment) == {
                        "profile": profile,
                        "network": False,
                        "mode": "readOnly",
                    }
                    own = sandbox(owner, "/bin/cat", owner / "draft")
                    assert (
                        own.returncode == 0 and own.stdout == f"{owner.name}-canary"
                    ), own
                    for target in (peer / "draft", company / "book", home / "auth"):
                        denied(owner, "/bin/cat", target)
                    denied(owner, "/usr/bin/touch", owner / "write-canary")
                    assert not (owner / "write-canary").exists()
                    denied(owner, "/usr/bin/nc", "-vz", "-G", "1", host, port)
                assert peer_process.poll() is None
                denied(alice, "/bin/kill", "-0", peer_process.pid)
            finally:
                peer_process.terminate()
                peer_process.wait(timeout=5)
    return {
        "profile": profile,
        "agents": 2,
        "own_workspace": "read",
        "peer_company_and_credential_reads": "denied",
        "workspace_writes": "denied",
        "peer_signal": "denied",
        "direct_loopback": "denied",
        "model_turns": 0,
        "scope": "native command boundary on the tested local runtime",
    }


if __name__ == "__main__":
    print(json.dumps(observe(), sort_keys=True))
