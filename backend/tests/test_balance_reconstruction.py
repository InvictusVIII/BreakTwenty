import unittest

from app.services.balance_reconstruction import reconstruct_daily_balances


class BalanceReconstructionTests(unittest.TestCase):
    def test_liability_reconstruction_keeps_debt_magnitudes_non_negative(self):
        series = reconstruct_daily_balances(
            [("2026-05-24", 100)],
            [("2026-05-23", 0), ("2026-05-24", -125)],
            is_liability=True,
        )

        self.assertEqual(25, series["2026-05-23"])
        self.assertEqual(100, series["2026-05-24"])

    def test_asset_reconstruction_preserves_signed_values(self):
        series = reconstruct_daily_balances(
            [("2026-05-24", 100)],
            [("2026-05-23", 0), ("2026-05-24", -125)],
            is_liability=False,
        )

        self.assertEqual(225, series["2026-05-23"])
