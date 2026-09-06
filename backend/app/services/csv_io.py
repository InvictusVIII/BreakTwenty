"""Canonical CSV schema contract for BreakTwenty import/export.

This module is the source of truth for export column layout and the documented
subset recognized by manual-account CSV import. Backup-only and export-only
columns preserve readable ownership context but are intentionally not a full
application restore contract.

Scope of this module: PURE, side-effect-free declarations and helpers only.
It performs no DB access, no FX conversion, no HTTP, and no categorization. The
export endpoints and the import service consume these definitions and apply the
stateful pieces (account resolution, FX backfill, content-dedup probing,
sign coercion via ``services.categories.canonical_amount_for_classification``).
This keeps the contract reusable from background jobs and multi-user paths.

Datasets
--------
- ``transactions``      import + export, full ledger; exact dedup via external_id.
- ``balances``          import + export; past-dated rows backfill the net worth graph.
- ``holdings``          export only; current-state position snapshot.
- ``networth_history``  export only; derived from balance history, not importable.

Cross-cutting conventions
-------------------------
- Headers are stable ``lower_snake_case`` names so export -> edit in a
  spreadsheet -> re-import needs ZERO column remapping.
- Dates are the user's LOCAL calendar day (``YYYY-MM-DD``). The graph and every
  read/upsert path key on the user-local day, so export must convert the stored
  UTC datetime to the user's timezone before emitting, and import anchors a
  date-only value to LOCAL NOON -> UTC (mirroring the manual-row noon trick) so a
  re-import lands on the same local day instead of shifting across midnight.
- Money/quantity are plain decimals: ``.`` decimal separator, no thousands
  separators, no currency symbols, leading ``-`` for negatives. Underlying
  storage is float, so exact byte round-trip is not guaranteed; the serializer
  rounds money to 2dp and the mint hash rounds to 2dp to stay idempotent.
- Currency is ISO 4217 (uppercase), carried natively per row. It is NOT required
  on import; precedence is row currency -> resolved account currency -> primary.
- Sign: transactions ``amount`` is cardholder-perspective (negative = outflow).
  Balances ``balance`` uses the app's display sign — liabilities owed are negative
  and liability credits are positive; assets export as stored. ``is_liability``
  records the account classification. ``networth_history`` is in the
  user's primary currency: ``total_liabilities`` is signed negative (debt; a net
  credit balance reads positive) and ``net_worth = total_assets - total_liabilities``.

Generalized import design notes (not the current restore contract):
The current importer targets an existing manual account and consumes only the
subset explicitly allowed by ``services.csv_import``. The broader rules below
document helpers retained for a possible generalized importer; they do not mean
that a BreakTwenty export can currently recreate an installation.
- Dedup is ``(user_id, external_id)`` (``uq_transactions_user_id_external_id``).
  BreakTwenty' own export carries ``external_id`` for exact idempotent round-trip. A
  FOREIGN CSV without one gets a deterministic minted id via
  :func:`mint_external_id` (normalized fields + an occurrence disambiguator for
  truly identical same-day rows). Minted ids never reuse a reserved prefix.
- external_id equality alone CANNOT catch a foreign row that duplicates an
  already-synced provider transaction (different ids, same economic event). The
  import service MUST run a content-based overlap suppression for minted
  ``csvimport_`` rows: probe the resolved account within a small date tolerance
  for a matching ``round(amount, 2)`` + currency (+ symbol for trades) and skip
  on match. Otherwise syncing a provider AND importing its CSV double-counts.
- Import is a DEDICATED code path, NOT ``upsert_transactions_for_account`` (that
  sync path overwrites ``description`` and ignores user overrides / manual
  category). On an existing-external_id match the importer preserves the
  immutable ``description`` and applies ``user_description``, ``user_notes``, a
  non-empty ``category`` (as ``category_source='manual'``), and the signed
  ``amount``. ``description`` is stored only on first insert.
- Manual category overrides always win: ``category_source`` round-trips. If the
  column is present it sets the exact source; if absent, a non-empty ``category``
  is treated as ``manual``.
- ``account_type`` is free-form/open; ``cash`` is the reserved type of the
  virtual Cash holder (provider ``manual``, account external_id ``cash:<CCY>``).
  Rows of type ``manual`` (or external_id prefix ``manual:``) must resolve to a
  cash account, never attach to a synced account.
- ``cash_mirror:`` legs are auto-derived; the importer skips them and lets the
  cash reconcile path regenerate them from the re-imported source rows.
- Liability statements present charges as positives; BreakTwenty ``amount`` is
  cardholder-perspective. Foreign liability files must supply ``debit``/``credit``
  columns or a per-file amount convention so charges store NEGATIVE.
- A complete readable ownership export includes hidden accounts and institutions
  because ordinary UI read paths filter them out. The current manual-account CSV
  importer does not restore this visibility context.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

# Single source of truth for the reserved transaction external_id namespaces and
# the cash account type. Imported (not re-declared) so this contract can never
# drift from the live cash/manual machinery in services.manual_accounts.
from app.services.manual_accounts import (
    CASH_MIRROR_PREFIX,
    CASH_OPENING_PREFIX,
    MANUAL_TX_PREFIX,
)

# --- Cross-cutting constants -------------------------------------------------

CATEGORY_PATH_SEPARATOR = " > "
MONEY_DECIMALS = 2
QUANTITY_DECIMALS = 8

# Prefix for deterministic ids minted for foreign CSV rows that carry no
# external_id. Chosen to sit OUTSIDE every reserved namespace below.
FOREIGN_IMPORT_PREFIX = "csvimport_"

# Reserved transaction external_id namespaces a minted foreign id must avoid.
RESERVED_EXTERNAL_ID_PREFIXES = (
    MANUAL_TX_PREFIX,
    CASH_OPENING_PREFIX,
    CASH_MIRROR_PREFIX,
)


# --- Schema declaration ------------------------------------------------------


@dataclass(frozen=True)
class CsvColumn:
    """One column in a dataset's canonical CSV layout."""

    header: str
    description: str
    required_on_import: bool = False
    export_only: bool = False
    import_only: bool = False
    # internal=True columns are backup-only: omitted from the lean per-page exports,
    # emitted only when an export opts in (include_internal_ids); ignored on import.
    internal: bool = False
    example: str = ""
    # Human-friendly display label emitted as the CSV header row. Empty = derive a
    # Title Case label from `header`. Import accepts either form (normalized).
    title: str = ""


@dataclass(frozen=True)
class CsvDataset:
    """A named dataset and its ordered canonical columns."""

    key: str
    purpose: str
    direction: str  # "import_export" | "export_only"
    columns: tuple[CsvColumn, ...]
    identity_columns: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


TRANSACTIONS = CsvDataset(
    key="transactions",
    purpose=(
        "Full per-row ledger (cash + investment activity) for readable data retention, "
        "spreadsheet analysis, and net-worth backfill alongside balances."
    ),
    direction="import_export",
    identity_columns=("external_id", "institution_name", "account_name", "account_external_id"),
    columns=(
        CsvColumn(
            "date",
            "Transaction date as the user-local calendar day (YYYY-MM-DD). Import "
            "anchors a date-only value to local noon -> UTC to avoid day-shift.",
            required_on_import=True,
            example="2026-01-31",
        ),
        CsvColumn(
            "institution_name",
            "Human-readable institution display name. Required per-row OR injected "
            "by a file-level account assignment. Never the numeric institution id.",
            required_on_import=True,
            example="Interactive Brokers",
        ),
        CsvColumn(
            "account_name",
            "Human-readable account display name within the institution. Required "
            "per-row OR via file-level account assignment.",
            required_on_import=True,
            example="TFSA",
            title="Account",
        ),
        CsvColumn(
            "account_type",
            "Account classification (open/extensible, see KNOWN_ACCOUNT_TYPES). "
            "'cash' is the reserved Cash-holder type. Passed through verbatim.",
            example="tfsa",
        ),
        CsvColumn(
            "description",
            "Transaction description: your override if set, else the raw provider "
            "description. Stored as the description on import.",
            example="HARVEST DIVERSIFIED HIGH INCOME ETF",
            title="Description",
        ),
        CsvColumn(
            "category",
            "Hierarchical category by display name: 'Parent > Child' (must resolve "
            "to a LEAF). Empty = let the resolver decide. A non-empty value is a "
            "manual assignment unless category_source says otherwise.",
            example="Shopping > Online Shopping",
        ),
        CsvColumn(
            "amount",
            "Signed monetary value in this row's currency, cardholder-perspective: "
            "negative = outflow (buy/fee/withdrawal), positive = inflow "
            "(sell/dividend/deposit). On liability accounts, charges are NEGATIVE.",
            required_on_import=True,
            example="-3750.00",
        ),
        CsvColumn(
            "currency",
            "ISO 4217 native currency. NOT required; precedence on import is row -> "
            "account currency -> primary. Drives per-row FX at the transaction date.",
            example="USD",
        ),
        # --- Backup-only columns (emitted only with include_internal_ids) ---
        CsvColumn(
            "external_id",
            "Authoritative source dedup key emitted in complete readable exports as "
            "ownership context. The current manual-account importer ignores this "
            "backup-only field and mints its own identity.",
            internal=True,
            example="ibkr_txn123",
        ),
        CsvColumn(
            "type",
            "Transaction type (open whitelist, see KNOWN_TRANSACTION_TYPES). Missing/"
            "unknown -> 'other'. 'manual' rows resolve to a cash account. Backup-only.",
            internal=True,
            example="buy",
        ),
        CsvColumn(
            "institution_provider",
            "Institution provider key; disambiguates same-display-name institutions. "
            "Backup-only.",
            export_only=True,
            internal=True,
            example="ibkr",
        ),
        CsvColumn(
            "account_external_id",
            "Provider account id/mask for precise account match. Backup-only.",
            internal=True,
            example="account:U123456",
        ),
        CsvColumn(
            "account_currency",
            "The resolved account's own currency (distinct from the per-row "
            "transaction currency); seeds deterministic account creation. Backup-only.",
            export_only=True,
            internal=True,
            example="USD",
        ),
        CsvColumn("symbol", "Security ticker for trade/dividend/distribution rows. Backup-only.", internal=True, example="AAPL"),
        CsvColumn("quantity", "Units/shares (unsigned; abs() normalized on import). Backup-only.", internal=True, example="100"),
        CsvColumn("price", "Unit execution price in row currency. Backup-only.", internal=True, example="37.50"),
        CsvColumn("commission", "Trade commission/fee in row currency (unsigned). Backup-only.", internal=True, example="1.00"),
        CsvColumn("mcc", "Visa/MC merchant category code; auto-categorization fallback. Backup-only.", internal=True, example="5812"),
        CsvColumn("provider_recurring", "Provider's own recurring/pre-auth flag. Backup-only.", internal=True, example="false"),
        CsvColumn("asset_category", "Provider asset class (for example FUT, STK, or OPT). Backup-only.", internal=True, example="FUT"),
        CsvColumn("realized_pnl", "Provider-reported realized profit/loss in row currency. Backup-only.", internal=True, example="125.50", title="Realized P&L"),
        CsvColumn("category_source", "auto | rule | manual; if present sets the exact source on import. Backup-only.", internal=True, example="manual"),
        CsvColumn("user_description", "Your display override (separate from the provider description). Backup-only.", internal=True, example=""),
        CsvColumn("user_notes", "Free-text notes. Backup-only.", internal=True, example=""),
        CsvColumn(
            "raw_amount",
            "Provider pre-coercion amount. Export-only diagnostic; NOT imported. Backup-only.",
            export_only=True,
            internal=True,
        ),
        # --- Import-only foreign two-column shape ---
        CsvColumn(
            "debit",
            "Optional foreign two-column shape: positive debit. Import-only; "
            "reconciled as amount = credit - debit (cardholder perspective).",
            import_only=True,
        ),
        CsvColumn(
            "credit",
            "Optional foreign two-column shape: positive credit. Import-only.",
            import_only=True,
        ),
    ),
    notes=(
        "Import is a dedicated path, not upsert_transactions_for_account.",
        "Minted csvimport_ rows require content-based overlap suppression vs synced rows.",
        "cash_mirror: rows are skipped on import and regenerated by cash reconcile.",
        "Sign for income/expense categories follows canonical_amount_for_classification; "
        "buy<0 / sell>0 / dividend>0 / withholding_tax<0 are repaired structurally.",
    ),
)


BALANCES = CsvDataset(
    key="balances",
    purpose=(
        "Per-account dated balance points. Past-dated rows backfill the net worth "
        "graph (the read path carries balances forward by local day)."
    ),
    direction="import_export",
    identity_columns=("institution_name", "account_name", "account_external_id", "date"),
    columns=(
        CsvColumn(
            "date",
            "Balance-as-of date, user-local calendar day (YYYY-MM-DD). Import upserts "
            "one row per account per local day; re-import overwrites that day.",
            required_on_import=True,
            example="2026-01-31",
            title="Balance Date",
        ),
        CsvColumn("institution_name", "Institution display name (or file-level assignment).", required_on_import=True, example="Interactive Brokers"),
        CsvColumn("institution_provider", "Provider key for same-name disambiguation. Backup-only column.", export_only=True, internal=True, example="ibkr"),
        CsvColumn("account_name", "Account display name (or file-level assignment).", required_on_import=True, example="Margin"),
        CsvColumn("account_external_id", "Provider account id/mask for precise re-import matching. Backup-only column.", internal=True, example="account:U123456"),
        CsvColumn("account_type", "Account classification (open set incl. 'cash').", example="margin"),
        CsvColumn(
            "is_liability",
            "Asset vs liability flag (true/false). Export context; ignored on import "
            "(liability status comes from the resolved account).",
            export_only=True,
            example="false",
        ),
        CsvColumn(
            "balance",
            "Display-signed balance in the row's native currency. Liability amounts owed "
            "export as negative, while a liability credit balance exports as positive. "
            "Assets export as stored and may be negative (for example, an overdraft).",
            required_on_import=True,
            example="261500.00",
        ),
        CsvColumn("currency", "ISO 4217 native currency. Not required; defaults to account/primary.", example="USD"),
        CsvColumn("account_hidden", "Account hidden flag for readable-export completeness. The current CSV importer does not restore it.", export_only=True, internal=True, example="false"),
        CsvColumn("institution_hidden", "Institution hidden flag for readable-export completeness. The current CSV importer does not restore it.", export_only=True, internal=True, example="false"),
        CsvColumn("opening_balance", "Manual account opening balance, when configured. Export-only account context.", export_only=True, internal=True, example="1000.00"),
        CsvColumn("opening_balance_date", "Manual account opening date (YYYY-MM-DD), when configured. Export-only account context.", export_only=True, internal=True, example="2026-01-01"),
        CsvColumn("purchase_value", "Tangible asset purchase value, when configured. Export-only account context.", export_only=True, internal=True, example="350000.00"),
        CsvColumn("purchase_date", "Tangible asset purchase date (YYYY-MM-DD), when configured. Export-only account context.", export_only=True, internal=True, example="2020-06-15"),
        CsvColumn("secured_asset_account", "Display name of the asset linked to a liability, when configured. Export-only account context.", export_only=True, internal=True, example="Primary Home"),
        CsvColumn("is_imported_account", "Whether the account was created from historical provider data. Export-only account context.", export_only=True, internal=True, example="false"),
    ),
    notes=(
        "Exact graph reproduction needs FX coverage back to the earliest imported "
        "balance date; import should trigger FX backfill over the imported range.",
        "A complete readable export includes hidden accounts/institutions.",
    ),
)


HOLDINGS = CsvDataset(
    key="holdings",
    purpose="Current-state position snapshot per account (not a historical series).",
    direction="export_only",
    identity_columns=("institution_name", "account_name", "symbol", "contract_multiplier"),
    columns=(
        CsvColumn("institution_name", "Institution display name (or file-level assignment).", required_on_import=True, example="Interactive Brokers"),
        CsvColumn("account_name", "Account display name (or file-level assignment).", required_on_import=True, example="Margin"),
        CsvColumn("account_type", "Account classification (open set).", example="margin"),
        CsvColumn("symbol", "Security ticker.", required_on_import=True, example="AAPL"),
        CsvColumn("name", "Instrument display name.", example="Apple Inc."),
        CsvColumn("last_price", "Latest price per unit (provider price, else derived from market value). Volatile snapshot.", export_only=True, example="185.00"),
        CsvColumn("average_cost", "Average cost per unit = total cost basis / quantity (2 dp).", example="150.25"),
        CsvColumn("unrealized_pnl_pct", "Unrealized profit/loss vs cost basis, as a percent. Computed; export-only.", export_only=True, example="23.15", title="Unrealized P&L %"),
        CsvColumn("quantity", "Units/shares held.", required_on_import=True, example="100"),
        CsvColumn("cost_basis", "Money cost basis / book value (options x contract multiplier, signed by position). Computed; export-only.", export_only=True, example="15025.00"),
        CsvColumn("market_value", "Current market value (volatile snapshot).", export_only=True, example="18500.00"),
        CsvColumn("currency", "ISO 4217 native currency of the position.", example="USD"),
        # --- Backup-only columns (emitted only with include_internal_ids) ---
        CsvColumn("institution_provider", "Provider key for same-name disambiguation. Backup-only.", export_only=True, internal=True, example="ibkr"),
        CsvColumn("account_external_id", "Provider account id/mask for precise match. Backup-only.", internal=True, example="account:U123456"),
        CsvColumn(
            "contract_multiplier",
            "Contract multiplier (options/derivatives); part of position identity. Backup-only.",
            internal=True,
            example="100",
        ),
        CsvColumn("sector", "Sector/classification when known. Backup-only.", internal=True, example="Technology"),
        CsvColumn("change_pct", "Daily change %. Backup-only.", export_only=True, internal=True, example="0.42"),
        CsvColumn("daily_pnl", "Daily P&L. Backup-only.", export_only=True, internal=True, example="77.00"),
        CsvColumn("as_of", "Snapshot timestamp (ISO 8601 UTC). Backup-only.", export_only=True, internal=True, example="2026-06-06T03:00:00Z"),
    ),
    notes=(
        "Import is upsert by (account, symbol, contract_multiplier); it does NOT "
        "delete positions absent from the file (not a destructive snapshot replace "
        "unless an explicit per-account replace mode is requested).",
    ),
)


NETWORTH_HISTORY = CsvDataset(
    key="networth_history",
    purpose=(
        "Daily net worth series in the user's primary currency (FX-converted per date), "
        "for backup/charting and mirroring the dashboard graph. "
        "Derived from balance history (carry-forward); NOT importable and not the "
        "backfill source — import balances to backfill the graph instead."
    ),
    direction="export_only",
    identity_columns=("date",),
    columns=(
        CsvColumn("date", "ISO 8601 user-local day (YYYY-MM-DD). No synthetic 'current' row is emitted.", example="2026-01-31"),
        CsvColumn("total_assets", "Sum of asset balances, FX-converted to the user's primary currency (matches the dashboard).", example="293671.49"),
        CsvColumn("total_liabilities", "Sum of liability balances, signed negative (debt; a net credit balance reads positive), in primary currency.", example="-32424.92"),
        CsvColumn("net_worth", "total_assets - total_liabilities (signed), in primary currency (matches the dashboard).", example="261246.57"),
        CsvColumn("currency", "User's primary currency (ISO 4217).", example="CAD"),
    ),
    notes=("Export-only; not deduped or importable.",)
)


INCOME = CsvDataset(
    key="income",
    purpose=(
        "Dividend/interest income aggregated by position, currency, and month, in each "
        "holding's NATIVE currency (export-only). Scoped to the Income view's account "
        "filter and timeline. Raw income rows live in the transactions export."
    ),
    direction="export_only",
    columns=(
        CsvColumn("symbol", "Security ticker (blank for interest, which has no position).", example="AAPL"),
        CsvColumn("month", "Calendar month (YYYY-MM).", example="2026-01"),
        CsvColumn("income", "Income received that month for this position, in the row's native currency.", example="120.00"),
        CsvColumn("withholding", "Withholding tax for that position-month (negative; tax paid), native currency.", example="-18.00"),
        CsvColumn("total", "Net income after withholding (income + withholding), native currency.", example="102.00"),
        CsvColumn("currency", "Native currency of the income (ISO 4217).", example="USD"),
    ),
    notes=("Export-only aggregation; not importable. Interest splits by currency, so a month can have two interest rows.",),
)


DATASETS: dict[str, CsvDataset] = {
    ds.key: ds for ds in (TRANSACTIONS, BALANCES, HOLDINGS, NETWORTH_HISTORY, INCOME)
}


# --- Header helpers ----------------------------------------------------------


def export_headers(dataset_key: str, *, include_internal_ids: bool = False) -> list[str]:
    """Ordered headers an export writes (excludes import-only and, by default, internal ids)."""
    dataset = DATASETS[dataset_key]
    return [
        column.header
        for column in dataset.columns
        if not column.import_only and (include_internal_ids or not column.internal)
    ]


_HEADER_ACRONYMS = {"id": "ID", "mcc": "MCC", "csv": "CSV", "fx": "FX", "url": "URL"}


def _default_title(header: str) -> str:
    """Derive a Title Case display label from a snake_case header."""
    words = []
    for word in str(header).split("_"):
        if not word:
            continue
        words.append(_HEADER_ACRONYMS.get(word, word.capitalize()))
    return " ".join(words)


def column_title(column: CsvColumn) -> str:
    return column.title or _default_title(column.header)


def export_header_labels(dataset_key: str, *, include_internal_ids: bool = False) -> list[str]:
    """Human-friendly header-row labels, aligned 1:1 with export_headers()."""
    dataset = DATASETS[dataset_key]
    return [
        column_title(column)
        for column in dataset.columns
        if not column.import_only and (include_internal_ids or not column.internal)
    ]


def normalize_header_key(name: str) -> str:
    """Fold a CSV header to a comparison key (lowercase alphanumerics only) so
    'Account Name', 'account_name', and 'ACCOUNT  NAME' all match."""
    return "".join(ch for ch in str(name).strip().lower() if ch.isalnum())


def canonical_header_map(dataset_key: str) -> dict[str, str]:
    """Map normalized header/title variants -> canonical column header, so import
    accepts both the friendly export labels and the snake_case keys."""
    dataset = DATASETS[dataset_key]
    mapping: dict[str, str] = {}
    for column in dataset.columns:
        mapping[normalize_header_key(column.header)] = column.header
        mapping[normalize_header_key(column_title(column))] = column.header
    return mapping


# --- Deterministic external_id minting / reserved-prefix guard ---------------


def is_reserved_external_id(external_id: str | None) -> bool:
    """True if external_id sits in a reserved namespace (manual/cash_opening/cash_mirror)."""
    text = external_id or ""
    return any(text.startswith(prefix) for prefix in RESERVED_EXTERNAL_ID_PREFIXES)


def mint_external_id(
    *,
    institution_name: str | None,
    account_name: str | None,
    date_iso: str | None,
    tx_type: str | None,
    amount: float | int | str | None,
    currency: str | None,
    description: str | None = None,
    symbol: str | None = None,
    occurrence: int = 0,
) -> str:
    """Deterministic external_id for a foreign CSV row that carries none.

    Hashes NORMALIZED canonical fields so the same logical row maps to one id
    across export variants, and re-importing the same file is idempotent. The
    ``occurrence`` disambiguator separates genuinely identical same-day rows
    (e.g. two identical coffees) so they are not silently merged. Inputs must be
    normalized (ISO date, whitelisted type) BEFORE calling for stable results.
    """
    try:
        amount_key = f"{float(amount or 0):.{MONEY_DECIMALS}f}"
    except (TypeError, ValueError):
        amount_key = str(amount or "")
    parts = (
        (institution_name or "").strip().lower(),
        (account_name or "").strip().lower(),
        (date_iso or "").strip(),
        (tx_type or "").strip().lower(),
        amount_key,
        (currency or "").strip().upper(),
        " ".join((description or "").split()).lower(),
        (symbol or "").strip().upper(),
        str(int(occurrence or 0)),
    )
    digest = hashlib.sha1("|".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"{FOREIGN_IMPORT_PREFIX}{digest}"


# --- Category path codec -----------------------------------------------------


def join_category_path(parent_name: str | None, child_name: str | None) -> str:
    """Build a 'Parent > Child' path; collapses to the present segment if only one."""
    parent = (parent_name or "").strip()
    child = (child_name or "").strip()
    if parent and child:
        return f"{parent}{CATEGORY_PATH_SEPARATOR}{child}"
    return child or parent


def split_category_path(path: str | None) -> tuple[str | None, str | None]:
    """Parse a category path into (parent_name, leaf_name). Tolerates ' > ' or '>'.

    A single segment is treated as the leaf with no parent. The leaf is always
    the last segment so deeper-than-two inputs still resolve to a leaf name.
    """
    if not path:
        return (None, None)
    segments = [segment.strip() for segment in str(path).split(">")]
    segments = [segment for segment in segments if segment]
    if not segments:
        return (None, None)
    if len(segments) == 1:
        return (None, segments[0])
    return (segments[0], segments[-1])


# --- Value (de)serialization -------------------------------------------------


def serialize_local_date(value: datetime | date | None, user_tz) -> str:
    """Render a stored datetime as the user-local calendar day (YYYY-MM-DD)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(user_tz).date().isoformat()
    return value.isoformat()


def parse_csv_date_to_utc(value: str | None, user_tz) -> datetime | None:
    """Parse a CSV date to an aware UTC datetime.

    A date-only ``YYYY-MM-DD`` is anchored to LOCAL NOON then converted to UTC so
    a re-import lands on the same local day (matching the manual-row noon trick).
    A full datetime with offset/``Z`` is honored and normalized to UTC. Non-ISO or
    ambiguous formats (e.g. MM/DD/YYYY, DD/MM/YYYY) return ``None`` so the import
    service skips the row with an "unparseable date" error rather than crashing; a
    declared file-level date format is a future enhancement.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        if "T" in text or (len(text) > 10 and " " in text):
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        parsed_date = date.fromisoformat(text[:10])
    except ValueError:
        return None
    local_noon = datetime(parsed_date.year, parsed_date.month, parsed_date.day, 12, 0, 0, tzinfo=user_tz)
    return local_noon.astimezone(timezone.utc)


def serialize_datetime_utc(value: datetime | None) -> str:
    """Render a datetime as ISO 8601 UTC with a trailing Z (export-only columns)."""
    if value is None:
        return ""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def serialize_decimal(value: float | int | None, places: int = MONEY_DECIMALS) -> str:
    """Render a number as a fixed-precision plain decimal (empty for None/non-finite)."""
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return f"{number:.{places}f}"


def serialize_quantity(value: float | int | None, places: int = 2) -> str:
    """2-decimal number, but keeps more precision when rounding to `places` would
    turn a non-zero value into zero (so tiny crypto quantities aren't lost)."""
    text = serialize_decimal(value, places)
    if text == "":
        return ""
    try:
        if float(value) != 0 and float(text) == 0:
            precise = serialize_decimal(value, QUANTITY_DECIMALS)
            if "." in precise:
                precise = precise.rstrip("0").rstrip(".")
            return precise
    except (TypeError, ValueError):
        pass
    return text


def parse_decimal(value: str | None) -> float | None:
    """Parse a plain decimal, tolerating thousands separators; empty -> None."""
    text = (value or "").strip().replace(",", "")
    if text == "":
        return None
    return float(text)


def serialize_bool(value: bool | None) -> str:
    """Render a boolean as lowercase true/false (empty for None)."""
    if value is None:
        return ""
    return "true" if value else "false"


def parse_bool(value: str | None) -> bool | None:
    """Parse a boolean; empty/unknown -> None."""
    text = (value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def normalize_currency(value: str | None, default: str | None = None) -> str | None:
    """Uppercase/trim an ISO 4217 code; falls back to default when blank."""
    text = (value or "").strip().upper()
    return text or (default.upper() if default else None)
