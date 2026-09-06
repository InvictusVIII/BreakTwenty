"""Connector-less net-worth "group" institutions for manual assets and debts.

BreakTwenty lets users add tangible assets (real estate, vehicles, valuables, private
investments, other assets) and manual debts to net worth. Each category is a
per-user, connector-less ``Institution`` container — exactly like the Cash holder
in ``services.manual_accounts`` — that the sync loop never touches (its provider is
a sentinel that carries no connector/route in the provider catalog, so sync routing
skips it). Individual assets/debts are ``Account`` rows inside the bucket, so they
reuse the whole accounts surface (net worth, balance history, allocation, change
tracking, FX) for free, and render as ordinary expandable institution cards on the
Accounts page (Real Estate -> its houses, just like a bank -> its accounts).

Two value-over-time doors, chosen by the bucket's ``value_door``:

* ``revaluation`` (assets) — value changes by re-appraisal, not money movement, so
  it is recorded as dated ``balance_history`` points. The initial value is seeded
  via ``Account.opening_balance`` + ``recompute_manual_account_balance`` (the
  tangible-asset case); later valuations are appended as balance points. Assets do
  NOT take transactions (a +$100k "transaction" would corrupt cash-flow/income/
  recurring/categorization, which all assume real money movement).
* ``flow`` (debt) — a balance that moves via transactions (e.g. a loan paid down),
  so it uses the manual-transaction path and the opening-balance ± cumulative
  derivation in ``services.manual_institutions``.

Mirrors the Cash holder's lifecycle: idempotent provisioning and prune-when-empty.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Institution
from app.services.manual_institutions import set_account_value_as_of

# Connector-less group institution type (shared with the Cash holder and user
# manual institutions); never a connector-catalog provider, so sync never runs.
GROUP_INSTITUTION_TYPE = "manual"

# Each category bucket. ``provider`` is the institution sentinel (also registered in
# the provider catalog with no connector/route so sync routing skips it); ``kind``
# drives ``is_liability``; ``value_door`` selects how value changes over time.
# ``provider`` equals the category key today (and the AddToNetWorthModal tile key),
# but callers should resolve via ASSET_GROUPS rather than assume the identity.
ASSET_GROUPS: dict[str, dict] = {
    "real_estate": {
        "provider": "real_estate",
        "name": "Real Estate",
        "kind": "asset",
        "value_door": "revaluation",
        "default_account_type": "real_estate",
    },
    "vehicles": {
        "provider": "vehicles",
        "name": "Vehicles",
        "kind": "asset",
        "value_door": "revaluation",
        "default_account_type": "car",
    },
    "valuables": {
        "provider": "valuables",
        "name": "Valuables",
        "kind": "asset",
        "value_door": "revaluation",
        "default_account_type": "art",
    },
    "private_investments": {
        "provider": "private_investments",
        "name": "Private Investments",
        "kind": "asset",
        "value_door": "revaluation",
        "default_account_type": "private_equity",
    },
    "other_assets": {
        "provider": "other_assets",
        "name": "Other Assets",
        "kind": "asset",
        "value_door": "revaluation",
        "default_account_type": "other_asset",
    },
    "debt": {
        "provider": "debt",
        "name": "Debt",
        "kind": "liability",
        "value_door": "flow",
        "default_account_type": "personal_loan",
    },
}

# Sentinel providers for the connector-less group institutions (membership tests).
ASSET_GROUP_PROVIDERS = {spec["provider"] for spec in ASSET_GROUPS.values()}


def is_asset_group_provider(provider: str | None) -> bool:
    """True if ``provider`` is one of the connector-less group sentinels."""
    return (provider or "") in ASSET_GROUP_PROVIDERS


async def ensure_asset_group_institution(
    db: AsyncSession, user_id: int, category: str
) -> Institution:
    """Idempotently return the user's connector-less bucket institution for
    ``category`` (e.g. Real Estate). Kept ``enabled=True``/``hidden=False`` so its
    accounts count toward net worth; the sentinel provider keeps the sync loop from
    ever treating it as a connector. Mirrors ``manual_accounts.ensure_manual_institution``.
    Caller commits."""
    spec = ASSET_GROUPS.get(category)
    if spec is None:
        raise ValueError(f"Unknown asset group category: {category!r}")
    provider = spec["provider"]
    institution = (
        await db.execute(
            select(Institution).where(
                Institution.user_id == user_id,
                Institution.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if institution is None:
        institution = Institution(
            user_id=user_id,
            name=spec["name"],
            type=GROUP_INSTITUTION_TYPE,
            provider=provider,
            enabled=True,
            hidden=False,
        )
        db.add(institution)
        await db.flush()
    elif institution.name != spec["name"]:
        # Idempotently migrate a previously-seeded display name.
        institution.name = spec["name"]
    return institution


async def create_group_account(
    db: AsyncSession,
    user_id: int,
    category: str,
    *,
    name: str,
    account_type: str | None = None,
    currency: str = "CAD",
    value: float | None = None,
    as_of: datetime | None = None,
    purchase_value: float | None = None,
    purchase_as_of: datetime | None = None,
) -> Account:
    """Create a named account inside the category bucket (provisioning the bucket
    institution on first use). ``is_liability`` follows the bucket ``kind``. The
    initial ``value`` (current worth for an asset, amount owed for a debt) is written
    through the revaluation door as a dated ``balance_history`` point — no
    ``opening_balance`` anchor and no transactions, so the account's worth is purely
    its user-maintained value timeline. An optional ``purchase_value`` at
    ``purchase_as_of`` (both required together) seeds an earlier point on that same
    timeline, so net-worth/appreciation reads from the purchase date instead of today.
    Caller commits.

    (Emptied buckets are pruned by ``delete_account_and_maybe_institution`` when their
    last account is removed, so no explicit prune is needed here.)"""
    spec = ASSET_GROUPS.get(category)
    if spec is None:
        raise ValueError(f"Unknown asset group category: {category!r}")
    name = (name or "").strip()
    if not name:
        raise ValueError("Account name is required")
    institution = await ensure_asset_group_institution(db, user_id, category)
    account = Account(
        user_id=user_id,
        institution_id=institution.id,
        external_id=None,
        name=name,
        account_type=(account_type or spec["default_account_type"]),
        currency=(currency or "CAD").upper(),
        is_liability=(spec["kind"] == "liability"),
        hidden=False,
    )
    db.add(account)
    await db.flush()
    # Earlier purchase point first (chronology is irrelevant — each upserts its own
    # local day — but reads naturally as "bought at X, worth Y today").
    if purchase_value is not None and purchase_as_of is not None:
        account.purchase_value = purchase_value
        account.purchase_date = purchase_as_of
        await set_account_value_as_of(db, user_id, account, purchase_value, as_of=purchase_as_of)
    if value is not None:
        await set_account_value_as_of(db, user_id, account, value, as_of=as_of)
    return account
