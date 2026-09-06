from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, false, text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base


UTC_TIMESTAMP = DateTime(timezone=True)
MONEY = Numeric(24, 8)
QUANTITY = Numeric(30, 12)
RATE = Numeric(30, 12)
PERCENT = Numeric(18, 8)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=True)
    created_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))

    institutions = relationship("Institution", back_populates="user")
    accounts = relationship("Account", back_populates="user")
    holdings = relationship("Holding", back_populates="user")
    transactions = relationship("Transaction", back_populates="user")
    balance_history = relationship("BalanceHistory", back_populates="user")
    settings = relationship("Setting", back_populates="user")
    categories = relationship("Category", back_populates="user")
    recurring_series = relationship("RecurringSeries", back_populates="user")


class Institution(Base):
    __tablename__ = "institutions"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_institutions_id_user_id"),
        Index(
            "uq_institutions_user_synced_provider",
            "user_id",
            "provider",
            unique=True,
            sqlite_where=text("type IN ('api', 'scraper')"),
            postgresql_where=text("type IN ('api', 'scraper')"),
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # "api" or "scraper"
    provider = Column(String, nullable=False)  # connector brand; one synced row per user/provider
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))

    hidden = Column(Boolean, nullable=False, default=False)
    sync_status = Column(String, nullable=False, default="ok")
    # Manual-institution presentation/classification (redesigned wizard): a user-uploaded
    # logo stored in-DB (served owner-scoped via GET /institutions/{id}/logo) and the
    # Banking/Crypto category. NULL for synced institutions (logo from bundled assets,
    # category derived from the provider catalog).
    logo = Column(LargeBinary, nullable=True)
    logo_mime = Column(String, nullable=True)
    category = Column(String, nullable=True)
    # Per-institution cash-tracking cutoff: ATM/cash dated before it stays an ordinary
    # Withdrawal; on/after it becomes wallet Cash. Defaults to the connection date so a
    # bank added later doesn't backfill its whole history as current cash.
    cash_tracking_start = Column(Date, default=lambda: datetime.now(timezone.utc).date())
    user = relationship("User", back_populates="institutions")
    accounts = relationship(
        "Account",
        back_populates="institution",
        foreign_keys="Account.institution_id",
    )


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_accounts_id_user_id"),
        UniqueConstraint(
            "id",
            "institution_id",
            "user_id",
            name="uq_accounts_id_institution_id_user_id",
        ),
        UniqueConstraint(
            "user_id",
            "institution_id",
            "external_id",
            name="uq_accounts_user_id_institution_id_external_id",
        ),
        ForeignKeyConstraint(
            ["institution_id", "user_id"],
            ["institutions.id", "institutions.user_id"],
            name="fk_accounts_institution_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["secured_asset_account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_accounts_secured_asset_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "secured_asset_account_id IS NULL OR secured_asset_account_id <> id",
            name="ck_accounts_secured_asset_not_self",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    institution_id = Column(Integer, nullable=False, index=True)
    external_id = Column(String, nullable=True)
    name = Column(String, nullable=False)
    account_type = Column(String)  # free-form, e.g. "chequing"/"savings"/"credit_card"/"loc"/"tfsa"/"rrsp"/"fhsa"/"margin"/"crypto"/"cash"
    currency = Column(String, nullable=False, default="CAD")
    is_liability = Column(Boolean, nullable=False, default=False)
    hidden = Column(Boolean, nullable=False, default=False)
    created_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    # True for accounts materialized from imported historical data (e.g. Questrade PDF
    # statements of a closed/transferred account) rather than a live provider sync. Such
    # an account lives under its real provider institution but is never synced, and its
    # "change" baseline is its first imported point (like a manual asset), so pre-import
    # reconstruction is measured in full and not clamped to the institution connect date.
    is_imported = Column(Boolean, default=False, nullable=False, server_default=false())
    last_synced = Column(UTC_TIMESTAMP)
    # Manual "starting balance" anchor (manual institutions + future tangible assets).
    # With transactions, the running balance is derived as opening_balance ± cumsum
    # (services.manual_institutions.recompute_manual_account_balance); without
    # transactions it is the value itself. NULL for synced accounts (provider-reported).
    opening_balance = Column(MONEY, nullable=True)
    opening_balance_date = Column(Date, nullable=True)
    # Tangible-asset acquisition cost basis (entered on the Add modal, or backfilled
    # later in the asset's settings). Also mirrored as the earliest balance_history
    # point so "All Time Change" reads as appreciation from purchase; NULL = not entered.
    purchase_value = Column(MONEY, nullable=True)
    purchase_date = Column(Date, nullable=True)
    # Asset↔liability link: on a LIABILITY account, the asset account it finances/secures
    # (mortgage → house, auto loan → car). Many liabilities → one asset; NULL = unsecured.
    # Drives the per-asset equity view; deleting the secured asset leaves the liability
    # intact and clears only the optional link.
    secured_asset_account_id = Column(Integer, nullable=True, index=True)

    user = relationship("User", back_populates="accounts")
    institution = relationship(
        "Institution",
        back_populates="accounts",
        foreign_keys=[institution_id],
    )
    balances = relationship(
        "BalanceHistory",
        back_populates="account",
        foreign_keys="BalanceHistory.account_id",
    )
    holdings = relationship(
        "Holding",
        back_populates="account",
        foreign_keys="Holding.account_id",
    )
    transactions = relationship(
        "Transaction",
        back_populates="account",
        foreign_keys="Transaction.account_id",
    )


class BalanceHistory(Base):
    __tablename__ = "balance_history"
    __table_args__ = (
        UniqueConstraint("account_id", "date", name="uq_balance_history_account_date"),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_balance_history_account_owner",
            ondelete="CASCADE",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    balance = Column(MONEY, nullable=False)
    date = Column(Date, nullable=False, default=lambda: datetime.now(timezone.utc).date(), index=True)

    user = relationship("User", back_populates="balance_history")
    account = relationship("Account", back_populates="balances", foreign_keys=[account_id])


class Holding(Base):
    __tablename__ = "holdings"
    __table_args__ = (
        UniqueConstraint("account_id", "symbol", name="uq_holdings_account_symbol"),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_holdings_account_owner",
            ondelete="CASCADE",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String, nullable=False)
    name = Column(String)
    quantity = Column(QUANTITY, nullable=False)
    market_value = Column(MONEY)
    average_cost = Column(MONEY)
    last_price = Column(MONEY)
    contract_multiplier = Column(QUANTITY)
    change_pct = Column(PERCENT)
    daily_pnl = Column(MONEY)
    currency = Column(String, nullable=False, default="CAD")
    sector = Column(String)
    last_updated = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="holdings")
    account = relationship("Account", back_populates="holdings", foreign_keys=[account_id])


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_settings_user_id_key"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    key = Column(String, nullable=False)
    value = Column(String, nullable=False)

    user = relationship("User", back_populates="settings")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_categories_id_user_id"),
        ForeignKeyConstraint(
            ["parent_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_categories_parent_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_categories_parent_not_self"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    parent_id = Column(Integer, nullable=True, index=True)
    name = Column(String, nullable=False)
    icon = Column(String, nullable=True)
    icon_set = Column(String, nullable=True)  # "twemoji" | "noto" | "fluent"; falls back to user default
    color_dark = Column(String, nullable=True)
    color_light = Column(String, nullable=True)
    classification = Column(String, nullable=False)  # income | expense | transfer | investment
    is_system = Column(Boolean, default=False, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    # Stable per-seed identifier (e.g. "coffee", "online_shopping"). Set on
    # seeded system rows; NULL for user-created categories. Survives user
    # renames so bundled rules can resolve targets without depending on the
    # current display name.
    seed_key = Column(String, nullable=True, index=True)
    created_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="categories")
    parent = relationship(
        "Category",
        back_populates="children",
        foreign_keys=[parent_id],
        remote_side=[id],
    )
    children = relationship(
        "Category",
        back_populates="parent",
        foreign_keys=[parent_id],
    )
    transactions = relationship(
        "Transaction",
        back_populates="category",
        foreign_keys="Transaction.category_id",
    )
    rules = relationship(
        "CategoryRule",
        back_populates="category",
        foreign_keys="CategoryRule.category_id",
    )


class CategoryRule(Base):
    __tablename__ = "category_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["category_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_category_rules_category_owner",
            ondelete="CASCADE",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    category_id = Column(Integer, nullable=False, index=True)
    priority = Column(Integer, default=100, nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    description_pattern = Column(String, nullable=False)
    is_regex = Column(Boolean, default=False, nullable=False)
    provider = Column(String, nullable=True)
    account_type = Column(String, nullable=True)
    amount_sign = Column(String, nullable=True)  # "positive" | "negative" | None
    transaction_type = Column(String, nullable=True)

    category = relationship("Category", back_populates="rules", foreign_keys=[category_id])


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="uq_transactions_account_id_external_id"),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["accounts.id", "accounts.user_id"],
            name="fk_transactions_account_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["category_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_transactions_category_owner",
        ),
        ForeignKeyConstraint(
            ["recurring_series_id", "user_id"],
            ["recurring_series.id", "recurring_series.user_id"],
            name="fk_transactions_recurring_series_owner",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    type = Column(String, nullable=False)  # dividend, buy, sell, deposit, withdrawal, fee, interest, distribution, transfer, withholding_tax
    symbol = Column(String, nullable=True)
    description = Column(String, nullable=True)
    amount = Column(MONEY, nullable=False)  # positive for inflows, negative for outflows
    # Signed amount captured before the first manual income/expense reclassification
    # flips the sign, so a revert (category delete / reset-to-default) can restore the
    # true provider sign — needed for sign-routed types like interest. NULL = amount
    # has never been flipped (it is already raw).
    raw_amount = Column(MONEY, nullable=True)
    currency = Column(String, nullable=False, default="CAD")
    quantity = Column(QUANTITY, nullable=True)
    price = Column(MONEY, nullable=True)
    commission = Column(MONEY, nullable=True)
    mcc = Column(String, nullable=True)  # Visa/MC merchant category code (e.g. "5812") when the provider supplies it
    asset_category = Column(String, nullable=True)  # provider asset class (e.g. "FUT", "STK", "OPT") for derivative handling; null when unknown
    realized_pnl = Column(MONEY, nullable=True)  # broker realized P&L on the trade (e.g. IBKR fifoPnlRealized) — the true cash impact of a futures/derivative close (0 on an opening trade)
    provider_recurring = Column(Boolean, nullable=True)  # provider's own recurring/pre-auth flag, when supplied
    external_id = Column(String, nullable=False)  # "{provider}_{unique_key}" for deduplication
    category_id = Column(Integer, nullable=True, index=True)
    category_source = Column(String, nullable=True)  # "auto" | "rule" | "manual" | "import" (CSV-provided)
    user_description = Column(String, nullable=True)  # user override; raw `description` stays untouched
    user_notes = Column(Text, nullable=True)
    recurring_series_id = Column(Integer, nullable=True, index=True)
    created_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="transactions")
    account = relationship("Account", back_populates="transactions", foreign_keys=[account_id])
    category = relationship("Category", back_populates="transactions", foreign_keys=[category_id])
    recurring_series = relationship(
        "RecurringSeries",
        back_populates="transactions",
        foreign_keys=[recurring_series_id],
    )


class TransactionImportAccountState(Base):
    __tablename__ = "transaction_import_account_states"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "institution_id",
            "account_external_id",
            name="uq_transaction_import_account_states_connection_account",
        ),
        ForeignKeyConstraint(
            ["institution_id", "user_id"],
            ["institutions.id", "institutions.user_id"],
            name="fk_transaction_import_states_connection_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "institution_id", "user_id"],
            ["accounts.id", "accounts.institution_id", "accounts.user_id"],
            name="fk_transaction_import_states_account_connection_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "target_start_date IS NULL OR target_end_date IS NULL OR target_start_date <= target_end_date",
            name="ck_transaction_import_states_target_date_order",
        ),
        CheckConstraint(
            "incremental_start_date IS NULL OR incremental_end_date IS NULL OR incremental_start_date <= incremental_end_date",
            name="ck_transaction_import_states_incremental_date_order",
        ),
        CheckConstraint(
            "last_successful_window_start_date IS NULL OR last_successful_window_end_date IS NULL OR last_successful_window_start_date <= last_successful_window_end_date",
            name="ck_transaction_import_states_success_date_order",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    institution_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    account_external_id = Column(String, nullable=False, index=True)
    account_name = Column(String, nullable=False)
    account_type = Column(String, nullable=True)
    is_liability = Column(Boolean, default=False, nullable=False, server_default=false())
    backfill_status = Column(String, nullable=False, default="not_started")
    target_start_date = Column(Date, nullable=True)
    target_end_date = Column(Date, nullable=True)
    backfill_completed_at = Column(UTC_TIMESTAMP, nullable=True)
    incremental_start_date = Column(Date, nullable=True)
    incremental_end_date = Column(Date, nullable=True)
    last_successful_window_start_date = Column(Date, nullable=True)
    last_successful_window_end_date = Column(Date, nullable=True)
    last_successful_fetch_at = Column(UTC_TIMESTAMP, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))


class TransactionImportWindow(Base):
    __tablename__ = "transaction_import_windows"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "institution_id",
            "account_external_id",
            "mode",
            "window_start_date",
            "window_end_date",
            name="uq_transaction_import_windows_connection_account_window",
        ),
        ForeignKeyConstraint(
            ["institution_id", "user_id"],
            ["institutions.id", "institutions.user_id"],
            name="fk_transaction_import_windows_connection_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "institution_id", "user_id"],
            ["accounts.id", "accounts.institution_id", "accounts.user_id"],
            name="fk_transaction_import_windows_account_connection_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint("window_start_date <= window_end_date", name="ck_transaction_import_windows_date_order"),
        CheckConstraint("transaction_count >= 0", name="ck_transaction_import_windows_transaction_count"),
        CheckConstraint("attempt_count >= 0", name="ck_transaction_import_windows_attempt_count"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    institution_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    account_external_id = Column(String, nullable=False, index=True)
    mode = Column(String, nullable=False, default="backfill")
    window_start_date = Column(Date, nullable=False, index=True)
    window_end_date = Column(Date, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    transaction_count = Column(Integer, nullable=False, default=0)
    attempt_count = Column(Integer, nullable=False, default=0)
    sync_id = Column(String, nullable=True, index=True)
    last_error = Column(Text, nullable=True)
    started_at = Column(UTC_TIMESTAMP, nullable=True)
    completed_at = Column(UTC_TIMESTAMP, nullable=True)
    updated_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))


class TransactionImportJob(Base):
    __tablename__ = "transaction_import_jobs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_transaction_import_jobs_job_id"),
        ForeignKeyConstraint(
            ["institution_id", "user_id"],
            ["institutions.id", "institutions.user_id"],
            name="fk_transaction_import_jobs_connection_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "stale_recovery_count >= 0",
            name="ck_transaction_import_jobs_stale_recovery_count",
        ),
        Index(
            "uq_transaction_import_jobs_active_connection_provider",
            "user_id",
            "institution_id",
            "provider",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    institution_id = Column(Integer, nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    job_id = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="queued", index=True)
    reason = Column(String, nullable=True)
    sync_id = Column(String, nullable=True, index=True)
    source_sync_id = Column(String, nullable=True, index=True)
    attempt_id = Column(String, nullable=True, index=True)
    last_error = Column(Text, nullable=True)
    last_progress_at = Column(UTC_TIMESTAMP, nullable=True, index=True)
    last_progress_label = Column(String, nullable=True)
    stale_recovery_count = Column(Integer, nullable=False, default=0)
    lease_token = Column(String, nullable=True, index=True)
    lease_expires_at = Column(UTC_TIMESTAMP, nullable=True, index=True)
    created_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    started_at = Column(UTC_TIMESTAMP, nullable=True)
    finished_at = Column(UTC_TIMESTAMP, nullable=True)
    updated_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))


class ProviderSyncLease(Base):
    __tablename__ = "provider_sync_leases"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "institution_key",
            name="uq_provider_sync_leases_user_provider_connection",
        ),
        UniqueConstraint("owner_token", name="uq_provider_sync_leases_owner_token"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    institution_key = Column(String, nullable=False)
    owner_token = Column(String, nullable=False)
    acquired_at = Column(UTC_TIMESTAMP, nullable=False)
    expires_at = Column(UTC_TIMESTAMP, nullable=False, index=True)


class PendingSyncStatus(Base):
    __tablename__ = "pending_sync_statuses"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "status_provider",
            "institution_key",
            name="uq_pending_sync_statuses_user_provider_connection",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status_provider = Column(String, nullable=False, index=True)
    institution_key = Column(String, nullable=False)
    final_status = Column(String, nullable=False)
    severity = Column(Integer, nullable=False, default=0)
    updated_at = Column(UTC_TIMESTAMP, nullable=False)


class SyncBatchJob(Base):
    __tablename__ = "sync_batch_jobs"
    __table_args__ = (
        UniqueConstraint("batch_id", name="uq_sync_batch_jobs_batch_id"),
        Index(
            "uq_sync_batch_jobs_active_signature",
            "user_id",
            "mode",
            "connection_signature",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id = Column(String, nullable=False, index=True)
    mode = Column(String, nullable=False)
    status = Column(String, nullable=False, index=True)
    connection_signature = Column(Text, nullable=False)
    state_json = Column(Text, nullable=False)
    lease_token = Column(String, nullable=True, index=True)
    lease_expires_at = Column(UTC_TIMESTAMP, nullable=True, index=True)
    created_at = Column(UTC_TIMESTAMP, nullable=False)
    updated_at = Column(UTC_TIMESTAMP, nullable=False, index=True)
    finished_at = Column(UTC_TIMESTAMP, nullable=True)


class SymbolSector(Base):
    __tablename__ = "symbol_sectors"

    symbol = Column(String, primary_key=True)  # normalized uppercase, e.g. "TSLA", "BEP.UN"
    sector = Column(String, nullable=True)       # None is terminal only when source is "local"
    instrument_kind = Column(String, nullable=True)  # "etf", "fund", or None for equities
    source = Column(String, nullable=False, default="fmp")  # provider id, "etf", or "local"


class SymbolName(Base):
    __tablename__ = "symbol_names"

    provider = Column(String, primary_key=True)  # "coinbase", "questrade", "ibkr"
    identifier = Column(String, primary_key=True)  # currency code, symbolId, or conid
    name = Column(String, nullable=False)
    updated_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))


class BenchmarkPrice(Base):
    __tablename__ = "benchmark_prices"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "symbol",
            "date",
            name="uq_benchmark_prices_provider_symbol_date",
        ),
    )

    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    close = Column(MONEY, nullable=False)
    fetched_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))


class RecurringSeries(Base):
    __tablename__ = "recurring_series"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_recurring_series_id_user_id"),
        UniqueConstraint(
            "user_id",
            "merchant_key",
            "direction",
            name="uq_recurring_series_user_merchant_direction",
        ),
        ForeignKeyConstraint(
            ["category_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_recurring_series_category_owner",
        ),
        ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_recurring_series_category_delete",
            ondelete="SET NULL",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_recurring_series_confidence"),
        CheckConstraint("occurrence_count >= 0", name="ck_recurring_series_occurrence_count"),
        CheckConstraint(
            "amount_min IS NULL OR amount_max IS NULL OR amount_min <= amount_max",
            name="ck_recurring_series_amount_range",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    merchant_key = Column(String, nullable=False, index=True)  # normalized fingerprint for grouping/upsert
    direction = Column(String, nullable=False, default="outflow")  # "inflow" | "outflow"
    display_name = Column(String, nullable=False)  # detected label (best raw description seen)
    category_id = Column(Integer, nullable=True, index=True)
    cadence = Column(String, nullable=False, default="irregular")  # weekly|biweekly|semimonthly|monthly|quarterly|annual|irregular
    interval_days = Column(Float, nullable=True)  # median observed interval in days
    avg_amount = Column(MONEY, nullable=False, default=0)  # average signed amount
    amount_min = Column(MONEY, nullable=True)
    amount_max = Column(MONEY, nullable=True)
    currency = Column(String, nullable=False, default="CAD")
    last_seen_date = Column(Date, nullable=True)
    next_expected_date = Column(Date, nullable=True)
    occurrence_count = Column(Integer, nullable=False, default=0)
    confidence = Column(Float, nullable=False, default=0.0)  # 0..1 detection confidence
    status = Column(String, nullable=False, default="active", index=True)  # active|lapsed|dismissed
    is_confirmed = Column(Boolean, nullable=False, default=False)  # user-confirmed recurring
    created_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="recurring_series")
    category = relationship("Category", foreign_keys=[category_id])
    transactions = relationship(
        "Transaction",
        back_populates="recurring_series",
        foreign_keys="Transaction.recurring_series_id",
    )


class MarketStripSnapshotPoint(Base):
    __tablename__ = "market_strip_snapshot_points"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "source",
            "mode",
            "symbol_key",
            "local_date",
            "sample_slot",
            name="uq_market_strip_snapshot_points_sample",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False)
    mode = Column(String, nullable=False)
    symbol_key = Column(String, nullable=False, index=True)
    local_date = Column(Date, nullable=False, index=True)
    sample_slot = Column(String, nullable=False)
    close = Column(MONEY, nullable=False)
    observed_at = Column(UTC_TIMESTAMP, nullable=False)


class RuntimeServiceLease(Base):
    __tablename__ = "runtime_service_leases"
    __table_args__ = (
        UniqueConstraint(
            "service_name",
            name="uq_runtime_service_leases_service_name",
        ),
        UniqueConstraint(
            "owner_token",
            name="uq_runtime_service_leases_owner_token",
        ),
    )

    id = Column(Integer, primary_key=True)
    service_name = Column(String, nullable=False)
    owner_token = Column(String, nullable=False)
    acquired_at = Column(UTC_TIMESTAMP, nullable=False)
    expires_at = Column(UTC_TIMESTAMP, nullable=False, index=True)


class EventStreamLease(Base):
    __tablename__ = "event_stream_leases"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "user_slot",
            name="uq_event_stream_leases_user_slot",
        ),
        UniqueConstraint(
            "global_slot",
            name="uq_event_stream_leases_global_slot",
        ),
        UniqueConstraint(
            "lease_id",
            name="uq_event_stream_leases_lease_id",
        ),
        CheckConstraint(
            "user_slot >= 1 AND user_slot <= 4",
            name="ck_event_stream_leases_user_slot",
        ),
        CheckConstraint(
            "global_slot >= 1 AND global_slot <= 128",
            name="ck_event_stream_leases_global_slot",
        ),
    )

    id = Column(Integer, primary_key=True)
    lease_id = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user_slot = Column(Integer, nullable=False)
    global_slot = Column(Integer, nullable=False)
    acquired_at = Column(UTC_TIMESTAMP, nullable=False)
    expires_at = Column(UTC_TIMESTAMP, nullable=False, index=True)


class ConnectionAuthArtifact(Base):
    __tablename__ = "connection_auth_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "institution_id",
            "artifact_kind",
            "slot",
            name="uq_connection_auth_artifacts_user_id_institution_id_artifact_kind_slot",
        ),
        ForeignKeyConstraint(
            ["institution_id", "user_id"],
            ["institutions.id", "institutions.user_id"],
            name="fk_connection_auth_artifacts_connection_owner",
            ondelete="CASCADE",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    institution_id = Column(Integer, nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    artifact_kind = Column(String, nullable=False)
    slot = Column(String, nullable=False, default="active")
    payload_json = Column(Text, nullable=False)
    updated_at = Column(UTC_TIMESTAMP, nullable=False)


class VisibleAuthAttemptArtifact(Base):
    __tablename__ = "visible_auth_attempt_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "attempt_id",
            "artifact_kind",
            name="uq_visible_auth_attempt_artifacts_user_provider_attempt_kind",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    attempt_id = Column(String, nullable=False, index=True)
    artifact_kind = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)
    updated_at = Column(UTC_TIMESTAMP, nullable=False)


class UserSecretArtifact(Base):
    __tablename__ = "user_secret_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "secret_kind",
            name="uq_user_secret_artifacts_user_id_secret_kind",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    secret_kind = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)
    updated_at = Column(UTC_TIMESTAMP, nullable=False)


class FxRate(Base):
    """Historical daily FX rates (global market data, not user-scoped).

    One row per (date, currency); `cad_per_unit` is the CAD value of 1 unit of
    `currency` on `date`. CAD itself is implicitly 1.0 and is not stored. Any
    from->to conversion at a date crosses through CAD.
    """

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("date", "currency", name="uq_fx_rates_date_currency"),
        CheckConstraint("cad_per_unit > 0", name="ck_fx_rates_cad_per_unit_positive"),
    )

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, index=True)
    currency = Column(String, nullable=False, index=True)  # uppercase ISO code
    cad_per_unit = Column(RATE, nullable=False)
    updated_at = Column(UTC_TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
