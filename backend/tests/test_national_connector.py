from __future__ import annotations

import unittest

from app.connectors.national import NationalBankConnector
from app.connectors.types import SyncStatus, account_result_key


class NationalBankConnectorTests(unittest.TestCase):
    def test_provider_credit_type_is_not_appended_to_description(self) -> None:
        result = NationalBankConnector()._build_from_payload(
            {
                "status": "ok",
                "accounts": [
                    {
                        "external_id": "account:69f4c100e3a0d12acc0805c9",
                        "key": "69f4c100e3a0d12acc0805c9",
                        "productName": {"en": "Savings Account", "fr": "Compte epargne"},
                        "type": "SAVINGS",
                        "balance": "100.00",
                        "currency": "CAD",
                    }
                ],
                "transactions": {
                    "account:69f4c100e3a0d12acc0805c9": [
                        {
                            "guid": "TRN-f342a6bc-07a7-4e07-8ef8-f0e871b269b8",
                            "createdDate": "2026-05-06",
                            "effectiveDate": "2026-05-06",
                            "description": {"en": "Tangerine", "fr": "Tangerine"},
                            "descriptionOrig": {
                                "en": "INVESTMENT TANGERINE",
                                "fr": "PLACEMENT TANGERINE",
                            },
                            "realAmount": 1,
                            "status": "POSTED",
                            "type": "CREDIT",
                            "_account_currency": "CAD",
                        }
                    ]
                },
                "transaction_fetch_succeeded_accounts": ["account:69f4c100e3a0d12acc0805c9"],
            }
        )

        self.assertEqual(SyncStatus.OK, result.status)
        self.assertEqual(1, len(result.transactions))
        tx = result.transactions[0]
        self.assertEqual("deposit", tx.type)
        self.assertEqual(1.0, tx.amount)
        self.assertEqual("CAD", tx.currency)
        self.assertEqual("Tangerine", tx.description)

        account_key = account_result_key(result.accounts[0])
        self.assertEqual([tx], result.transactions_by_account[account_key])
        self.assertEqual({account_key}, result.transaction_fetch_succeeded_accounts)


if __name__ == "__main__":
    unittest.main()
