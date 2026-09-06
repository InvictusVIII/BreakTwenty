from __future__ import annotations

import unittest

from app.connectors.cibc import CIBCConnector
from app.connectors.types import SyncStatus, account_result_key


class CIBCConnectorTests(unittest.TestCase):
    def test_deposit_credit_column_shape_keeps_amount_and_compacts_description(self) -> None:
        result = CIBCConnector()._build_from_payload(
            {
                "status": "ok",
                "accounts": [
                    {
                        "external_id": "account:savings-1",
                        "name": "Savings",
                        "categorization": {"category": "DEPOSIT", "subCategory": "SAVINGS"},
                        "balance": "100.00",
                        "currency": "CAD",
                    }
                ],
                "transactions": {
                    "account:savings-1": [
                        {
                            "transactionId": "0",
                            "date": "2026-05-06",
                            "description": "DEPOSIT Tangerine",
                            "descriptionLine1": "DEPOSIT",
                            "descriptionLine2": "Tangerine",
                            "debit": "",
                            "credit": "$12.34",
                            "creditSortValue": "12.34",
                            "_account_currency": "CAD",
                        }
                    ]
                },
                "transaction_fetch_succeeded_accounts": ["account:savings-1"],
            }
        )

        self.assertEqual(SyncStatus.OK, result.status)
        self.assertEqual(1, len(result.transactions))
        tx = result.transactions[0]
        self.assertEqual("deposit", tx.type)
        self.assertEqual(12.34, tx.amount)
        self.assertEqual("CAD", tx.currency)
        self.assertEqual("DEPOSIT Tangerine", tx.description)

        account_key = account_result_key(result.accounts[0])
        self.assertEqual([tx], result.transactions_by_account[account_key])
        self.assertEqual({account_key}, result.transaction_fetch_succeeded_accounts)

    def test_withdrawal_debit_column_shape_is_negative(self) -> None:
        result = CIBCConnector()._build_from_payload(
            {
                "status": "ok",
                "accounts": [
                    {
                        "external_id": "account:savings-1",
                        "name": "Savings",
                        "categorization": {"category": "DEPOSIT", "subCategory": "SAVINGS"},
                        "balance": "100.00",
                    }
                ],
                "transactions": {
                    "account:savings-1": [
                        {
                            "transactionId": "1",
                            "date": "2026-05-06",
                            "description": "DEBIT PURCHASE",
                            "debit": "",
                            "debitSortValue": "5.67",
                            "credit": "",
                            "_account_currency": "CAD",
                        }
                    ]
                },
                "transaction_fetch_succeeded_accounts": ["account:savings-1"],
            }
        )

        tx = result.transactions[0]
        self.assertEqual("withdrawal", tx.type)
        self.assertEqual(-5.67, tx.amount)

    def test_transaction_location_prefix_is_kept_when_present(self) -> None:
        result = CIBCConnector()._build_from_payload(
            {
                "status": "ok",
                "accounts": [
                    {
                        "external_id": "account:savings-1",
                        "name": "Savings",
                        "categorization": {"category": "DEPOSIT", "subCategory": "SAVINGS"},
                        "balance": "100.00",
                    }
                ],
                "transactions": {
                    "account:savings-1": [
                        {
                            "transactionId": "2",
                            "date": "2026-05-06",
                            "transactionLocation": "Electronic Funds Transfer",
                            "description": "DEPOSIT Tangerine",
                            "descriptionLine1": "DEPOSIT",
                            "descriptionLine2": "Tangerine",
                            "creditSortValue": "12.34",
                            "_account_currency": "CAD",
                        }
                    ]
                },
                "transaction_fetch_succeeded_accounts": ["account:savings-1"],
            }
        )

        tx = result.transactions[0]
        self.assertEqual("Electronic Funds Transfer DEPOSIT Tangerine", tx.description)

    def test_compact_transaction_location_code_is_not_displayed(self) -> None:
        result = CIBCConnector()._build_from_payload(
            {
                "status": "ok",
                "accounts": [
                    {
                        "external_id": "account:savings-1",
                        "name": "Savings",
                        "categorization": {"category": "DEPOSIT", "subCategory": "SAVINGS"},
                        "balance": "100.00",
                    }
                ],
                "transactions": {
                    "account:savings-1": [
                        {
                            "transactionId": "3",
                            "date": "2026-05-06",
                            "transactionLocation": "SG007",
                            "transactionType": "TG012",
                            "transactionDescription": "DEPOSIT Tangerine",
                            "descriptionLine1": "DEPOSIT",
                            "descriptionLine2": " Tangerine",
                            "credit": "1.0",
                            "_account_currency": "CAD",
                        }
                    ]
                },
                "transaction_fetch_succeeded_accounts": ["account:savings-1"],
            }
        )

        tx = result.transactions[0]
        self.assertEqual("DEPOSIT Tangerine", tx.description)


if __name__ == "__main__":
    unittest.main()
