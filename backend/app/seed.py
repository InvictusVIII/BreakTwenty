import asyncio
from datetime import datetime, timezone

from sqlalchemy import delete

from app.auth import DEFAULT_DEV_USER_ID
from app.database import async_session
from app.models import (
    Account,
    BalanceHistory,
    Holding,
    Institution,
    Transaction,
)
from app.services.user_utils import ensure_default_dev_user


async def seed(user_id: int = DEFAULT_DEV_USER_ID):
    async with async_session() as db:
        await ensure_default_dev_user(db)

        await db.execute(delete(Transaction).where(Transaction.user_id == user_id))
        await db.execute(delete(Holding).where(Holding.user_id == user_id))
        await db.execute(delete(BalanceHistory).where(BalanceHistory.user_id == user_id))
        await db.execute(delete(Account).where(Account.user_id == user_id))
        await db.execute(delete(Institution).where(Institution.user_id == user_id))
        await db.flush()

        # Institutions
        ibkr = Institution(user_id=user_id, name="Interactive Brokers", type="api", provider="ibkr")
        questrade = Institution(user_id=user_id, name="Questrade", type="api", provider="questrade")
        scotia = Institution(user_id=user_id, name="Scotiabank", type="scraper", provider="scotiabank")
        td = Institution(user_id=user_id, name="TD", type="scraper", provider="td")
        amex = Institution(user_id=user_id, name="American Express", type="scraper", provider="amex")

        db.add_all([ibkr, questrade, scotia, td, amex])
        await db.flush()

        # IBKR accounts
        ibkr_margin = Account(
            user_id=user_id,
            institution_id=ibkr.id,
            name="Margin Account",
            account_type="margin",
            currency="USD",
        )
        ibkr_tfsa = Account(
            user_id=user_id,
            institution_id=ibkr.id,
            name="TFSA",
            account_type="tfsa",
            currency="CAD",
        )

        # Questrade accounts
        qt_rrsp = Account(
            user_id=user_id,
            institution_id=questrade.id,
            name="RRSP",
            account_type="rrsp",
            currency="CAD",
        )
        qt_fhsa = Account(
            user_id=user_id,
            institution_id=questrade.id,
            name="FHSA",
            account_type="fhsa",
            currency="CAD",
        )

        # Bank accounts
        scotia_chq = Account(
            user_id=user_id,
            institution_id=scotia.id,
            name="Chequing",
            account_type="chequing",
            currency="CAD",
        )
        scotia_cc = Account(
            user_id=user_id,
            institution_id=scotia.id,
            name="Visa Infinite",
            account_type="credit_card",
            currency="CAD",
            is_liability=True,
        )
        scotia_loc = Account(
            user_id=user_id,
            institution_id=scotia.id,
            name="Line of Credit",
            account_type="loc",
            currency="CAD",
            is_liability=True,
        )
        td_savings = Account(
            user_id=user_id,
            institution_id=td.id,
            name="High Interest Savings",
            account_type="savings",
            currency="CAD",
        )
        amex_cc = Account(
            user_id=user_id,
            institution_id=amex.id,
            name="Cobalt Card",
            account_type="credit_card",
            currency="CAD",
            is_liability=True,
        )

        all_accounts = [
            ibkr_margin,
            ibkr_tfsa,
            qt_rrsp,
            qt_fhsa,
            scotia_chq,
            scotia_cc,
            scotia_loc,
            td_savings,
            amex_cc,
        ]
        db.add_all(all_accounts)
        await db.flush()

        # Holdings for IBKR Margin
        ibkr_holdings = [
            Holding(user_id=user_id, account_id=ibkr_margin.id, symbol="NVDA", name="NVIDIA Corp", quantity=50, market_value=6750.00, average_cost=4800.00, currency="USD"),
            Holding(user_id=user_id, account_id=ibkr_margin.id, symbol="AAPL", name="Apple Inc", quantity=100, market_value=17200.00, average_cost=14500.00, currency="USD"),
            Holding(user_id=user_id, account_id=ibkr_margin.id, symbol="MSFT", name="Microsoft Corp", quantity=40, market_value=16800.00, average_cost=13200.00, currency="USD"),
            Holding(user_id=user_id, account_id=ibkr_margin.id, symbol="VOO", name="Vanguard S&P 500 ETF", quantity=30, market_value=14850.00, average_cost=12600.00, currency="USD"),
        ]

        # Holdings for IBKR TFSA
        tfsa_holdings = [
            Holding(user_id=user_id, account_id=ibkr_tfsa.id, symbol="XEQT", name="iShares Core Equity ETF", quantity=200, market_value=6200.00, average_cost=5400.00, currency="CAD"),
            Holding(user_id=user_id, account_id=ibkr_tfsa.id, symbol="VFV", name="Vanguard S&P 500 Index ETF", quantity=80, market_value=8960.00, average_cost=7200.00, currency="CAD"),
        ]

        # Holdings for Questrade RRSP
        rrsp_holdings = [
            Holding(user_id=user_id, account_id=qt_rrsp.id, symbol="VCN", name="Vanguard FTSE Canada All Cap", quantity=150, market_value=6450.00, average_cost=5850.00, currency="CAD"),
            Holding(user_id=user_id, account_id=qt_rrsp.id, symbol="XAW", name="iShares Core MSCI All World ex Canada", quantity=120, market_value=4680.00, average_cost=4200.00, currency="CAD"),
        ]

        # Holdings for FHSA
        fhsa_holdings = [
            Holding(user_id=user_id, account_id=qt_fhsa.id, symbol="XEQT", name="iShares Core Equity ETF", quantity=100, market_value=3100.00, average_cost=2800.00, currency="CAD"),
        ]

        db.add_all(ibkr_holdings + tfsa_holdings + rrsp_holdings + fhsa_holdings)

        # Current balances
        now = datetime.now(timezone.utc).date()
        balances = [
            BalanceHistory(user_id=user_id, account_id=ibkr_margin.id, balance=55600.00, date=now),
            BalanceHistory(user_id=user_id, account_id=ibkr_tfsa.id, balance=15160.00, date=now),
            BalanceHistory(user_id=user_id, account_id=qt_rrsp.id, balance=11130.00, date=now),
            BalanceHistory(user_id=user_id, account_id=qt_fhsa.id, balance=3100.00, date=now),
            BalanceHistory(user_id=user_id, account_id=scotia_chq.id, balance=4200.00, date=now),
            BalanceHistory(user_id=user_id, account_id=scotia_cc.id, balance=2800.00, date=now),
            BalanceHistory(user_id=user_id, account_id=scotia_loc.id, balance=5000.00, date=now),
            BalanceHistory(user_id=user_id, account_id=td_savings.id, balance=12000.00, date=now),
            BalanceHistory(user_id=user_id, account_id=amex_cc.id, balance=1400.00, date=now),
        ]
        db.add_all(balances)

        await db.commit()
        print(f"Seed data created successfully for user {user_id}!")


if __name__ == "__main__":
    asyncio.run(seed())
