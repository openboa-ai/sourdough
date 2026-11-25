import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sourdough.company import FoundingTerms, found
from sourdough.paper_funding import (
    FUNDING_INTERVAL_NS,
    FundingChart,
    FundingRateEvidence,
    FundingRefused,
    MarkPriceEvidence,
    book_funding_settlement,
    read_funding_settlements,
)
from sourdough.writer_lease import hold_company_writer


class PaperFundingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.root / "company.sqlite"
        self.leases = self.root / "leases"
        self.company = found(
            self.store, FoundingTerms("funding-co", "charter-1", "paper", "profile")
        ).company_id
        self.chart = FundingChart(
            revision="funding-chart-v1",
            currency="USDT",
            units_per_whole=100_000_000,
            funding_expense_account="funding-expense",
            funding_income_account="funding-income",
            paper_cash_account="paper-cash",
        )
        # 2026-11-22 08:00:00 UTC in nanoseconds (exact 8h boundary)
        self.interval_end_ns = 62_310 * FUNDING_INTERVAL_NS

    def _book(self, **kwargs):
        with hold_company_writer(
            self.store, self.company, lease_directory=self.leases
        ) as writer:
            return book_funding_settlement(writer, **kwargs)

    def test_books_long_expense_and_short_income_atomically_into_gl(self):
        funding = FundingRateEvidence(
            instrument_id="BTCUSDT-PERP",
            interval_end_ns=self.interval_end_ns,
            funding_rate_e9=100_000,  # +0.0001 (1 bps)
            available_at_ns=self.interval_end_ns - 1_000,
            source_revision="binance-funding-1",
        )
        mark = MarkPriceEvidence(
            instrument_id="BTCUSDT-PERP",
            observed_ns=self.interval_end_ns - 500,
            mark_price_atoms=50_000_000_000,  # 500 USDT per lot
            quote_units_per_whole=100_000_000,
            source_revision="binance-mark-1",
        )
        # Fault injection mid-tx rolls back BOTH journal_entry and paper_funding_settlement.
        with (
            patch(
                "sourdough.paper_funding.append_audit_event",
                side_effect=OSError("cut before commit"),
            ),
            self.assertRaisesRegex(OSError, "before commit"),
        ):
            self._book(
                destination_id="perp-venue",
                account_id="acct-1",
                instrument_id="BTCUSDT-PERP",
                interval_end_ns=self.interval_end_ns,
                signed_position_lots=2,
                funding=funding,
                mark=mark,
                chart=self.chart,
            )
        self.assertEqual(read_funding_settlements(self.store, self.company), ())
        with closing(sqlite3.connect(self.store)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM journal_entry").fetchone(), (0,)
            )

        # Long 2 lots @ 50_000_000_000 * 100_000 / 1e9 = -10_000_000 atoms (pays funding).
        settled = self._book(
            destination_id="perp-venue",
            account_id="acct-1",
            instrument_id="BTCUSDT-PERP",
            interval_end_ns=self.interval_end_ns,
            signed_position_lots=2,
            funding=funding,
            mark=mark,
            chart=self.chart,
        )
        self.assertEqual(settled.status, "booked")
        self.assertEqual(settled.funding_cashflow_atoms, -10_000_000)
        self.assertIsNotNone(settled.journal_id)

        # Exact idempotent replay returns identical receipt; conflicting rate is refused.
        self.assertEqual(
            self._book(
                destination_id="perp-venue",
                account_id="acct-1",
                instrument_id="BTCUSDT-PERP",
                interval_end_ns=self.interval_end_ns,
                signed_position_lots=2,
                funding=funding,
                mark=mark,
                chart=self.chart,
            ),
            settled,
        )
        with self.assertRaisesRegex(FundingRefused, "conflicts"):
            self._book(
                destination_id="perp-venue",
                account_id="acct-1",
                instrument_id="BTCUSDT-PERP",
                interval_end_ns=self.interval_end_ns,
                signed_position_lots=2,
                funding=replace(funding, funding_rate_e9=200_000),
                mark=mark,
                chart=self.chart,
            )

        # Next 8h interval: short 3 lots receives +15_000_000 atoms.
        next_end = self.interval_end_ns + FUNDING_INTERVAL_NS
        received = self._book(
            destination_id="perp-venue",
            account_id="acct-1",
            instrument_id="BTCUSDT-PERP",
            interval_end_ns=next_end,
            signed_position_lots=-3,
            funding=replace(
                funding, interval_end_ns=next_end, available_at_ns=next_end
            ),
            mark=replace(mark, observed_ns=next_end),
            chart=self.chart,
        )
        self.assertEqual(received.status, "booked")
        self.assertEqual(received.funding_cashflow_atoms, 15_000_000)
        with closing(sqlite3.connect(self.store)) as db:
            # All GL entries strictly balance to zero.
            self.assertEqual(
                db.execute("SELECT sum(amount_minor) FROM journal_line").fetchone(),
                (0,),
            )

    def test_missing_stale_or_lookahead_evidence_records_unavailable_never_zero(self):
        # Missing funding rate -> status='unavailable', cashflow=None, 0 journal rows.
        missing_rate = self._book(
            destination_id="perp-venue",
            account_id="acct-1",
            instrument_id="BTCUSDT-PERP",
            interval_end_ns=self.interval_end_ns,
            signed_position_lots=2,
            funding=None,
            mark=MarkPriceEvidence(
                "BTCUSDT-PERP",
                self.interval_end_ns,
                50_000_000_000,
                100_000_000,
                "mark-1",
            ),
            chart=self.chart,
        )
        self.assertEqual(missing_rate.status, "unavailable")
        self.assertEqual(missing_rate.unavailable_reason, "missing-funding-rate")
        self.assertIsNone(missing_rate.funding_cashflow_atoms)
        self.assertIsNone(missing_rate.journal_id)

        # Lookahead funding rate (available_at_ns > interval_end_ns) -> unavailable with NULL stored figures.
        next_end = self.interval_end_ns + FUNDING_INTERVAL_NS
        lookahead = self._book(
            destination_id="perp-venue",
            account_id="acct-1",
            instrument_id="BTCUSDT-PERP",
            interval_end_ns=next_end,
            signed_position_lots=1,
            funding=FundingRateEvidence(
                "BTCUSDT-PERP", next_end, 100_000, next_end + 1, "rate-future"
            ),
            mark=MarkPriceEvidence(
                "BTCUSDT-PERP", next_end, 50_000_000_000, 100_000_000, "mark-2"
            ),
            chart=self.chart,
        )
        self.assertEqual(lookahead.status, "unavailable")
        self.assertEqual(lookahead.unavailable_reason, "lookahead-funding-rate")
        self.assertIsNone(lookahead.funding_cashflow_atoms)
        self.assertIsNone(lookahead.funding_rate_e9)
        self.assertIsNone(lookahead.mark_price_atoms)

        # Stale mark price (> 15 min old) -> unavailable.
        stale_end = next_end + FUNDING_INTERVAL_NS
        stale = self._book(
            destination_id="perp-venue",
            account_id="acct-1",
            instrument_id="BTCUSDT-PERP",
            interval_end_ns=stale_end,
            signed_position_lots=1,
            funding=FundingRateEvidence(
                "BTCUSDT-PERP", stale_end, 100_000, stale_end, "rate-stale"
            ),
            mark=MarkPriceEvidence(
                "BTCUSDT-PERP",
                stale_end - 20 * 60 * 10**9,
                50_000_000_000,
                100_000_000,
                "mark-stale",
            ),
            chart=self.chart,
        )
        self.assertEqual(stale.status, "unavailable")
        self.assertEqual(stale.unavailable_reason, "stale-mark-price")

        # Mark units mismatch is refused even when funding is None.
        with self.assertRaisesRegex(FundingRefused, "chart units"):
            self._book(
                destination_id="perp-venue",
                account_id="acct-1",
                instrument_id="BTCUSDT-PERP",
                interval_end_ns=stale_end + FUNDING_INTERVAL_NS,
                signed_position_lots=1,
                funding=None,
                mark=MarkPriceEvidence(
                    "BTCUSDT-PERP", stale_end, 50_000_000_000, 100, "mark-wrong-unit"
                ),
                chart=self.chart,
            )

        # Explicit flat position with valid funding+mark -> distinct status='zero', cashflow=0.
        third_end = stale_end + FUNDING_INTERVAL_NS
        explicit_zero = self._book(
            destination_id="perp-venue",
            account_id="acct-1",
            instrument_id="BTCUSDT-PERP",
            interval_end_ns=third_end,
            signed_position_lots=0,
            funding=FundingRateEvidence(
                "BTCUSDT-PERP", third_end, 100_000, third_end, "rate-3"
            ),
            mark=MarkPriceEvidence(
                "BTCUSDT-PERP", third_end, 50_000_000_000, 100_000_000, "mark-3"
            ),
            chart=self.chart,
        )
        self.assertEqual(explicit_zero.status, "zero")
        self.assertEqual(explicit_zero.funding_cashflow_atoms, 0)
        self.assertIsNone(explicit_zero.journal_id)

        # Non-8h boundary and >= 2^63 timestamp refused cleanly (no OverflowError).
        for bad_ts in (self.interval_end_ns + 1, 320_256 * FUNDING_INTERVAL_NS):
            with self.assertRaises(FundingRefused):
                self._book(
                    destination_id="perp-venue",
                    account_id="acct-1",
                    instrument_id="BTCUSDT-PERP",
                    interval_end_ns=bad_ts,
                    signed_position_lots=1,
                    funding=None,
                    mark=None,
                    chart=self.chart,
                )
