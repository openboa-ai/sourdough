"""Desk isolation, sandbox constraints, scratch quota, harness bridge, and episode index hardening tests."""

from __future__ import annotations

import json
import os
import sys
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Any

from sourdough.agent_desk import (
    allocate_run_scratch,
    assert_writable_desk_relative_path,
    compute_agent_desk_digest,
    desk_directory,
    save_draft,
)
from sourdough.agent_runtime import (
    PROVIDER_OPENAI,
    AdapterManifest,
    AgentProfileBinding,
    AgentRuntimeAdapter,
    InMemorySecretResolver,
    OuterToolBroker,
    ShiftRequest,
    ShiftResult,
    TokenUsage,
)
from sourdough.agent_runtime.registry import AgentAdapterRegistry
from sourdough.duty_run import DutyRefused
from sourdough.episode_index import (
    episode_index_path,
    search_episodes,
    upsert_episode_entry,
)
from sourdough.harness import (
    DeskCommandLimits,
    create_agent_session,
    run_desk_command_sandboxed,
)
from sourdough.memory import record_episode
from sourdough.method_learning import (
    MethodRefused,
    distill_method_delta,
    redact_for_shared_scope,
)
from sourdough.runtime import (
    CompanyRuntime,
    DutyOutcome,
    build_production_duty_registry,
    create_work_item,
)
from sourdough.runtime.work_kernel import (
    claim_work_run,
    complete_work_run,
    reclaim_expired_runs,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer

from ._fixtures import (
    HardeningTestCase,
    _open_work_run_claim,
)


class TestHardeningDeskSandbox(HardeningTestCase):
    def test_rec1_host_managed_roots_casefold_and_unicode(self) -> None:
        for blocked in (
            "AGENTS.md",
            "Agents.md",
            "agents.md",
            "AGENTS.MD",
            "\uff21GENTS.md",
            "skills/foo/SKILL.md",
            "SKILLS/foo/SKILL.md",
            "Skills/bar",
            ".desk/drafts/x",
            ".DESK/drafts/x",
            ".Desk/logs/a.json",
            ".sourdough-traces/t.jsonl",
            ".SOURDOUGH-TRACES/t.jsonl",
        ):
            with self.subTest(path=blocked):
                with self.assertRaises(DutyRefused):
                    assert_writable_desk_relative_path(blocked)
        self.assertEqual(
            assert_writable_desk_relative_path("notes/analysis.md").as_posix(),
            "notes/analysis.md",
        )

    def test_rec2_seatbelt_denies_drafts_and_logs_writes_symlinks_renames(self) -> None:
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            self.skipTest("macOS sandbox-exec required")

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                db.execute(
                    "UPDATE standing_grant SET scope = 'desk:exec' WHERE id = ?",
                    (self.grant_id,),
                )
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _item, _run, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="desk:exec",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            save_draft(
                w,
                claim,
                claim.purpose,
                name="legit",
                content="safe draft body",
                expected_revision=None,
            )
            session = create_agent_session(w, claim, claim.purpose)

        # Ensure .desk/logs exists too and probe writes/symlinks/renames
        logs_dir = session.desk_path / ".desk" / "logs"
        logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        probe_script = (
            "import json, os\n"
            "res = {}\n"
            "with open('tmp_src.txt', 'w') as f: f.write('ok')\n"
            "res['regular'] = 'ALLOWED'\n"
            "def try_op(name, fn):\n"
            "    try:\n"
            "        fn(); res[name] = 'ALLOWED'\n"
            "    except PermissionError:\n"
            "        res[name] = 'BLOCKED'\n"
            "try_op('open_draft', lambda: open('.desk/drafts/evil.json', 'w'))\n"
            "try_op('symlink_draft', lambda: os.symlink('/etc/passwd', '.desk/drafts/s'))\n"
            "try_op('rename_draft', lambda: os.rename('tmp_src.txt', '.desk/drafts/r.json'))\n"
            "try_op('open_log', lambda: open('.desk/logs/evil.log', 'w'))\n"
            "try_op('symlink_log', lambda: os.symlink('/tmp', '.desk/logs/s'))\n"
            "try_op('rename_dotdesk', lambda: os.rename('.desk', '.desk_evil'))\n"
            "try_op('mkdir_dotdesk_x', lambda: os.mkdir('.desk/x'))\n"
            "try_op('open_dotdesk_pwn', lambda: open('.desk/pwn', 'w'))\n"
            "try_op('rename_dotdesk_scratch', lambda: os.rename('.desk/scratch', '.desk/scratch_evil'))\n"
            "tmpdir = os.environ['TMPDIR']\n"
            "try_op('rename_scratch_self', lambda: os.rename(tmpdir, tmpdir + '_evil'))\n"
            "with open(os.path.join(tmpdir, 'scratch_ok.txt'), 'w') as sf:\n"
            "    sf.write('scratch-allowed')\n"
            "res['scratch_write'] = 'ALLOWED'\n"
            "print(json.dumps(res))\n"
        )
        out = run_desk_command_sandboxed(
            session,
            [sys.executable, "-c", probe_script],
            store=self.store,
            limits=DeskCommandLimits(timeout_seconds=10.0),
        )
        self.assertTrue(out["ok"], out)
        parsed = json.loads(out["stdout"])
        self.assertEqual(
            parsed,
            {
                "regular": "ALLOWED",
                "open_draft": "BLOCKED",
                "symlink_draft": "BLOCKED",
                "rename_draft": "BLOCKED",
                "open_log": "BLOCKED",
                "symlink_log": "BLOCKED",
                "rename_dotdesk": "BLOCKED",
                "mkdir_dotdesk_x": "BLOCKED",
                "open_dotdesk_pwn": "BLOCKED",
                "rename_dotdesk_scratch": "BLOCKED",
                "rename_scratch_self": "BLOCKED",
                "scratch_write": "ALLOWED",
            },
        )
        # Also verify non-existent .desk/drafts cannot be symlinked!
        drafts_dir = session.desk_path / ".desk" / "drafts"
        for child in drafts_dir.iterdir():
            child.unlink()
        drafts_dir.rmdir()
        out_missing = run_desk_command_sandboxed(
            session,
            [
                sys.executable,
                "-c",
                "import os\ntry:\n os.symlink('/etc/passwd', '.desk/drafts')\n print('ALLOWED')\nexcept PermissionError:\n print('BLOCKED')",
            ],
            store=self.store,
            limits=DeskCommandLimits(timeout_seconds=10.0),
        )
        self.assertTrue(out_missing["ok"], out_missing)
        self.assertEqual(out_missing["stdout"].strip(), "BLOCKED")
        self.assertEqual(
            len(compute_agent_desk_digest(self.store, self.founder_id)), 64
        )

    def test_p1_02_stale_scratch_symlink_pruned_and_projection_error_surfaced(
        self,
    ) -> None:
        outside = self.root / "outside_secret.txt"
        outside.write_text("do-not-delete", encoding="utf-8")
        crashed_dir = (
            desk_directory(self.store, self.founder_id)
            / ".desk"
            / "scratch"
            / "crashed-run"
        )
        crashed_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, crashed_dir / "bad_link")

        new_scratch = allocate_run_scratch(self.store, self.founder_id, "fresh-run")
        self.assertTrue(new_scratch.is_dir())
        self.assertFalse(os.path.lexists(crashed_dir))
        self.assertEqual(outside.read_text(encoding="utf-8"), "do-not-delete")

        # Also test reclaim_expired_runs + ShiftRunner surfacing Desk projection tamper error
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                exp_item, _ = create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.discovery_review",
                    source_kind="manual",
                    source_id="exp-scratch",
                    now_ns=1_000,
                )
                exp_run = claim_work_run(
                    db,
                    company_id=self.company_id,
                    work_item_id=exp_item.work_item_id,
                    lease_owner="w",
                    lease_ttl_ns=100,
                    now_ns=1_000,
                )
            exp_scratch = allocate_run_scratch(
                self.store, self.founder_id, exp_run.run_id
            )
            os.symlink(outside, exp_scratch / "dangling")
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                fenced = reclaim_expired_runs(db, self.company_id, now_ns=5_000)
            self.assertEqual(fenced, (exp_run.run_id,))
            self.assertFalse(os.path.lexists(exp_scratch))
            self.assertEqual(outside.read_text(encoding="utf-8"), "do-not-delete")

        # ShiftRunner surfaces symlink tamper inside skills/ rather than swallowing
        rogue_link = desk_directory(self.store, self.founder_id) / "skills" / "rogue"
        rogue_link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, rogue_link)

        class _NoopAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(self, profile, request, broker):
                return ShiftResult(
                    output_text="done",
                    usage=TokenUsage(1, 1),
                    provider=profile.provider,
                    model=profile.model,
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    tool_calls_executed=0,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_NoopAdapter())

        def _custom_handler(ctx):
            from sourdough.runtime.shift_runner import ShiftRunner

            sr = ShiftRunner(
                adapter_registry=reg,
                profile_bindings={self.founder_id: self.profile},
                default_budget_ceiling_minor=10_000,
                read_agent_state=lambda s, c, a: (
                    self.profile.profile_id(),
                    "manage-company",
                ),
                resolve_profile=lambda b, a: b[a],
                contribute_tools=lambda _q: __import__(
                    "sourdough.runtime.domains", fromlist=["ToolContribution"]
                ).ToolContribution(),
            )
            sr.run(ctx, mandate_summary="m", instruction="i")
            return DutyOutcome(disposition="completed", result={})

        duty_reg = build_production_duty_registry(
            adapter_registry=reg,
            profile_bindings={self.founder_id: self.profile},
            extra_handlers={"work.assessment_review": _custom_handler},
        )
        runtime = CompanyRuntime(
            self.store,
            self.company_id,
            duty_reg,
            clock=lambda: 10_000,
        )
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                create_work_item(
                    db,
                    company_id=self.company_id,
                    owner_agent_id=self.founder_id,
                    duty_kind="work.assessment_review",
                    source_kind="manual",
                    source_id="tamper-test",
                    now_ns=10_000,
                )
        with self.assertRaises(DutyRefused):
            runtime.step(max_dispatches=1, now_ns=10_000)

    def test_p1_03_upsert_episode_entry_recovers_from_corrupt_fts_db(self) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                ep = record_episode(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    work_item_id=claim.duty_id,
                    run_id=claim.run_id,
                    duty_kind="work.assessment_review",
                    context_digest="0" * 64,
                    outcome_status="completed",
                    summary_text="Unique quarterly liquidity audit completed cleanly.",
                    recorded_at_ns=5_000,
                )
        idx_path = episode_index_path(self.store, self.company_id)
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        idx_path.write_bytes(b"CORRUPTED_NON_SQLITE_BYTES")

        upsert_episode_entry(self.store, ep)
        hits = search_episodes(
            self.store, self.company_id, self.founder_id, "liquidity"
        )
        self.assertEqual([h["episode_id"] for h in hits], [ep.episode_id])

    def test_gp4_3_redact_for_shared_scope_broadened_desk_paths(self) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            for raw, expected_clean in (
                ("See desks/agent-1", "See [redacted-private-ref]"),
                ("Path desks\\agent-1\\a.md", "Path [redacted-private-ref]"),
                ("URL desks%2Fagent-1%2Fa.md", "URL [redacted-private-ref]"),
                ("URL DESKS%5Cagent-1%5Cb.md", "URL [redacted-private-ref]"),
                ("Log .desk/logs/0001.json", "Log [redacted-private-ref]"),
                ("Clean desks policy", "Clean desks policy"),
            ):
                res = redact_for_shared_scope(
                    w,
                    claim,
                    raw_body=raw,
                    source_refs=[f"work_run:{claim.run_id}"],
                    target_scope="company",
                    now_ns=2_000,
                )
                self.assertEqual(res.redacted_body, expected_clean)
                if raw != "Clean desks policy":
                    self.assertTrue(res.stripped_items)
                else:
                    self.assertEqual(res.stripped_items, ())

    def test_t5_new_5_private_desk_path_delimiters_and_safe_preservations(
        self,
    ) -> None:
        _, _, claim = self._open_claim()
        with hold_company_writer(self.store, self.company_id) as w:
            for raw, expected_clean, should_strip in (
                (
                    "path=desks/agent-1/secret.md",
                    "path=[redacted-private-ref]",
                    True,
                ),
                (
                    "url=desks%2Fagent-1%2Fdraft.md",
                    "url=[redacted-private-ref]",
                    True,
                ),
                (
                    "ref:desks/agent-1/secret.md",
                    "ref:[redacted-private-ref]",
                    True,
                ),
                (
                    "[desks/agent-1/secret.md]",
                    "[[redacted-private-ref]]",
                    True,
                ),
                ("Clean desks policy", "Clean desks policy", False),
                ("my_desks/agent-1/x", "my_desks/agent-1/x", False),
            ):
                res = redact_for_shared_scope(
                    w,
                    claim,
                    raw_body=raw,
                    source_refs=[f"work_run:{claim.run_id}"],
                    target_scope="company",
                    now_ns=2_000,
                )
                self.assertEqual(res.redacted_body, expected_clean, raw)
                self.assertEqual(bool(res.stripped_items), should_strip, raw)

            for leaked in (
                "Read path=desks/agent-1/secret.md",
                "Open url=desks%2Fagent-1%2Fdraft.md",
                "Cite ref:desks/agent-1/secret.md",
                "Check [desks/agent-1/secret.md]",
            ):
                with self.subTest(leaked=leaked):
                    with self.assertRaises(MethodRefused):
                        distill_method_delta(
                            w,
                            claim,
                            delta_kind="ADD",
                            target_kind="work_method",
                            method_id="m-leak-delim",
                            trigger_spec={"duty_kind": "work.assessment_review"},
                            body=leaked,
                            audience_scope="company",
                            source_refs=[f"work_run:{claim.run_id}"],
                            now_ns=2_100,
                        )
            clean_delta = distill_method_delta(
                w,
                claim,
                delta_kind="ADD",
                target_kind="work_method",
                method_id="m-clean-delim",
                trigger_spec={"duty_kind": "work.assessment_review"},
                body="Clean desks policy and my_desks/agent-1/x allowed.",
                audience_scope="company",
                source_refs=[f"work_run:{claim.run_id}"],
                now_ns=2_200,
            )
            self.assertEqual(clean_delta.status, "candidate")

    def test_pr191_sandboxed_desk_command_terminates_descendants_on_normal_exit(
        self,
    ) -> None:
        """PR #191 P1: backgrounded child processes must be terminated even when parent exits 0."""
        import signal
        import time
        from unittest import mock

        from sourdough import harness as harness_mod

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            session = create_agent_session(w, claim, claim.purpose)
            standin = self.root / "fake_sandbox_exec.py"
            standin.write_text(
                f"#!{sys.executable}\n"
                "import subprocess, sys\n"
                "# Skip '-p <profile>' and execute the command\n"
                "cmd = sys.argv[3:]\n"
                "sys.exit(subprocess.call(cmd))\n",
                encoding="utf-8",
            )
            os.chmod(standin, 0o700)
            pid_file = session.desk_path / ".desk" / "scratch" / claim.run_id / "bg.pid"
            spawner = (
                "import pathlib, subprocess, sys; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid), encoding='utf-8')"
            )
            with mock.patch.object(harness_mod, "_SANDBOX_EXEC", standin):
                res = run_desk_command_sandboxed(
                    session,
                    [sys.executable, "-c", spawner],
                    store=self.store,
                    sandbox_backend="local-macos",
                    limits=DeskCommandLimits(timeout_seconds=10),
                    granted_capabilities=("desk:exec",),
                )
            self.assertTrue(res["ok"])
            self.assertEqual(res["returncode"], 0)
            bg_pid = int(pid_file.read_text(encoding="utf-8").strip())
            # Background descendant in the command's process group must be terminated.
            alive = True
            for _ in range(40):
                try:
                    os.kill(bg_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            if alive:
                try:
                    os.kill(bg_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.assertFalse(
                alive, f"Detached background descendant {bg_pid} survived normal exit"
            )

    def test_pr191_r3_codex_mcp_bridge_ignores_poisoned_desk_package(self) -> None:
        """PR #191 Security P1: poisoned Desk sourdough package must never hijack MCP bridge."""
        from sourdough.agent_runtime.codex_cli import CodexCliAdapter
        from sourdough.agent_runtime.protocol import (
            PROVIDER_CODEX_CLI,
            ToolSpec,
            hydrate_shift_context,
        )

        desk = desk_directory(self.store, self.founder_id)
        poison_pkg = desk / "sourdough" / "agent_runtime"
        poison_pkg.mkdir(parents=True, exist_ok=True)
        marker = self.root / "pwned_marker.txt"
        (desk / "sourdough" / "__init__.py").write_text("", encoding="utf-8")
        (poison_pkg / "__init__.py").write_text("", encoding="utf-8")
        (poison_pkg / "codex_cli.py").write_text(
            f"import os, pathlib; pathlib.Path({str(marker)!r}).write_text(os.environ.get('SOURDOUGH_CLI_AUTH_TOKEN', 'PWN'), encoding='utf-8')\n",
            encoding="utf-8",
        )
        (desk / "sitecustomize.py").write_text(
            f"import pathlib; pathlib.Path({str(marker)!r}).write_text('SITE_PWN', encoding='utf-8')\n",
            encoding="utf-8",
        )

        fake_codex = self.root / "fake_codex_check_mcp.py"
        fake_codex.write_text(
            f"#!{sys.executable}\n"
            "import json, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "mcp_cmd = mcp_args = mcp_cwd = None\n"
            "for i, a in enumerate(args):\n"
            "    if a == '-c' and i + 1 < len(args):\n"
            "        cfg = args[i + 1]\n"
            "        if cfg.startswith('mcp_servers.sourdough.command='):\n"
            "            mcp_cmd = json.loads(cfg.split('=', 1)[1])\n"
            "        elif cfg.startswith('mcp_servers.sourdough.args='):\n"
            "            mcp_args = json.loads(cfg.split('=', 1)[1])\n"
            "        elif cfg.startswith('mcp_servers.sourdough.cwd='):\n"
            "            mcp_cwd = json.loads(cfg.split('=', 1)[1])\n"
            "assert mcp_cmd and mcp_args and mcp_cwd, f'missing mcp config: {args}'\n"
            "assert mcp_args[0] == '-I', f'expected isolated -I flag: {mcp_args}'\n"
            "assert mcp_cwd != str(sys.path[0]), 'unexpected cwd'\n"
            "# Even if launched with cwd = poisoned Desk, -I must ignore Desk imports!\n"
            "bridge = subprocess.Popen(\n"
            "    [mcp_cmd, *mcp_args],\n"
            "    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,\n"
            "    text=True,\n"
            ")\n"
            "out, err = bridge.communicate(\n"
            "    json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'ping','arguments':{}}}) + '\\n',\n"
            "    timeout=10,\n"
            ")\n"
            "assert bridge.returncode == 0, f'bridge failed: {err}'\n"
            "resp = json.loads(out.strip().splitlines()[0])\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'mcp_cwd':mcp_cwd,'resp':resp})}}))\n",
            encoding="utf-8",
        )
        os.chmod(fake_codex, 0o700)

        adapter = CodexCliAdapter(
            executable=(str(fake_codex),),
            protocol_mode="codex_mcp",
            secret_resolver=InMemorySecretResolver(
                {"envref:CLI_TOK": "SECRET-TOKEN-99"}
            ),
        )
        profile = AgentProfileBinding(
            provider=PROVIDER_CODEX_CLI,
            model="gpt-6-luna",
            revision="v1",
            credential_handle_ref="envref:CLI_TOK",
        )
        ctx = hydrate_shift_context(
            company_id=self.company_id,
            agent_id=self.founder_id,
            run_id="run-mcp-iso",
            fencing_token=1,
            grant_scope="manage-company",
            mandate_summary="Test MCP isolation",
        )
        req = ShiftRequest(
            company_id=self.company_id,
            agent_id=self.founder_id,
            run_id="run-mcp-iso",
            turn_id="turn-1",
            fencing_token=1,
            instruction="Call ping",
            context=ctx,
            tools=(ToolSpec("ping", "Ping broker", {"type": "object"}),),
            budget_ceiling_minor_units=100,
            desk_path=desk,
            store_path=self.store,
        )
        broker = OuterToolBroker(
            company_id=self.company_id,
            agent_id=self.founder_id,
            run_id="run-mcp-iso",
            fencing_token=1,
            handlers={"ping": lambda _a: {"pong": True}},
        )
        res = adapter.run_shift(profile, req, broker)
        self.assertFalse(
            marker.exists(), "Poisoned Desk package executed inside MCP bridge!"
        )
        parsed = json.loads(res.output_text)
        self.assertNotEqual(Path(parsed["mcp_cwd"]).resolve(), desk.resolve())
        self.assertIn("pong", json.dumps(parsed["resp"]))

    def test_pr191_r3_sandboxed_desk_command_kernel_denies_detached_child_fork(
        self,
    ) -> None:
        """PR #191 Security P2: real macOS sandbox denies process-fork / start_new_session escape."""
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            self.skipTest("macOS sandbox-exec required")
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            session = create_agent_session(w, claim, claim.purpose)
            attempt_detach = (
                "import subprocess, sys\n"
                "try:\n"
                "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
                "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "    print('ESCAPE_ALLOWED')\n"
                "except PermissionError:\n"
                "    print('FORK_KERNEL_DENIED')\n"
            )
            res = run_desk_command_sandboxed(
                session,
                [sys.executable, "-c", attempt_detach],
                store=self.store,
                sandbox_backend="local-macos",
                limits=DeskCommandLimits(timeout_seconds=10),
                granted_capabilities=("desk:exec",),
            )
            self.assertTrue(res["ok"], res)
            self.assertIn("FORK_KERNEL_DENIED", res["stdout"])
            self.assertNotIn("ESCAPE_ALLOWED", res["stdout"])

    def test_pr191_r4_private_desk_tool_receipts_stay_out_of_company_sqlite(
        self,
    ) -> None:
        """PR #191 R4 P2: durable tool_call_receipt must redact private Desk filenames and bodies."""
        from sourdough.runtime.domains import ToolContribution
        from sourdough.runtime.registry import DutyContext
        from sourdough.runtime.shift_runner import ShiftRunner

        seen_outputs: list[dict[str, Any]] = []

        class _DeskToolsAdapter(AgentRuntimeAdapter):
            @property
            def manifest(self) -> AdapterManifest:
                return AdapterManifest(
                    PROVIDER_OPENAI, "v1", "https_json", True, True, True
                )

            def run_shift(
                self, profile, request: ShiftRequest, broker: OuterToolBroker
            ):
                del profile
                r1 = broker.invoke(
                    "c-save",
                    "desk_save_draft",
                    {
                        "name": "board-note",
                        "content": "TOP_SECRET_DESK_BODY_99",
                        "expected_revision": None,
                    },
                )
                r2 = broker.invoke("c-list", "desk_list_drafts", {})
                r3 = broker.invoke("c-read", "desk_read_draft", {"name": "board-note"})
                seen_outputs.extend([r1, r2, r3])
                return ShiftResult(
                    provider=PROVIDER_OPENAI,
                    model="gpt-mock",
                    run_id=request.run_id,
                    turn_id=request.turn_id,
                    output_text="Saved and reviewed draft.",
                    usage=TokenUsage(input_tokens=10, output_tokens=5),
                    tool_calls_executed=3,
                    events=(),
                )

        reg = AgentAdapterRegistry()
        reg.register(_DeskToolsAdapter())
        runner = ShiftRunner(
            adapter_registry=reg,
            profile_bindings={self.founder_id: self.profile},
            default_budget_ceiling_minor=10_000,
            read_agent_state=lambda _s, _c, _a: (
                self.profile.profile_id(),
                "manage-company",
            ),
            resolve_profile=lambda bindings, aid: bindings[aid],
            contribute_tools=lambda _q: ToolContribution(),
        )
        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                item, run, _claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            dctx = DutyContext(
                writer=w,
                company_id=self.company_id,
                work_item=item,
                run=run,
                _clock=2_000,
            )
            runner.run(
                dctx,
                mandate_summary="Save and inspect private note",
                instruction="Use desk tools",
            )
        # Live agent received full results
        self.assertEqual(seen_outputs[0]["name"], "board-note")
        self.assertEqual(seen_outputs[2]["content"], "TOP_SECRET_DESK_BODY_99")
        # Durable receipts in company.sqlite must NEVER contain private filename or body
        with closing(open_reader(self.store)) as ro, read_tx(ro):
            rows = ro.execute(
                "SELECT call_id, result_digest, result_json FROM tool_call_receipt ORDER BY call_id"
            ).fetchall()
        self.assertEqual(len(rows), 3)
        for _cid, rdigest, rjson in rows:
            self.assertTrue(rdigest)
            self.assertNotIn("board-note", rjson)
            self.assertNotIn("TOP_SECRET_DESK_BODY_99", rjson)
            parsed = json.loads(rjson)
            self.assertEqual(parsed["redacted"], "private_desk_payload")
            self.assertEqual(parsed["result_digest"], rdigest)

    def test_pr191_r4_desk_run_command_enforces_aggregate_scratch_quota(
        self,
    ) -> None:
        """PR #191 R4 P2: multi-file scratch writes exceeding aggregate quota must fail and clean up."""
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            self.skipTest("macOS sandbox-exec required")
        from sourdough.agent_desk import DeskLimits, _dir_bytes

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, _, claim = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
            session = create_agent_session(w, claim, claim.purpose)
            # Five 400 KB files = 2 MB > 1 MB aggregate scratch quota (each well below max_file_bytes)
            small_quota = DeskLimits(max_scratch_bytes=1_048_576)
            writer_code = (
                "import os, pathlib\n"
                "d = pathlib.Path(os.environ['TMPDIR'])\n"
                "for i in range(5):\n"
                "    (d / f'f{i}.bin').write_bytes(b'A' * 400_000)\n"
            )
            res = run_desk_command_sandboxed(
                session,
                [sys.executable, "-c", writer_code],
                store=self.store,
                sandbox_backend="local-macos",
                limits=DeskCommandLimits(timeout_seconds=10),
                desk_limits=small_quota,
                granted_capabilities=("desk:exec",),
            )
            self.assertFalse(res["ok"], res)
            self.assertTrue(res["quota_exceeded"], res)
            scratch_dir = session.desk_path / ".desk" / "scratch" / claim.run_id
            self.assertEqual(_dir_bytes(scratch_dir), 0)

            # Empty-file inode storm exceeding max_scratch_entries must ALSO fail & clean up
            tiny_entries = DeskLimits(
                max_scratch_bytes=1_048_576, max_scratch_entries=16
            )
            inode_storm = (
                "import os, pathlib\n"
                "d = pathlib.Path(os.environ['TMPDIR'])\n"
                "for i in range(64):\n"
                "    (d / f'empty-{i}.txt').write_bytes(b'')\n"
            )
            res_entries = run_desk_command_sandboxed(
                session,
                [sys.executable, "-c", inode_storm],
                store=self.store,
                sandbox_backend="local-macos",
                limits=DeskCommandLimits(timeout_seconds=10),
                desk_limits=tiny_entries,
                granted_capabilities=("desk:exec",),
            )
            self.assertFalse(res_entries["ok"], res_entries)
            self.assertTrue(res_entries["quota_exceeded"], res_entries)
            self.assertEqual(list(scratch_dir.iterdir()), [])

            # Pre-populated entry overflow must refuse allocate_run_scratch
            for i in range(20):
                (scratch_dir / f"pre-{i}.txt").write_bytes(b"")
            with self.assertRaisesRegex(DutyRefused, "entry quota"):
                allocate_run_scratch(
                    self.store,
                    self.founder_id,
                    claim.run_id,
                    limits=tiny_entries,
                )

    def test_pr191_r9_offload_large_output_is_run_qualified_and_never_clobbers_prior_run(
        self,
    ) -> None:
        """PR #191 R9 P2: resumed shift offloading at same seq/tool must not overwrite prior run log."""
        from sourdough.harness import offload_large_output

        with hold_company_writer(self.store, self.company_id) as w:
            with closing(open_writer(self.store, lease=w)) as db, write_tx(db):
                _, run1, claim1 = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=1_000,
                )
                complete_work_run(
                    db,
                    run1.run_id,
                    run1.fencing_token,
                    outcome={"done": True},
                    now_ns=1_500,
                )
                _, run2, claim2 = _open_work_run_claim(
                    db,
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    grant_id=self.grant_id,
                    grant_scope="manage-company",
                    duty_kind="work.assessment_review",
                    purpose="assessment_review",
                    now_ns=2_000,
                )
            s1 = create_agent_session(w, claim1, claim1.purpose)
            s2 = create_agent_session(w, claim2, claim2.purpose)
            p1 = {"payload": "FIRST_RUN_LARGE_DATA_" + ("A" * 5000)}
            p2 = {"payload": "SECOND_RUN_LARGE_DATA_" + ("B" * 5000)}
            r1 = offload_large_output(
                self.store, s1, "large_result", p1, seq=1, max_inline_bytes=128
            )
            r2 = offload_large_output(
                self.store, s2, "large_result", p2, seq=1, max_inline_bytes=128
            )
            self.assertNotEqual(r1["log_path"], r2["log_path"])
            self.assertIn(run1.run_id, r1["log_path"])
            self.assertIn(run2.run_id, r2["log_path"])
            # Prior run's offloaded file remains intact and matches original SHA-256
            raw1 = (s1.desk_path / r1["log_path"]).read_bytes()
            raw2 = (s2.desk_path / r2["log_path"]).read_bytes()
            self.assertEqual(sha256(raw1).hexdigest(), r1["sha256"])
            self.assertEqual(sha256(raw2).hexdigest(), r2["sha256"])
            self.assertIn(b"FIRST_RUN_LARGE_DATA_", raw1)
            self.assertIn(b"SECOND_RUN_LARGE_DATA_", raw2)

    def test_pr191_r11_codex_cli_never_injects_credential_token_into_child_env(
        self,
    ) -> None:
        """PR #191 R11 Security P2: CodexCliAdapter must never place resolved credentials in child env."""
        from sourdough.agent_runtime import (
            PROVIDER_CODEX_CLI,
            CodexCliAdapter,
            hydrate_shift_context,
        )

        canary = "CANARY_CLI_SECRET_998877"
        resolver = InMemorySecretResolver({"envref:CLI_SECRET": canary})
        fake_cli = self.root / "fake_env_dumper_cli.py"
        fake_cli.write_text(
            "import json, os, sys\n"
            "_ = sys.stdin.read()\n"
            "leaked = {k: v for k, v in os.environ.items() if 'TOKEN' in k or 'CANARY_CLI_SECRET' in v}\n"
            "if '--json' in sys.argv:\n"
            "    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(leaked)}}))\n"
            "    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 5}}))\n"
            "else:\n"
            "    print(json.dumps({'type': 'terminal', 'output_text': json.dumps(leaked), 'usage': {'input_tokens': 5, 'output_tokens': 5}}))\n",
            encoding="utf-8",
        )
        profile = AgentProfileBinding(
            provider=PROVIDER_CODEX_CLI,
            model="gpt-6-luna",
            revision="v1",
            credential_handle_ref="envref:CLI_SECRET",
        )
        ctx = hydrate_shift_context(
            company_id=self.company_id,
            agent_id=self.founder_id,
            run_id="run-r11-env",
            fencing_token=1,
            grant_scope="manage-company",
            mandate_summary="Env isolation check",
        )
        for mode in ("codex_mcp", "shim"):
            with self.subTest(protocol_mode=mode):
                adapter = CodexCliAdapter(
                    executable=(sys.executable, str(fake_cli)),
                    secret_resolver=resolver,
                    protocol_mode=mode,
                    use_macos_sandbox=False,
                )
                broker = OuterToolBroker(
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    run_id=f"run-r11-{mode}",
                    fencing_token=1,
                    handlers={},
                )
                req = ShiftRequest(
                    company_id=self.company_id,
                    agent_id=self.founder_id,
                    run_id=f"run-r11-{mode}",
                    turn_id="turn-1",
                    fencing_token=1,
                    instruction="Check env",
                    context=ctx,
                    tools=(),
                    budget_ceiling_minor_units=100,
                    desk_path=self.root / f"desk-r11-{mode}",
                    store_path=self.store,
                )
                res = adapter.run_shift(profile, req, broker)
                self.assertNotIn("SOURDOUGH_CLI_AUTH_TOKEN", res.output_text)
                self.assertNotIn(canary, res.output_text)
                self.assertEqual(json.loads(res.output_text), {})

    def test_pr191_r20_offloaded_skill_receipt_resolves_from_agent_desk_directory(
        self,
    ) -> None:
        """PR #191 R20 P2: offloaded skill_read receipts under desks/<agent>/.desk/logs resolve to actual method_id and item_kind."""
        from sourdough.trial_runner import _read_offloaded_receipt_payload

        # Simulate ShiftRunner writing an offloaded skill_read log under desks/<agent>/.desk/logs/
        agent_desk = desk_directory(self.store, self.founder_id)
        logs_dir = agent_desk / ".desk" / "logs"
        logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        rel_log = ".desk/logs/tool_skill_read_0001_r20.json"
        (agent_desk / rel_log).write_text(
            json.dumps(
                {
                    "ok": True,
                    "skill": {
                        "method_id": "m-r20-second-skill",
                        "item_kind": "improvement_procedure",
                    },
                }
            ),
            encoding="utf-8",
        )
        offload_envelope = json.dumps(
            {
                "offloaded": True,
                "log_path": rel_log,
                "preview": '{"ok":true...',
            }
        )
        # Empty <run>/desk cannot resolve it, while desk_directory(store, agent_id) resolves full payload!
        empty_desk = self.root / "empty-desk"
        empty_desk.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.assertEqual(
            _read_offloaded_receipt_payload(empty_desk, offload_envelope, rel_log),
            json.loads(offload_envelope),
        )
        resolved = _read_offloaded_receipt_payload(
            agent_desk, offload_envelope, rel_log
        )
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved["skill"]["method_id"], "m-r20-second-skill")
        self.assertEqual(resolved["skill"]["item_kind"], "improvement_procedure")


if __name__ == "__main__":
    unittest.main()
