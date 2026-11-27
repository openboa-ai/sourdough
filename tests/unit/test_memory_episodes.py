"""Unit tests for $0 Agent episodic memory and physically separate FTS5 episode index."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found
from sourdough.episode_index import (
    episode_index_path,
    rebuild_episodes_index,
    search_episodes,
    sync_episodes_index,
    upsert_episode_entry,
)
from sourdough.memory import (
    MemoryRefused,
    list_recent_episodes,
    read_episode,
    record_episode,
)
from sourdough.runtime.work_kernel import (
    claim_work_run,
    complete_work_run,
    create_work_item,
)
from sourdough.storage.db import open_reader, open_writer, read_tx, write_tx
from sourdough.writer_lease import hold_company_writer


class EpisodicMemoryAndSeparateFtsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="sd-mem-")))
        self.store = self.root / "company.sqlite"
        self.leases = self.root / "leases"
        self.founding = found(
            self.store,
            FoundingTerms("setup-mem", "charter-v1", "manage-company", "profile-v1"),
        )
        self.company_id = self.founding.company_id
        self.agent_a = self.founding.founder_id
        self.agent_b = "agent-peer-b"
        self.writer = self.enterContext(
            hold_company_writer(
                self.store, self.company_id, lease_directory=self.leases
            )
        )
        with closing(open_writer(self.store, lease=self.writer)) as db, write_tx(db):
            db.execute(
                "INSERT INTO agent VALUES (?, ?, 'profile-v1')",
                (self.agent_b, self.company_id),
            )
            db.execute(
                "INSERT INTO standing_grant VALUES ('grant-b', ?, 'assessment')",
                (self.agent_b,),
            )

    def _make_run(self, agent_id: str, suffix: str, now_ns: int) -> tuple[str, str]:
        with closing(open_writer(self.store, lease=self.writer)) as db, write_tx(db):
            item, _ = create_work_item(
                db,
                company_id=self.company_id,
                owner_agent_id=agent_id,
                duty_kind="work.assessment_review",
                source_kind="manual",
                source_id=f"src-{suffix}",
                now_ns=now_ns,
            )
            run = claim_work_run(
                db,
                company_id=self.company_id,
                work_item_id=item.work_item_id,
                lease_owner="worker-1",
                lease_ttl_ns=60_000_000_000,
                now_ns=now_ns,
            )
            complete_work_run(
                db,
                run.run_id,
                run.fencing_token,
                outcome={"disposition": "completed"},
                now_ns=now_ns + 1,
            )
        return item.work_item_id, run.run_id

    def test_record_episode_idempotent_strict_schema_and_agent_isolation(self):
        wi_a, run_a = self._make_run(self.agent_a, "a1", 10_000)
        wi_b, run_b = self._make_run(self.agent_b, "b1", 20_000)

        with closing(open_writer(self.store, lease=self.writer)) as db, write_tx(db):
            ep_a = record_episode(
                db,
                company_id=self.company_id,
                agent_id=self.agent_a,
                work_item_id=wi_a,
                run_id=run_a,
                duty_kind="work.assessment_review",
                context_digest="ctx-a1",
                active_snapshot_id="snap-1",
                outcome_status="completed",
                summary_text="Reconciled quarterly variance and flagged liquidity buffer.",
                decisions=[{"action": "publish", "target": "proj-1"}],
                evidence_refs=["shared_publication:asset-1"],
                receipts=["rcpt-1"],
                tool_calls_count=3,
                input_tokens=120,
                output_tokens=45,
                cost_minor_units=2,
                recorded_at_ns=15_000,
            )
            # Idempotent replay on same (company_id, run_id) returns identical episode
            ep_a_dup = record_episode(
                db,
                company_id=self.company_id,
                agent_id=self.agent_a,
                work_item_id=wi_a,
                run_id=run_a,
                duty_kind="work.assessment_review",
                context_digest="ctx-other",
                outcome_status="failed",
                summary_text="Should not overwrite",
                recorded_at_ns=99_000,
            )
            self.assertEqual(ep_a_dup, ep_a)

            ep_b = record_episode(
                db,
                company_id=self.company_id,
                agent_id=self.agent_b,
                work_item_id=wi_b,
                run_id=run_b,
                duty_kind="work.assessment_review",
                context_digest="ctx-b1",
                outcome_status="waiting",
                summary_text="Liquidity buffer peer secret note for agent B only.",
                recorded_at_ns=25_000,
            )

        with closing(open_reader(self.store)) as reader, read_tx(reader):
            # company.sqlite remains 100% STRICT with zero FTS5 tables
            non_strict = reader.execute(
                """SELECT name FROM pragma_table_list
                   WHERE schema = 'main' AND type = 'table'
                     AND name NOT LIKE 'sqlite_%' AND strict != 1"""
            ).fetchall()
            self.assertEqual(non_strict, [])
            fts_in_company = reader.execute(
                "SELECT name FROM sqlite_schema WHERE lower(COALESCE(sql, '')) LIKE '%fts5%'"
            ).fetchall()
            self.assertEqual(fts_in_company, [])

            # Strictly agent-scoped reads
            self.assertEqual(
                read_episode(reader, self.company_id, self.agent_a, ep_a.episode_id),
                ep_a,
            )
            with self.assertRaises(MemoryRefused):
                read_episode(reader, self.company_id, self.agent_a, ep_b.episode_id)
            with self.assertRaises(MemoryRefused):
                read_episode(reader, "wrong-co", self.agent_a, ep_a.episode_id)

            recent_a = list_recent_episodes(reader, self.company_id, self.agent_a)
            self.assertEqual(recent_a, (ep_a,))
            recent_b = list_recent_episodes(reader, self.company_id, self.agent_b)
            self.assertEqual(recent_b, (ep_b,))

    def test_separate_fts5_index_scoping_sanitization_and_rebuild(self):
        wi_a1, run_a1 = self._make_run(self.agent_a, "sa1", 10_000)
        wi_a2, run_a2 = self._make_run(self.agent_a, "sa2", 20_000)
        wi_b1, run_b1 = self._make_run(self.agent_b, "sb1", 30_000)

        with closing(open_writer(self.store, lease=self.writer)) as db, write_tx(db):
            ep_a1 = record_episode(
                db,
                company_id=self.company_id,
                agent_id=self.agent_a,
                work_item_id=wi_a1,
                run_id=run_a1,
                duty_kind="work.assessment_review",
                context_digest="c1",
                outcome_status="completed",
                summary_text="Quarterly variance audit completed cleanly.",
                recorded_at_ns=11_000,
            )
            ep_a2 = record_episode(
                db,
                company_id=self.company_id,
                agent_id=self.agent_a,
                work_item_id=wi_a2,
                run_id=run_a2,
                duty_kind="work.discovery_review",
                context_digest="c2",
                outcome_status="completed",
                summary_text="Deferred duplicate reminder for tomorrow.",
                recorded_at_ns=21_000,
            )
            ep_b1 = record_episode(
                db,
                company_id=self.company_id,
                agent_id=self.agent_b,
                work_item_id=wi_b1,
                run_id=run_b1,
                duty_kind="work.assessment_review",
                context_digest="c3",
                outcome_status="completed",
                summary_text="Confidential quarterly variance private to B.",
                recorded_at_ns=31_000,
            )

        idx_path = episode_index_path(self.store, self.company_id)
        self.assertNotIn("desks", idx_path.parts)
        self.assertEqual(idx_path.parent.parent.name, ".sourdough-indexes")

        upsert_episode_entry(self.store, ep_a1)
        self.assertTrue(idx_path.is_file())
        # sync picks up ep_a2 and ep_b1
        synced = sync_episodes_index(self.store, self.company_id)
        self.assertGreaterEqual(synced, 2)

        # Agent A finds only Agent A's matching episode (never Agent B's!)
        hits_a = search_episodes(
            self.store, self.company_id, self.agent_a, "quarterly variance"
        )
        self.assertEqual([h["episode_id"] for h in hits_a], [ep_a1.episode_id])

        # Agent B finds only Agent B's matching episode
        hits_b = search_episodes(
            self.store, self.company_id, self.agent_b, "quarterly variance"
        )
        self.assertEqual([h["episode_id"] for h in hits_b], [ep_b1.episode_id])

        # Arbitrary punctuation / FTS5 syntax injection does not raise OperationalError
        odd_hits = search_episodes(
            self.store,
            self.company_id,
            self.agent_a,
            'AND OR NOT "variance" (audit): *^',
        )
        self.assertEqual([h["episode_id"] for h in odd_hits], [ep_a1.episode_id])

        # Delete & corrupt index -> search_episodes auto-rebuilds from company.sqlite!
        idx_path.unlink()
        self.assertFalse(idx_path.exists())
        rebuilt_hits = search_episodes(
            self.store, self.company_id, self.agent_a, "deferred reminder"
        )
        self.assertEqual([h["episode_id"] for h in rebuilt_hits], [ep_a2.episode_id])

        # Explicit rebuild returns all 3 episodes
        self.assertEqual(rebuild_episodes_index(self.store, self.company_id), 3)

        # Corrupt index file and verify graceful auto-rebuild
        with closing(sqlite3.connect(idx_path)) as raw, raw:
            raw.execute("DROP TABLE episodes_fts")
        recovered_hits = search_episodes(
            self.store, self.company_id, self.agent_a, "quarterly"
        )
        self.assertEqual([h["episode_id"] for h in recovered_hits], [ep_a1.episode_id])


if __name__ == "__main__":
    unittest.main()
