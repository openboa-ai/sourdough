import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from sourdough.company import FoundingTerms, found, reopen
from sourdough.paper_account import (
    BindingRefused,
    PaperAccount,
    PaperMandate,
    bind,
    reopen_binding,
)


class Destination:
    def __init__(self, account):
        self.account = account
        self.observations = 0

    def observe_account(self, account_id):
        self.observations += 1
        return self.account


class PaperBindingTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.store = Path(directory) / "company.sqlite"
        self.company = found(
            self.store, FoundingTerms("setup", "charter", "paper", "runtime")
        )
        self.account = PaperAccount(
            "account", "local-v1", "rules", "USD", 5000, "paper", "venue-id"
        )
        self.destination = Destination(self.account)
        self.mandate = PaperMandate(
            "mandate", self.company.company_id, "charter", self.account, 1000
        )

    def test_replay_and_reopen_preserve_one_binding_and_original_duty(self):
        bind(self.store, self.mandate, self.destination)
        bind(self.store, self.mandate, self.destination)
        self.assertEqual(
            reopen_binding(self.store, self.company.company_id), self.mandate
        )
        self.assertEqual(self.destination.observations, 2)
        self.assertEqual(
            reopen(self.store, self.company.company_id).due_work_ids,
            (self.company.due_work_id,),
        )
        with closing(sqlite3.connect(f"{self.store.as_uri()}?mode=ro", uri=True)) as db:
            self.assertEqual(
                db.execute("SELECT revision, account_id FROM paper_binding").fetchall(),
                [("mandate", "account")],
            )

    def test_mismatch_does_not_create_or_replace_binding(self):
        wrong = Destination(replace(self.account, account_id="different"))
        with self.assertRaises(BindingRefused):
            bind(self.store, self.mandate, wrong)
        with self.assertRaises(BindingRefused):
            reopen_binding(self.store, self.company.company_id)
        bind(self.store, self.mandate, self.destination)
        with self.assertRaises(BindingRefused):
            bind(self.store, replace(self.mandate, revision="other"), self.destination)
        self.assertEqual(
            reopen_binding(self.store, self.company.company_id), self.mandate
        )

    def test_live_environment_is_refused_before_destination_access(self):
        live = replace(self.mandate, account=replace(self.account, environment="live"))
        with self.assertRaises(BindingRefused):
            bind(self.store, live, self.destination)
        self.assertEqual(self.destination.observations, 0)
