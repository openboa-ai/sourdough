import unittest
from dataclasses import replace
from fractions import Fraction

from sourdough.fund_returns import (
    ExternalFlow,
    ReturnRefused,
    ReturnScope,
    link,
    measure,
)


TRADING = ReturnScope("fund", "all-accounts", "USD", "trading_net", "policy-1")
COMPANY = replace(TRADING, series="company_net")


class FundReturns(unittest.TestCase):
    def test_midperiod_contribution_is_not_profit_and_subperiods_link_exactly(self):
        contribution = ExternalFlow("capital-1", 5, 0, 100, 1100, 1200)
        result = measure(
            TRADING, 0, 10, 1000, 1140, (contribution,), sources=("values-1",)
        )
        self.assertEqual(
            (
                result.gross_in_minor,
                result.gross_out_minor,
                result.net_flow_minor,
                result.pnl_minor,
            ),
            (100, 0, 100, 40),
        )
        self.assertEqual(result.time_weighted_return, Fraction(45, 1000))
        next_result = measure(TRADING, 10, 20, 1140, 1254, (), sources=("values-2",))
        self.assertEqual(link((result, next_result)), Fraction(299, 2000))
        self.assertIsNone(link((result, replace(next_result, start_ns=11))))
        self.assertIsNone(link((result, replace(next_result, scope=COMPANY))))
        self.assertIsNone(link((result, replace(next_result, opening_minor=1139))))

    def test_trading_and_company_keep_exact_zero_loss_and_missing_separate(self):
        contribution = ExternalFlow("opening-capital", 0, 1, 100, 1000, 1100)
        trading = measure(
            TRADING, 0, 10, 1000, 1108, (contribution,), sources=("venue",)
        )
        company = measure(
            COMPANY, 0, 10, 1000, 1105, (contribution,), sources=("venue", "charges")
        )
        unavailable = measure(
            COMPANY,
            0,
            10,
            1000,
            1108,
            (contribution,),
            sources=("venue",),
            missing=("compute-receipt",),
        )
        self.assertEqual(
            (trading.pnl_minor, trading.time_weighted_return), (8, Fraction(8, 1100))
        )
        self.assertEqual(
            (company.pnl_minor, company.time_weighted_return), (5, Fraction(5, 1100))
        )
        self.assertEqual(
            (unavailable.pnl_minor, unavailable.time_weighted_return), (None, None)
        )
        self.assertEqual(unavailable.missing, ("compute-receipt",))
        flat = measure(COMPANY, 10, 20, 1105, 1105, (), sources=("close",))
        loss = measure(COMPANY, 10, 20, 1105, 1100, (), sources=("close",))
        self.assertEqual((flat.pnl_minor, flat.time_weighted_return), (0, Fraction(0)))
        self.assertEqual(
            (loss.pnl_minor, loss.time_weighted_return), (-5, Fraction(-5, 1105))
        )

    def test_missing_middle_mark_preserves_amounts_but_breaks_the_chain(self):
        delayed = ExternalFlow("capital", 5, 0, 100, None, 1200)
        incomplete = measure(TRADING, 0, 10, 1000, 1140, (delayed,), sources=("cut",))
        self.assertEqual((incomplete.pnl_minor, incomplete.net_flow_minor), (40, 100))
        self.assertIsNone(incomplete.time_weighted_return)
        self.assertEqual(incomplete.missing, ("flow-mark:capital",))
        after = measure(TRADING, 10, 20, 1140, 1140, (), sources=("next",))
        self.assertIsNone(link((incomplete, after)))
        corrected = measure(
            TRADING,
            0,
            10,
            1000,
            1140,
            (replace(delayed, before_minor=1100),),
            sources=("corrected-cut",),
        )
        self.assertEqual(link((corrected, after)), Fraction(45, 1000))
        zero_base = measure(TRADING, 0, 10, 0, 10, (), sources=("zero-capital",))
        self.assertEqual(zero_base.pnl_minor, 10)
        self.assertIsNone(zero_base.time_weighted_return)

    def test_refuses_inconsistent_flow_pairs_replays_and_event_order(self):
        first = ExternalFlow("withdrawal", 5, 0, -100, 1000, 900)
        with self.assertRaisesRegex(ReturnRefused, "do not reconcile"):
            replace(first, before_minor=999)
        with self.assertRaisesRegex(ReturnRefused, "Duplicate"):
            measure(TRADING, 0, 10, 1000, 900, (first, first), sources=("close",))
        conflicting_sequence = replace(first, source_id="contribution")
        with self.assertRaisesRegex(ReturnRefused, "sequenced"):
            measure(
                TRADING,
                0,
                10,
                1000,
                900,
                (first, conflicting_sequence),
                sources=("close",),
            )
