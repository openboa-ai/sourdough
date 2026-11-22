import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.cli import main
from sourdough.company import FoundingTerms, found
from sourdough.company_run import CompanyRun
from sourdough.nautilus_paper import NautilusPaperVenue
from sourdough.supervisor import SupervisionResult


class ManagedCompanyDestination(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.engine = self.root / "engine"
        company = found(
            self.store, FoundingTerms("owner", "charter", "paper", "profile")
        )
        self.scope = [
            str(self.store),
            str(self.engine),
            "--company-id",
            company.company_id,
            "--employee",
            str(self.root / "employee"),
            "--managed-engine",
        ]

    def test_supervisors_forward_the_same_explicit_destination_on_every_attempt(self):
        for role, worker in (
            ("supervise-paper", "_paper-worker"),
            ("supervise-hired-paper", "_hired-paper-worker"),
        ):
            with (
                self.subTest(role=role),
                patch("sys.argv", ["sourdough", role, *self.scope]),
                patch("builtins.print"),
                patch(
                    "sourdough.cli.supervise_worker",
                    return_value=SupervisionResult((), True),
                ) as supervise,
            ):
                self.assertEqual(main(), 0)
                command, original_store, _ = supervise.call_args.args
                self.assertEqual(original_store, self.store)
                self.assertEqual(command[4], worker)
                self.assertEqual(command[6], str(self.engine))
                self.assertEqual(command.count("--managed-engine"), 1)

    def test_founder_and_hired_worker_reopen_the_engine_journal_without_fallback(self):
        expected = self.engine / "nautilus-1.231.0/sandbox.sqlite"
        for command, function in (
            ("_paper-worker", "run_paper_company"),
            ("_hired-paper-worker", "run_hired_paper_company"),
        ):
            with (
                self.subTest(command=command),
                patch(
                    "sys.argv",
                    [
                        "sourdough",
                        command,
                        *self.scope,
                        "--ready-fd",
                        "5",
                        "--store-key",
                        "1:2",
                        "--budget-from",
                        "0",
                    ],
                ),
                patch("builtins.print"),
                patch("sourdough.cli.EmployeeProcess"),
                patch("sourdough.cli.FixedQuoteVenue") as fixture,
                patch(
                    f"sourdough.cli.{function}", return_value=CompanyRun(None, None)
                ) as run,
            ):
                self.assertEqual(main(), 0)
                destination = run.call_args.args[2]
                self.assertIsInstance(destination, NautilusPaperVenue)
                self.assertEqual(destination.journal, expected.resolve())
                fixture.assert_not_called()
