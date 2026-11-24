import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.agent_desk import resolve_desk, save_draft
from sourdough.company import FoundingTerms, _connect, found
from sourdough.company_assets import (
    Selection,
    assign_project,
    publish_selection,
    read_shared_asset,
)
from sourdough.duty_run import DutyRefused, resolve_run, start_run
from sourdough.retirement import (
    Retirement,
    TERMINATION_SCOPE,
    employment_revision,
    retire_agent,
)
from sourdough.writer_lease import hold_company_writer


class RetirementFence(unittest.TestCase):
    def test_cutoff_blocks_new_and_stale_runs_but_preserves_the_admitted_asset(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        store = folder / "company.sqlite"
        company = found(
            store, FoundingTerms("retirement", "charter", "manage-company", "profile")
        )
        company_id = company.company_id
        with hold_company_writer(store, company_id) as writer:
            with closing(_connect(store, "rw")) as connection, connection:
                for agent, scope in (
                    ("manager", TERMINATION_SCOPE),
                    ("author", "assessment"),
                    ("peer", "assessment"),
                ):
                    connection.execute(
                        "INSERT INTO agent VALUES (?,?,'profile')", (agent, company_id)
                    )
                    connection.execute(
                        "INSERT INTO standing_grant VALUES (?,?,?)",
                        (f"{agent}-grant", agent, scope),
                    )
                    connection.execute(
                        "INSERT INTO private_desk VALUES (?,'revision')", (agent,)
                    )
                claims = {}
                for agent in ("author", "peer"):
                    duty = f"{agent}-duty"
                    connection.execute(
                        "INSERT INTO duty VALUES (?,?,'due')", (duty, agent)
                    )
                    claims[agent] = start_run(
                        connection,
                        company_id,
                        agent,
                        f"{agent}-grant",
                        duty,
                        "profile",
                        purpose="assessment",
                        grant_scope="assessment",
                    )
                connection.execute("INSERT INTO duty VALUES ('later','author','due')")
            project = assign_project(
                writer,
                company.grant_id,
                "review",
                ("author", "peer"),
                publishers=("author",),
            )
            draft = save_draft(
                writer,
                claims["author"],
                "assessment",
                name="record",
                content="Approved asset. Private context.",
                expected_revision=None,
            )
            selection = Selection(
                "prior",
                draft.name,
                draft.revision,
                "review",
                project,
                ("peer",),
                0,
                15,
            )
            asset = publish_selection(writer, claims["author"], "assessment", selection)
            revision = employment_revision(writer, "manager", "manager-grant", "author")
            request = Retirement(
                "release", "author", revision, "End the completed assignment."
            )
            with self.assertRaisesRegex(DutyRefused, "Current termination grant"):
                retire_agent(writer, company.founder_id, company.grant_id, request)
            with self.assertRaisesRegex(DutyRefused, "revision changed"):
                retire_agent(
                    writer,
                    "manager",
                    "manager-grant",
                    replace(request, employment_revision="stale"),
                )
            founder_revision = employment_revision(
                writer, "manager", "manager-grant", company.founder_id
            )
            with self.assertRaisesRegex(
                DutyRefused, "Cannot archive the founding owner"
            ):
                retire_agent(
                    writer,
                    "manager",
                    "manager-grant",
                    replace(
                        request,
                        agent_id=company.founder_id,
                        employment_revision=founder_revision,
                    ),
                )
            cutoff = retire_agent(writer, "manager", "manager-grant", request)
            self.assertEqual(cutoff.agent_id, "author")
            self.assertEqual(
                retire_agent(writer, "manager", "manager-grant", request), cutoff
            )
            with self.assertRaisesRegex(DutyRefused, "original request"):
                retire_agent(
                    writer,
                    "manager",
                    "manager-grant",
                    replace(request, reason="Other."),
                )
            with self.assertRaisesRegex(DutyRefused, "no longer current"):
                resolve_desk(writer, claims["author"], "assessment")
            with self.assertRaisesRegex(DutyRefused, "no longer current"):
                publish_selection(
                    writer,
                    claims["author"],
                    "assessment",
                    replace(selection, request_id="late"),
                )
            self.assertEqual(
                read_shared_asset(writer, claims["peer"], "assessment", asset.asset_id),
                asset,
            )

        with closing(_connect(store, "rw")) as observed, observed:
            self.assertEqual(
                observed.execute(
                    "SELECT cutoff,agent_id,previous_scope FROM agent_archive"
                ).fetchall(),
                [(cutoff.sequence, "author", "assessment")],
            )
            self.assertEqual(
                observed.execute(
                    "SELECT id,state FROM duty WHERE agent_id='author' ORDER BY id"
                ).fetchall(),
                [
                    ("author-duty", "archived"),
                    ("later", "archived"),
                ],
            )
            historical = resolve_run(observed, claims["author"])
            self.assertEqual(
                (historical.issued_scope, historical.current_scope),
                ("assessment", "revoked"),
            )
            self.assertFalse(historical.is_current)
            with self.assertRaisesRegex(DutyRefused, "employment is fenced"):
                start_run(
                    observed,
                    company_id,
                    "author",
                    "author-grant",
                    "later",
                    "profile",
                    purpose="assessment",
                    grant_scope="assessment",
                )
            self.assertEqual(
                observed.execute("SELECT count(*) FROM shared_publication").fetchone(),
                (1,),
            )
