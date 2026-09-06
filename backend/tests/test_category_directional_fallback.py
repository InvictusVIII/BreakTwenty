import unittest

from app.services.categories import resolve_category_for_transaction


class CategoryDirectionalFallbackTests(unittest.TestCase):
    def _resolve(self, *, transaction_type: str, account_type: str) -> int | None:
        category_id, _source, _rule_id = resolve_category_for_transaction(
            description="Unrecognized provider transaction",
            amount=-12.34 if transaction_type == "withdrawal" else 12.34,
            provider="test-provider",
            account_type=account_type,
            transaction_type=transaction_type,
            user_rules=[],
            bundled_rules=(),
            category_seed_key_to_id={"uncategorized": 99},
            type_to_category_id={"deposit": 10, "withdrawal": 11},
        )
        return category_id

    def test_bank_directional_types_fall_through_to_uncategorized(self) -> None:
        self.assertEqual(
            99,
            self._resolve(transaction_type="withdrawal", account_type="credit_card"),
        )
        self.assertEqual(
            99,
            self._resolve(transaction_type="deposit", account_type="chequing"),
        )
        self.assertEqual(
            99,
            self._resolve(transaction_type="withdrawal", account_type="auto_loan"),
        )

    def test_investment_directional_types_keep_cash_movement_fallback(self) -> None:
        self.assertEqual(
            11,
            self._resolve(transaction_type="withdrawal", account_type="margin"),
        )
        self.assertEqual(
            10,
            self._resolve(transaction_type="deposit", account_type="crypto"),
        )
