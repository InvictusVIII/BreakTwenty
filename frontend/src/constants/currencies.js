// Single source of truth for the currencies the app offers for user selection
// (primary currency, onboarding, manual cash, manual/asset account currency).
// CAD is the FX base (implicit 1.0).
//
// EVERY user-facing currency picker in the app pulls from `SELECTABLE_CURRENCIES`
// (or `CURRENCY_OPTIONS`) so they all show and process the exact same set — never
// hand-roll a per-component currency list. The set is the fiat currencies we always
// backfill historical FX for PLUS the two cryptos we always price; it mirrors the
// backend always-backfill set in `app/services/fx_history.py`
// (`SUPPORTED_CURRENCIES ∪ CRYPTO_CURRENCIES`), so every offered currency always has
// full history for accurate conversion. Do not add a currency here unless the backend
// backfills its history (e.g. AUD/JPY are intentionally excluded — no always-on FX).
export const SUPPORTED_CURRENCIES = ['CAD', 'USD', 'EUR', 'GBP', 'CHF', 'CZK'];
export const CRYPTO_CURRENCIES = ['BTC', 'ETH'];
export const SELECTABLE_CURRENCIES = [...SUPPORTED_CURRENCIES, ...CRYPTO_CURRENCIES];
// Ready-made `{ value, label }` options for the shared <Dropdown>.
export const CURRENCY_OPTIONS = SELECTABLE_CURRENCIES.map((c) => ({ value: c, label: c }));
// Physical cash is fiat-only — crypto lives in crypto wallets/exchanges (the Crypto &
// Wallets tile / crypto manual institution), never your physical wallet. The wallet Cash
// flows use this subset so BTC/ETH aren't offered as "cash on hand".
export const FIAT_CURRENCY_OPTIONS = SUPPORTED_CURRENCIES.map((c) => ({ value: c, label: c }));
