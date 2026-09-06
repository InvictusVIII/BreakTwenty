"""One-off backfill: populate Transaction.quantity for existing dividend rows.

Derives the share count from the transaction description using
`derive_dividend_quantity_from_description`, which understands the IBKR Flex
("<CCY> <RATE> PER SHARE") and Questrade ("ON <N> SHS REC ...") description
formats. Records without a parseable share count or rate are skipped.

Run from the host:
  docker exec -w /app breaktwenty-backend-1 python3 scripts/backfill_ibkr_dividend_quantity.py
"""

import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402

from app.database import async_session  # noqa: E402
from app.models import Transaction  # noqa: E402
from app.services.flex_query import derive_dividend_quantity_from_description  # noqa: E402


async def main() -> int:
    updated = 0
    skipped = 0
    already_set = 0

    async with async_session() as db:
        rows = (
            await db.execute(
                select(Transaction).where(Transaction.type == "dividend")
            )
        ).scalars().all()

        for tx in rows:
            if tx.quantity is not None:
                already_set += 1
                continue
            quantity = derive_dividend_quantity_from_description(tx.description, tx.amount)
            if quantity is None:
                skipped += 1
                continue
            tx.quantity = quantity
            updated += 1

        await db.commit()

    print(f"Updated: {updated}")
    print(f"Skipped (no per-share data in description): {skipped}")
    print(f"Already populated: {already_set}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
