import { API } from '../config';
import providerCatalog from '../constants/providerCatalog.json';
import marketStripCatalog from '../constants/marketStripCatalog.generated.json';
import { SELECTABLE_CURRENCIES } from '../constants/currencies';
import {
  getSeedCategory,
  SEED_CATEGORIES_BY_KEY,
  SEED_CATEGORY_PARENTS,
  SEED_CATEGORY_TAXONOMY,
} from '../constants/seedCategoryTaxonomy';
import { setAppClockOverride } from '../utils/appClock';

const PROMO_ACTIVE_STORAGE_KEY = 'breaktwenty_promo_demo_active_v1';
const PROMO_STORE_STORAGE_KEY = 'breaktwenty_promo_demo_store_v1';
const PROMO_FETCH_WRAPPER_KEY = '__breaktwentyPromoDemoFetchWrapper';
const PROMO_ORIGINAL_FETCH_KEY = '__breaktwentyPromoDemoOriginalFetch';
const PROMO_FETCH_STATE_KEY = '__breaktwentyPromoDemoFetchState';
export const PROMO_DEMO_CHANGE_EVENT = 'breaktwenty:promo-demo-change';
const PROMO_STORE_VERSION = 7;
const PROMO_STORE_SEED_ID = 'promo-frozen-snapshot-2026-06-28';
export const PROMO_DEMO_NOW_ISO = '2026-06-28T16:00:00-04:00';
const PROMO_NOW_ISO = PROMO_DEMO_NOW_ISO;
const PROMO_TODAY = '2026-06-28';
const PROMO_CREATED_AT = '2026-01-02T12:00:00.000Z';
const PROMO_MARKET_DATA_PROVIDERS = [
  { key: 'fmp', label: 'FMP' },
  { key: 'polygon_massive', label: 'Polygon / Massive' },
  { key: 'twelve_data', label: 'Twelve Data' },
];

// Frozen historical observations for the June 28, 2026 Promo snapshot.
// June 28 was a Sunday, so traditional-market values use the latest official
// close (Friday, June 26). Index/FX paths are genuine June 26 hourly samples
// ending at the official FRED close; Bitcoin uses CoinGecko's June 28 UTC
// hourly range. These arrays are deliberately literal so the Promo chart never
// changes when a live provider revises or drops historical intraday access.
const PROMO_MARKET_TILE_VALUES = {
  sp500: {
    value: 7354.02,
    reference_value: 7357.49,
    change: -3.47,
    change_pct: -0.05,
    source: 'fred',
    points: [7312.74, 7361.98, 7374.04, 7361.52, 7375.14, 7355.99, 7349.41, 7337.81, 7354.02],
  },
  nasdaq: {
    value: 25297.62,
    reference_value: 25358.60,
    change: -60.98,
    change_pct: -0.24,
    source: 'fred',
    points: [25105.41, 25328.75, 25401.16, 25363.21, 25423.54, 25310.13, 25281.87, 25206.57, 25297.62],
  },
  dow: {
    value: 51876.11,
    reference_value: 51920.62,
    change: -44.51,
    change_pct: -0.09,
    source: 'fred',
    points: [51803.77, 51980.70, 52045.53, 51897.59, 51976.53, 51893.25, 51850.69, 51788.01, 51876.11],
  },
  usd_cad: {
    value: 1.4182,
    reference_value: 1.4191,
    change: -0.0009,
    change_pct: -0.06,
    source: 'fred',
    points: [
      1.4195, 1.41981, 1.42046, 1.4194, 1.41911, 1.4187, 1.41913, 1.41975,
      1.41857, 1.41848, 1.41798, 1.41863, 1.41773, 1.41731, 1.42001, 1.41839,
      1.41923, 1.4188, 1.4189, 1.4176, 1.419, 1.4182,
    ],
  },
  bitcoin: {
    value: 59494.61,
    reference_value: 59953.44,
    change: -458.84,
    change_pct: -0.77,
    source: 'coingecko',
    points: [
      59953.44, 60078.18, 60148.71, 60101.46, 60125.53, 60056.06, 59910.76,
      59929.77, 60141.84, 60309.98, 60197.89, 60097.37, 60269.23, 60216.16,
      60004.52, 59972.08, 59823.83, 59624.20, 59517.74, 59545.68, 59405.68,
      59582.52, 59350.75, 59009.78, 59494.61,
    ],
  },
};
const PROMO_MARKET_TILE_BY_ID = Object.fromEntries(
  marketStripCatalog.tiles.map((tile) => [tile.id, tile]),
);
const PROMO_DEFAULT_MARKET_WATCHLIST = marketStripCatalog.default_watchlist.map((id) => ({ id }));
const PROMO_MARKET_NEWS = {
  status: 'ok',
  source: 'official_public_feeds',
  partial: false,
  limit: 3,
  fetched_at: PROMO_NOW_ISO,
  articles: [
    {
      id: 'promo-sec-debt-rule',
      title: 'SEC Proposes Amendments to Exchange Act Rule 3a12-8 to Add European Union Debt Obligations',
      source: 'Securities and Exchange Commission',
      published_at: '2026-06-28T11:00:00-04:00',
      url: 'https://www.sec.gov/',
    },
    {
      id: 'promo-fed-warsh',
      title: 'Warsh, In Our Time',
      source: 'Federal Reserve',
      published_at: '2026-06-27T13:30:00-04:00',
      url: 'https://www.federalreserve.gov/',
    },
    {
      id: 'promo-eia-uranium',
      title: 'U.S. uranium production more than tripled and reached its highest level since 2017',
      source: 'U.S. Energy Information Administration',
      published_at: '2026-06-26T09:00:00-04:00',
      url: 'https://www.eia.gov/',
    },
  ],
};

const AUTH_REQUIRED_SYNC_DAYS_AGO = {
  td: 2,
  eqbank: 7,
};

// Frozen CAD value of one unit of each selectable currency at the Promo
// snapshot. June 28 was a Sunday, so fiat uses the latest official business-day
// observations (Bank of Canada / ECB, June 26). Crypto uses the June 28 daily
// Coinbase Exchange close, converted with that frozen USD/CAD observation.
//
// Keep this table independent from the market strip: the real app also uses its
// FX service for portfolio conversion and a separate quote provider for tiles.
const CAD_PER_UNIT = {
  CAD: 1,
  USD: 1.4186,
  EUR: 1.6172,
  GBP: 1.874,
  CHF: 1.7541,
  CZK: 0.06663232506387538,
  BTC: 84369.830586,
  ETH: 2226.365026,
};

// Preserve the established Promo screenshot headline after replacing the old
// placeholder FX values with the real frozen June 28 observations. This is a
// genuine dummy holding (and matching native account balance), not an override
// in the net-worth calculation: every normal Accounts/Holdings/Dashboard path
// sees the same position. Its CAD value is exactly $7,358.88115388, bringing the
// frozen totals back to $1,495,046.02 assets / $977,246.02 net worth.
const PROMO_SGOV_POSITION_VALUE_USD = 5187.425034456532;

const FX_HISTORY = {
  base: 'CAD',
  rates: {
    ...Object.fromEntries(
      SELECTABLE_CURRENCIES
        .filter((currency) => currency !== 'CAD')
        .map((currency) => [currency, { [PROMO_TODAY]: CAD_PER_UNIT[currency] }]),
    ),
  },
};

const CAT = {
  paycheck: getSeedCategory('paycheck'),
  dividend: getSeedCategory('dividend'),
  interest: getSeedCategory('interest'),
  groceries: getSeedCategory('groceries'),
  mortgage: getSeedCategory('mortgage'),
  gas: getSeedCategory('gas'),
  insurance: getSeedCategory('insurance'),
  buy: getSeedCategory('buy'),
  sell: getSeedCategory('sell'),
  withholdingTax: getSeedCategory('withheld'),
  interestCharged: getSeedCategory('interest_paid'),
};

const MANUAL_GROUPS = [
  { id: 330, name: 'Canada Life', type: 'manual', provider: 'manual_custom', category: 'bank_brokerage' },
  { id: 331, name: 'Real Estate', type: 'manual', provider: 'real_estate', category: 'real_estate' },
  { id: 332, name: 'Vehicles', type: 'manual', provider: 'vehicles', category: 'vehicles' },
  { id: 333, name: 'Valuables', type: 'manual', provider: 'valuables', category: 'valuables' },
  { id: 334, name: 'Private Investments', type: 'manual', provider: 'private_investments', category: 'private_investments' },
  { id: 335, name: 'Other Assets', type: 'manual', provider: 'other_assets', category: 'other_assets' },
  { id: 336, name: 'Cash', type: 'manual', provider: 'manual', category: null },
  { id: 337, name: 'Debt', type: 'manual', provider: 'debt', category: 'debt' },
];

const SYNCABLE_PROVIDER_ORDER = [
  'ibkr',
  'questrade',
  'coinbase',
  'wealthsimple',
  'wise',
  'rbc',
  'tangerine',
  'scotiabank',
  'amex',
  'td',
  'eqbank',
  'bmo',
  'cibc',
  'national',
  'moomoo',
];

const SYNCABLE_PROVIDER_SET = new Set(SYNCABLE_PROVIDER_ORDER);

const SYNCABLE_ACCOUNTS = {
  ibkr: [
    ['Portfolio Margin', 'margin', 'USD', false, 84200],
    ['RRSP', 'rrsp', 'CAD', false, 38250],
  ],
  questrade: [
    ['TFSA', 'tfsa', 'CAD', false, 48100],
    ['Margin', 'margin', 'CAD', false, 26800],
  ],
  coinbase: [
    ['Crypto Portfolio', 'crypto', 'BTC', false, 0.42],
  ],
  wealthsimple: [
    ['Managed TFSA', 'tfsa', 'CAD', false, 34450],
    ['Cash Account', 'cash', 'CAD', false, 6200],
  ],
  wise: [
    ['Multi-Currency CAD', 'chequing', 'CAD', false, 7800],
    ['Multi-Currency USD', 'chequing', 'USD', false, 3150],
  ],
  rbc: [
    ['Day to Day Banking', 'chequing', 'CAD', false, 12650],
    ['Avion Visa', 'credit_card', 'CAD', true, 1840],
  ],
  tangerine: [
    ['No-Fee Daily Chequing', 'chequing', 'CAD', false, 4250],
    ['High Interest Savings', 'savings', 'CAD', false, 18200],
  ],
  scotiabank: [
    ['Preferred Package', 'chequing', 'CAD', false, 5300],
    ['Passport Visa Infinite', 'credit_card', 'CAD', true, 2320],
    ['Mortgage', 'mortgage', 'CAD', true, 472000, { secured_asset_account_id: 401 }],
  ],
  amex: [
    ['Cobalt Card', 'credit_card', 'CAD', true, 960],
  ],
  td: [
    ['Every Day Chequing', 'chequing', 'CAD', false, 9200],
    ['ePremium Savings', 'savings', 'CAD', false, 15400],
    ['Line of Credit', 'line_of_credit', 'CAD', true, 6200],
  ],
  eqbank: [
    ['Savings Plus', 'savings', 'CAD', false, 27600],
    ['GIC Ladder', 'savings', 'CAD', false, 15000],
  ],
  bmo: [
    ['Performance Chequing', 'chequing', 'CAD', false, 7600],
    ['CashBack Mastercard', 'credit_card', 'CAD', true, 1180],
  ],
  cibc: [
    ['Smart Account', 'chequing', 'CAD', false, 6800],
    ['Student Loan', 'student_loan', 'CAD', true, 14200],
  ],
  national: [
    ['The Connected Account', 'chequing', 'CAD', false, 4900],
    ['All-In-One Line', 'line_of_credit', 'CAD', true, 8100],
  ],
  moomoo: [
    ['Universal Account', 'margin', 'USD', false, 22600 + PROMO_SGOV_POSITION_VALUE_USD],
  ],
};

const HOLDINGS_TEMPLATE = {
  ibkr: [
    ['VFV.TO', 'Vanguard S&P 500 Index ETF', 160, 12320, 10100, 77, 'CAD', 'ETF', 'etf'],
    ['AAPL', 'Apple Inc.', 36, 10400, 9400, 288.89, 'USD', 'Technology', 'equity'],
    ['CAD', 'Canadian Dollar Cash', 3200, 3200, 3200, 1, 'CAD', 'Cash', 'cash'],
    ['AAPL 31JUL26 240 C', 'AAPL 31JUL26 240 C', 2, 1260, 9.8, 6.3, 'USD', 'Options', 'option', { contract_multiplier: 100, daily_pnl: 185, change_pct: 4.7 }],
    ['SPY 31JUL26 620 P', 'SPY 31JUL26 620 P', -1, -740, -8.1, 7.4, 'USD', 'Options', 'option', { contract_multiplier: 100, daily_pnl: -92, change_pct: -2.4 }],
  ],
  questrade: [
    ['XEQT.TO', 'iShares Core Equity ETF Portfolio', 720, 22460, 19700, 31.19, 'CAD', 'ETF', 'etf'],
    ['RY.TO', 'Royal Bank of Canada', 88, 14520, 11200, 165, 'CAD', 'Financial Services', 'equity'],
    ['CAD', 'Canadian Dollar Cash', 4850, 4850, 4850, 1, 'CAD', 'Cash', 'cash'],
    ['RY.TO 24JUL26 170 C', 'RY.TO 24JUL26 170 C', -3, -615, -7.5, 2.05, 'CAD', 'Options', 'option', { contract_multiplier: 100, daily_pnl: 68, change_pct: 1.9 }],
  ],
  coinbase: [
    ['BTC', 'Bitcoin', 0.32, 0.32, 0.24, 1, 'BTC', 'Crypto', 'crypto'],
    ['ETH', 'Ethereum', 7.5, 7.5, 6.2, 1, 'ETH', 'Crypto', 'crypto'],
  ],
  wealthsimple: [
    ['VDY.TO', 'Vanguard FTSE Canadian High Dividend Yield ETF', 360, 16020, 12800, 44.5, 'CAD', 'ETF', 'etf'],
    ['BNS.TO', 'Bank of Nova Scotia', 110, 8580, 7100, 78, 'CAD', 'Financial Services', 'equity'],
    ['CAD', 'Canadian Dollar Cash', 2150, 2150, 2150, 1, 'CAD', 'Cash', 'cash'],
  ],
  moomoo: [
    ['NVDA', 'NVIDIA Corp.', 16, 12800, 9400, 800, 'USD', 'Technology', 'equity'],
    ['USD', 'US Dollar Cash', 1450, 1450, 1450, 1, 'USD', 'Cash', 'cash'],
    ['NVDA 31JUL26 900 P', 'NVDA 31JUL26 900 P', 1, 980, 12.2, 9.8, 'USD', 'Options', 'option', { contract_multiplier: 100, daily_pnl: -140, change_pct: -3.8 }],
    ['SGOV', 'iShares 0-3 Month Treasury Bond ETF', 51.25, PROMO_SGOV_POSITION_VALUE_USD, 5000, 101.21804945281038, 'USD', 'Fixed Income', 'etf'],
  ],
};

function syncedAtDaysAgo(daysAgo = 0) {
  const date = new Date(new Date(PROMO_NOW_ISO).getTime() - (Number(daysAgo) || 0) * 24 * 60 * 60 * 1000);
  return date.toISOString();
}

function providerSyncStatus(provider) {
  return Object.prototype.hasOwnProperty.call(AUTH_REQUIRED_SYNC_DAYS_AGO, provider) ? 'auth_required' : 'ok';
}

function providerLastSyncedAt(provider) {
  return syncedAtDaysAgo(AUTH_REQUIRED_SYNC_DAYS_AGO[provider] || 0);
}

const PROMO_NET_WORTH_HISTORY_POINTS = 1500;
const PROMO_NET_WORTH_HISTORY_END_MS = Date.UTC(2026, 5, 28);

function promoNetWorthShapeBase(t) {
  const pts = [[0, 0.94], [0.18, 0.82], [0.45, 0.54], [0.66, 0.65], [0.82, 0.78], [1, 1]];
  for (let k = 1; k < pts.length; k += 1) {
    if (t <= pts[k][0]) {
      const t0 = pts[k - 1][0];
      const v0 = pts[k - 1][1];
      const t1 = pts[k][0];
      const v1 = pts[k][1];
      const f = (t - t0) / (t1 - t0);
      return v0 + (v1 - v0) * (f * f * (3 - 2 * f));
    }
  }
  return 1;
}

const PROMO_NET_WORTH_WALK = (() => {
  let a = 0x9e3779b9;
  const rng = () => {
    a = (a + 0x6d2b79f5) | 0;
    let x = Math.imul(a ^ (a >>> 15), 1 | a);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
  const walk = [];
  let slow = 0;
  let fast = 0;
  for (let i = 0; i < PROMO_NET_WORTH_HISTORY_POINTS; i += 1) {
    slow = slow * 0.99 + (rng() - 0.5) * 0.04;
    fast = fast * 0.80 + (rng() - 0.5) * 0.045;
    walk.push((slow + fast) * Math.min(1, (1 - i / (PROMO_NET_WORTH_HISTORY_POINTS - 1)) * 60));
  }
  return walk;
})();

function providerInstitution(provider, index) {
  const metadata = providerCatalog?.[provider] || {};
  return {
    id: 200 + index,
    name: metadata.displayName || provider,
    type: metadata.institutionType || 'api',
    provider,
    category: metadata.category || null,
    hidden: false,
    enabled: true,
    has_logo: false,
    sync_status: providerSyncStatus(provider),
    added_at: PROMO_CREATED_AT,
  };
}

function account({
  id,
  institutionId,
  institutionName,
  provider,
  name,
  accountType,
  currency,
  isLiability = false,
  balance,
  extra = {},
}) {
  return {
    id,
    institution_id: institutionId,
    institution: institutionName,
    provider,
    name,
    account_type: accountType,
    currency,
    is_liability: isLiability,
    hidden: false,
    balance,
    last_synced: extra.last_synced === undefined ? providerLastSyncedAt(provider) : extra.last_synced,
    added_at: extra.added_at || PROMO_CREATED_AT,
    connected_at: extra.connected_at === undefined ? PROMO_CREATED_AT : extra.connected_at,
    is_imported: false,
    has_transactions: Boolean(extra.has_transactions),
    cash_opening: extra.cash_opening || null,
    opening_balance: extra.opening_balance ?? null,
    opening_balance_date: extra.opening_balance_date || null,
    purchase: extra.purchase || null,
    secured_asset_account_id: extra.secured_asset_account_id || null,
  };
}

function toCad(amount, currency) {
  const rate = CAD_PER_UNIT[String(currency || 'CAD').toUpperCase()] || 1;
  return Number(amount || 0) * rate;
}

function fromCad(amount, currency) {
  const rate = CAD_PER_UNIT[String(currency || 'CAD').toUpperCase()] || 1;
  return Number(amount || 0) / rate;
}

function promoPrimaryCurrency(store) {
  const requested = String(store?.settings?.primary_currency || 'CAD').trim().toUpperCase();
  return SELECTABLE_CURRENCIES.includes(requested) ? requested : 'CAD';
}

function convertPromoCurrency(amount, fromCurrency, toCurrency) {
  return fromCad(toCad(amount, fromCurrency), toCurrency);
}

function roundPromoAmount(amount) {
  return Math.round((Number(amount) + Number.EPSILON) * 100000000) / 100000000;
}

function getFxRates(store) {
  const base = promoPrimaryCurrency(store);
  const cadPerBase = CAD_PER_UNIT[base];
  return {
    base,
    cached_at: PROMO_NOW_ISO,
    rates: Object.fromEntries(
      SELECTABLE_CURRENCIES.map((currency) => [
        currency,
        cadPerBase / CAD_PER_UNIT[currency],
      ]),
    ),
  };
}

function getFxHistory(store) {
  return {
    ...FX_HISTORY,
    primary: promoPrimaryCurrency(store),
  };
}

function latestBalance(account, store) {
  const points = store.valueHistoryByAccountId[String(account.id)] || [];
  if (!points.length) return Number(account.balance || 0);
  return Number(points[points.length - 1].value || 0);
}

function syncAccountBalance(account, store) {
  account.balance = latestBalance(account, store);
  return account;
}

function datedPoint(store, accountId, value, date, flags = {}) {
  return {
    id: store.nextHistoryPointId++,
    value: Number(value || 0),
    date: date || PROMO_NOW_ISO,
    is_purchase: Boolean(flags.is_purchase),
    is_opening: Boolean(flags.is_opening),
  };
}

function sortHistory(points) {
  return [...points].sort((left, right) => String(left.date || '').localeCompare(String(right.date || '')));
}

function seedHistory(store, accountId, value, date, flags) {
  const key = String(accountId);
  store.valueHistoryByAccountId[key] = sortHistory([
    ...(store.valueHistoryByAccountId[key] || []),
    datedPoint(store, accountId, value, date, flags),
  ]);
}

function createInitialStore() {
  const institutions = SYNCABLE_PROVIDER_ORDER.map(providerInstitution);
  institutions.push(...MANUAL_GROUPS.map((institution) => ({
    ...institution,
    hidden: false,
    enabled: true,
    has_logo: false,
    sync_status: 'ok',
    added_at: PROMO_CREATED_AT,
  })));

  const accounts = [];
  let nextAccountId = 210;
  SYNCABLE_PROVIDER_ORDER.forEach((provider, index) => {
    const institution = institutions.find((inst) => inst.provider === provider && inst.id === 200 + index);
    (SYNCABLE_ACCOUNTS[provider] || []).forEach((entry) => {
      const [name, accountType, currency, isLiability, balance, extra = {}] = entry;
      accounts.push(account({
        id: nextAccountId++,
        institutionId: institution.id,
        institutionName: institution.name,
        provider,
        name,
        accountType,
        currency,
        isLiability,
        balance,
        extra: { has_transactions: true, last_synced: providerLastSyncedAt(provider), ...extra },
      }));
    });
  });

  accounts.push(
    account({
      id: 390,
      institutionId: 330,
      institutionName: 'Canada Life',
      provider: 'manual_custom',
      name: 'Canada Life Policy',
      accountType: 'insurance',
      currency: 'CAD',
      balance: 42000,
      extra: {
        last_synced: null,
        connected_at: null,
        opening_balance: 31000,
        opening_balance_date: '2020-03-01T12:00:00.000Z',
      },
    }),
    account({
      id: 401,
      institutionId: 331,
      institutionName: 'Real Estate',
      provider: 'real_estate',
      name: 'Primary Residence',
      accountType: 'primary_residence',
      currency: 'CAD',
      balance: 790000,
      extra: {
        last_synced: null,
        connected_at: null,
        purchase: { value: 620000, date: '2021-05-01T12:00:00.000Z' },
      },
    }),
    account({ id: 402, institutionId: 332, institutionName: 'Vehicles', provider: 'vehicles', name: '2023 Model Y', accountType: 'car', currency: 'CAD', balance: 52000, extra: { last_synced: null, connected_at: null, purchase: { value: 68000, date: '2023-08-20T12:00:00.000Z' } } }),
    account({ id: 403, institutionId: 333, institutionName: 'Valuables', provider: 'valuables', name: 'Heirloom Watch', accountType: 'jewelry', currency: 'CAD', balance: 18500, extra: { last_synced: null, connected_at: null, purchase: { value: 12000, date: '2018-06-12T12:00:00.000Z' } } }),
    account({ id: 404, institutionId: 334, institutionName: 'Private Investments', provider: 'private_investments', name: 'Design Studio Equity', accountType: 'business', currency: 'CAD', balance: 95000, extra: { last_synced: null, connected_at: null, purchase: { value: 50000, date: '2022-01-10T12:00:00.000Z' } } }),
    account({ id: 405, institutionId: 335, institutionName: 'Other Assets', provider: 'other_assets', name: 'Equipment Deposit', accountType: 'other_asset', currency: 'CAD', balance: 7200, extra: { last_synced: null, connected_at: null, purchase: { value: 7200, date: '2025-09-01T12:00:00.000Z' } } }),
    account({ id: 406, institutionId: 336, institutionName: 'Cash', provider: 'manual', name: 'Cash (CAD)', accountType: 'cash', currency: 'CAD', balance: 1800, extra: { last_synced: null, connected_at: null, has_transactions: true, cash_opening: { amount: 1500, date: '2026-01-01T12:00:00.000Z' } } }),
    account({ id: 407, institutionId: 336, institutionName: 'Cash', provider: 'manual', name: 'Cash (USD)', accountType: 'cash', currency: 'USD', balance: 900, extra: { last_synced: null, connected_at: null, has_transactions: true, cash_opening: { amount: 900, date: '2026-01-01T12:00:00.000Z' } } }),
    account({ id: 408, institutionId: 337, institutionName: 'Debt', provider: 'debt', name: 'Family Loan', accountType: 'personal_loan', currency: 'CAD', isLiability: true, balance: 11000, extra: { last_synced: null, connected_at: null, purchase: { value: 15000, date: '2024-04-01T12:00:00.000Z' } } }),
  );

  const scotiaMortgage = accounts.find((acct) => acct.provider === 'scotiabank' && acct.account_type === 'mortgage');
  if (scotiaMortgage) scotiaMortgage.secured_asset_account_id = 401;

  const store = {
    version: PROMO_STORE_VERSION,
    seed_id: PROMO_STORE_SEED_ID,
    nextInstitutionId: 500,
    nextAccountId: 600,
    nextTransactionId: 7000,
    nextHistoryPointId: 8000,
    settings: {
      primary_currency: 'CAD',
      user_timezone: 'America/Toronto',
      user_timezone_configured: true,
      user_time_format: '12h',
    },
    marketData: {
      provider: 'fmp',
      maskedKey: null,
      configRevision: '',
    },
    marketWatchlist: PROMO_DEFAULT_MARKET_WATCHLIST,
    institutions,
    accounts,
    holdingsByAccountId: {},
    valueHistoryByAccountId: {},
    transactions: [],
  };

  accounts
    .filter((acct) => acct.provider === 'manual_custom' || ['real_estate', 'vehicles', 'valuables', 'private_investments', 'other_assets', 'debt', 'manual'].includes(acct.provider))
    .forEach((acct) => {
      if (acct.purchase) seedHistory(store, acct.id, acct.purchase.value, acct.purchase.date, { is_purchase: true });
      if (acct.opening_balance != null) seedHistory(store, acct.id, acct.opening_balance, acct.opening_balance_date || PROMO_NOW_ISO, { is_opening: true });
      seedHistory(store, acct.id, acct.balance, PROMO_NOW_ISO);
    });

  SYNCABLE_PROVIDER_ORDER.forEach((provider) => {
    const firstInvestingAccount = accounts.find((acct) => acct.provider === provider && ['margin', 'tfsa', 'rrsp', 'crypto'].includes(acct.account_type));
    const template = HOLDINGS_TEMPLATE[provider];
    if (!firstInvestingAccount || !template) return;
    store.holdingsByAccountId[String(firstInvestingAccount.id)] = template.map((row, rowIndex) => {
      const [symbol, name, quantity, marketValue, averageCost, lastPrice, currency, sector, instrumentKind, extra = {}] = row;
      return {
        id: firstInvestingAccount.id * 100 + rowIndex,
        symbol,
        name,
        quantity,
        market_value: marketValue,
        average_cost: averageCost,
        last_price: lastPrice,
        contract_multiplier: extra.contract_multiplier ?? null,
        change_pct: rowIndex % 2 === 0 ? 0.8 : -0.3,
        daily_pnl: rowIndex % 2 === 0 ? 84 : -22,
        currency,
        sector,
        instrument_kind: instrumentKind,
        ...extra,
      };
    });
  });

  seedTransactions(store);
  return store;
}

function seedTransactions(store) {
  const accountByProvider = (provider, type) => store.accounts.find((acct) => acct.provider === provider && (!type || acct.account_type === type));
  const txs = [
    [accountByProvider('td', 'chequing'), '2026-06-28', 'Payroll Deposit', 3150, CAT.paycheck, { type: 'deposit' }],
    [accountByProvider('td', 'chequing'), '2026-06-14', 'Payroll Deposit', 3150, CAT.paycheck, { type: 'deposit' }],
    [accountByProvider('scotiabank', 'mortgage'), '2026-06-13', 'Mortgage Payment', 2350, CAT.mortgage, { type: 'manual' }],
    [accountByProvider('amex', 'credit_card'), '2026-06-12', 'Farm Boy', -94.21, CAT.groceries],
    [accountByProvider('rbc', 'credit_card'), '2026-06-11', 'Loblaws', -188.43, CAT.groceries],
    [accountByProvider('td', 'chequing'), '2026-06-10', 'Metro', -126.77, CAT.groceries],
    [accountByProvider('amex', 'credit_card'), '2026-06-09', 'Uber Eats', -54.89, CAT.groceries],
    [accountByProvider('rbc', 'credit_card'), '2026-06-08', 'Costco Grocery', -243.62, CAT.groceries],
    [accountByProvider('amex', 'credit_card'), '2026-06-07', 'Starbucks', -31.48, CAT.groceries],
    [accountByProvider('td', 'chequing'), '2026-06-06', 'Whole Foods', -160.60, CAT.groceries],
    [accountByProvider('rbc', 'credit_card'), '2026-06-05', 'Tim Hortons', -18.25, CAT.groceries],
    [accountByProvider('amex', 'credit_card'), '2026-06-04', 'DoorDash', -67.75, CAT.groceries],
    [accountByProvider('td', 'chequing'), '2026-06-03', 'Save-On-Foods', -44.00, CAT.groceries],
    [accountByProvider('rbc', 'credit_card'), '2026-06-11', 'Petro-Canada', -71.4, CAT.gas],
    [accountByProvider('questrade', 'tfsa'), '2026-06-10', 'VFV.TO Dividend', 420, CAT.dividend, { type: 'dividend', symbol: 'VFV.TO' }],
    [accountByProvider('wealthsimple', 'tfsa'), '2026-06-09', 'VDY.TO Distribution', 680, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO', quantity: 360 }],
    [accountByProvider('questrade', 'margin'), '2026-06-08', 'RY.TO Dividend', 704, CAT.dividend, { type: 'dividend', symbol: 'RY.TO', quantity: 88 }],
    [accountByProvider('wealthsimple', 'tfsa'), '2026-06-08', 'BNS.TO Dividend', 126, CAT.dividend, { type: 'dividend', symbol: 'BNS.TO', quantity: 110 }],
    [accountByProvider('questrade', 'tfsa'), '2026-06-07', 'XEQT.TO Distribution', 310, CAT.dividend, { type: 'dividend', symbol: 'XEQT.TO', quantity: 720 }],
    [accountByProvider('ibkr', 'margin'), '2026-06-06', 'AAPL Dividend', 95, CAT.dividend, { type: 'dividend', symbol: 'AAPL', currency: 'USD', amount_primary: 130.32, quantity: 36 }],
    [accountByProvider('questrade', 'margin'), '2026-06-05', 'HYLD.TO Distribution', 559.68, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 900 }],
    [accountByProvider('ibkr', 'margin'), '2026-06-06', 'AAPL Foreign Withholding Tax', -14.25, CAT.withholdingTax, { type: 'withholding_tax', symbol: 'AAPL', currency: 'USD', amount_primary: -19.55 }],
    [accountByProvider('wealthsimple', 'cash'), '2026-06-02', 'Cash Interest', 18.42, CAT.interest, { type: 'interest' }],
    [accountByProvider('eqbank', 'savings'), '2026-06-01', 'Savings Interest', 64.10, CAT.interest, { type: 'interest' }],
    [accountByProvider('td', 'line_of_credit'), '2026-06-01', 'Interest Charged', -38.50, CAT.interestCharged, { type: 'interest' }],
    [accountByProvider('moomoo', 'margin'), '2026-06-09', 'BUY 4 NVDA', -3200, CAT.buy, { type: 'buy', symbol: 'NVDA', currency: 'USD', amount_primary: -4389.76 }],
    [accountByProvider('manual_custom'), '2026-06-07', 'Policy Premium', -185, CAT.insurance],
    [accountByProvider('questrade', 'tfsa'), '2026-05-08', 'VFV.TO Dividend', 405, CAT.dividend, { type: 'dividend', symbol: 'VFV.TO' }],
    [accountByProvider('wealthsimple', 'tfsa'), '2026-05-08', 'VDY.TO Distribution', 652, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO' }],
    [accountByProvider('eqbank', 'savings'), '2026-05-01', 'Savings Interest', 61.88, CAT.interest, { type: 'interest' }],
    [accountByProvider('wealthsimple', 'cash'), '2026-05-01', 'Cash Interest', 17.96, CAT.interest, { type: 'interest' }],
    [accountByProvider('questrade', 'tfsa'), '2026-04-08', 'XEQT.TO Distribution', 296, CAT.dividend, { type: 'dividend', symbol: 'XEQT.TO' }],
    [accountByProvider('questrade', 'margin'), '2026-04-08', 'RY.TO Dividend', 690, CAT.dividend, { type: 'dividend', symbol: 'RY.TO' }],
    [accountByProvider('ibkr', 'margin'), '2026-04-06', 'AAPL Dividend', 92, CAT.dividend, { type: 'dividend', symbol: 'AAPL', currency: 'USD', amount_primary: 126.21 }],
    [accountByProvider('ibkr', 'margin'), '2026-04-06', 'AAPL Foreign Withholding Tax', -13.80, CAT.withholdingTax, { type: 'withholding_tax', symbol: 'AAPL', currency: 'USD', amount_primary: -18.93 }],
    [accountByProvider('eqbank', 'savings'), '2026-04-01', 'Savings Interest', 59.74, CAT.interest, { type: 'interest' }],
    [accountByProvider('wealthsimple', 'tfsa'), '2026-03-09', 'BNS.TO Dividend', 118, CAT.dividend, { type: 'dividend', symbol: 'BNS.TO' }],
    [accountByProvider('wealthsimple', 'tfsa'), '2026-03-09', 'VDY.TO Distribution', 636, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO' }],
    [accountByProvider('wealthsimple', 'cash'), '2026-03-02', 'Cash Interest', 16.84, CAT.interest, { type: 'interest' }],
    [accountByProvider('td', 'line_of_credit'), '2026-03-01', 'Interest Charged', -36.20, CAT.interestCharged, { type: 'interest' }],
    [accountByProvider('questrade', 'tfsa'), '2026-02-09', 'VFV.TO Dividend', 390, CAT.dividend, { type: 'dividend', symbol: 'VFV.TO' }],
    [accountByProvider('questrade', 'margin'), '2026-02-09', 'HYLD.TO Distribution', 655, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO' }],
    [accountByProvider('eqbank', 'savings'), '2026-02-02', 'Savings Interest', 57.35, CAT.interest, { type: 'interest' }],
    [accountByProvider('wealthsimple', 'tfsa'), '2026-01-09', 'VDY.TO Distribution', 610, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO' }],
    [accountByProvider('questrade', 'margin'), '2026-01-09', 'RY.TO Dividend', 664, CAT.dividend, { type: 'dividend', symbol: 'RY.TO' }],
    [accountByProvider('ibkr', 'margin'), '2026-01-06', 'AAPL Dividend', 89, CAT.dividend, { type: 'dividend', symbol: 'AAPL', currency: 'USD', amount_primary: 122.09 }],
    [accountByProvider('ibkr', 'margin'), '2026-01-06', 'AAPL Foreign Withholding Tax', -13.35, CAT.withholdingTax, { type: 'withholding_tax', symbol: 'AAPL', currency: 'USD', amount_primary: -18.31 }],
    [accountByProvider('wealthsimple', 'cash'), '2026-01-02', 'Cash Interest', 15.92, CAT.interest, { type: 'interest' }],
    [accountByProvider('questrade', 'tfsa'), '2025-12-09', 'XEQT.TO Distribution', 281, CAT.dividend, { type: 'dividend', symbol: 'XEQT.TO' }],
    [accountByProvider('wealthsimple', 'tfsa'), '2025-12-09', 'BNS.TO Dividend', 112, CAT.dividend, { type: 'dividend', symbol: 'BNS.TO' }],
    [accountByProvider('eqbank', 'savings'), '2025-12-01', 'Savings Interest', 53.42, CAT.interest, { type: 'interest' }],
    [accountByProvider('questrade', 'margin'), '2025-11-10', 'RY.TO Dividend', 642, CAT.dividend, { type: 'dividend', symbol: 'RY.TO' }],
    [accountByProvider('wealthsimple', 'cash'), '2025-11-03', 'Cash Interest', 14.80, CAT.interest, { type: 'interest' }],
    [accountByProvider('wealthsimple', 'tfsa'), '2025-10-09', 'VDY.TO Distribution', 588, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO' }],
    [accountByProvider('ibkr', 'margin'), '2025-10-06', 'AAPL Dividend', 86, CAT.dividend, { type: 'dividend', symbol: 'AAPL', currency: 'USD', amount_primary: 117.98 }],
  ];
  store.transactions = txs.filter(([acct]) => acct).map(([acct, date, description, amount, category, opts = {}], index) => ({
    id: 7100 + index,
    account_id: acct.id,
    account_name: acct.name,
    account_type: acct.account_type,
    institution_name: acct.institution,
    institution_provider: acct.provider,
    date,
    type: opts.type || (amount >= 0 ? 'deposit' : 'debit'),
    symbol: opts.symbol || null,
    description,
    user_description: null,
    display_description: description,
    user_notes: null,
    manual_kind: null,
    amount,
    currency: opts.currency || acct.currency,
    amount_primary: opts.amount_primary ?? toCad(amount, opts.currency || acct.currency),
    quantity: opts.quantity || null,
    price: opts.price || null,
    commission: opts.commission || null,
    category_source: 'auto',
    category,
  }));
}

function normalizeStore(raw) {
  if (
    !raw
    || raw.version !== PROMO_STORE_VERSION
    || raw.seed_id !== PROMO_STORE_SEED_ID
    || !Array.isArray(raw.institutions)
    || !Array.isArray(raw.accounts)
  ) {
    return createInitialStore();
  }
  const visibleInstitutionIds = new Set(
    raw.institutions
      .filter((inst) => inst && inst.enabled !== false && !inst.hidden)
      .map((inst) => inst.id),
  );
  const hasVisibleAccount = raw.accounts.some((acct) => (
    acct && !acct.hidden && visibleInstitutionIds.has(acct.institution_id)
  ));
  if (visibleInstitutionIds.size === 0 || !hasVisibleAccount) {
    return createInitialStore();
  }
  const defaults = createInitialStore();
  return {
    ...defaults,
    ...raw,
    settings: { ...defaults.settings, ...(raw.settings || {}) },
    marketData: { ...defaults.marketData, ...(raw.marketData || {}) },
    marketWatchlist: Array.isArray(raw.marketWatchlist) && raw.marketWatchlist.length
      ? raw.marketWatchlist
      : defaults.marketWatchlist,
    institutions: raw.institutions,
    accounts: raw.accounts,
    holdingsByAccountId: raw.holdingsByAccountId || {},
    valueHistoryByAccountId: raw.valueHistoryByAccountId || {},
    transactions: Array.isArray(raw.transactions)
      ? raw.transactions.map((transaction) => ({
        ...transaction,
        category: SEED_CATEGORIES_BY_KEY[transaction.category?.seed_key] || transaction.category,
      }))
      : defaults.transactions,
  };
}

function readStore() {
  if (typeof window === 'undefined') return createInitialStore();
  try {
    return normalizeStore(JSON.parse(window.localStorage.getItem(PROMO_STORE_STORAGE_KEY) || 'null'));
  } catch (_) {
    return createInitialStore();
  }
}

function writeStore(store) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(PROMO_STORE_STORAGE_KEY, JSON.stringify(store));
}

function mutateStore(mutator) {
  const store = readStore();
  const result = mutator(store);
  writeStore(store);
  return result;
}

export function isPromoDemoActive() {
  if (!import.meta.env.DEV) return false;
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(PROMO_ACTIVE_STORAGE_KEY) === '1';
  } catch (_) {
    return false;
  }
}

export function setPromoDemoActive(on) {
  if (!import.meta.env.DEV) return;
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(PROMO_ACTIVE_STORAGE_KEY, on ? '1' : '0');
    setAppClockOverride(on ? PROMO_NOW_ISO : null);
    if (on) writeStore(readStore());
    window.dispatchEvent(new CustomEvent(PROMO_DEMO_CHANGE_EVENT, {
      detail: { active: Boolean(on) },
    }));
  } catch (_) {
    // Keep promo optional when storage is unavailable.
  }
}

function visibleInstitutions(store) {
  return store.institutions.filter((inst) => inst.enabled !== false && !inst.hidden);
}

function visibleAccounts(store) {
  const visibleInstitutionIds = new Set(visibleInstitutions(store).map((inst) => inst.id));
  return store.accounts
    .filter((acct) => visibleInstitutionIds.has(acct.institution_id) && !acct.hidden)
    .map((acct) => {
      const row = syncAccountBalance({ ...acct }, store);
      if (SYNCABLE_PROVIDER_SET.has(row.provider)) {
        row.last_synced = providerLastSyncedAt(row.provider);
      }
      return row;
    });
}

function institutionPayload(institution, includeHidden = false) {
  return {
    id: institution.id,
    name: institution.name,
    type: institution.type,
    provider: institution.provider,
    category: institution.category || null,
    sync_status: institution.sync_status || 'ok',
    hidden: includeHidden ? Boolean(institution.hidden) : undefined,
    has_logo: Boolean(institution.has_logo),
    added_at: institution.added_at || PROMO_CREATED_AT,
  };
}

function scopeInstitutionPayloads(store) {
  return store.institutions
    .filter((inst) => inst.enabled !== false)
    .map((institution) => {
      const accounts = store.accounts
        .filter((acct) => acct.institution_id === institution.id)
        .map((acct) => ({
          ...syncAccountBalance({ ...acct }, store),
          institution: institution.name,
          institution_id: institution.id,
          provider: institution.provider,
          hidden: Boolean(acct.hidden),
        }));
      return {
        ...institutionPayload(institution, true),
        key: `institution-${institution.id}`,
        accounts,
        accountIds: accounts.map((account) => account.id),
      };
    });
}

function getNetWorth(store) {
  let totalAssetsCad = 0;
  let totalLiabilitiesCad = 0;
  visibleAccounts(store).forEach((acct) => {
    const cad = toCad(acct.balance, acct.currency);
    if (acct.is_liability) totalLiabilitiesCad += cad;
    else totalAssetsCad += cad;
  });
  const currency = promoPrimaryCurrency(store);
  const totalAssets = fromCad(totalAssetsCad, currency);
  const totalLiabilities = fromCad(totalLiabilitiesCad, currency);
  const current = {
    total_assets: roundPromoAmount(totalAssets),
    total_liabilities: roundPromoAmount(totalLiabilities),
    net_worth: roundPromoAmount(totalAssets - totalLiabilities),
    currency,
    date: PROMO_TODAY,
  };
  return {
    current,
    history: Array.from({ length: PROMO_NET_WORTH_HISTORY_POINTS }, (_, index) => {
      const t = index / (PROMO_NET_WORTH_HISTORY_POINTS - 1);
      const date = new Date(PROMO_NET_WORTH_HISTORY_END_MS - (PROMO_NET_WORTH_HISTORY_POINTS - 1 - index) * 86400000);
      const netWorth = index === PROMO_NET_WORTH_HISTORY_POINTS - 1
        ? current.net_worth
        : roundPromoAmount(current.net_worth * promoNetWorthShapeBase(t) * (1 + PROMO_NET_WORTH_WALK[index]));
      const liabilities = index === PROMO_NET_WORTH_HISTORY_POINTS - 1
        ? current.total_liabilities
        : roundPromoAmount(current.total_liabilities * (1.05 - t * 0.06 + Math.sin(t * Math.PI * 5) * 0.015));
      const assets = roundPromoAmount(netWorth + liabilities);
      return {
        date: index === PROMO_NET_WORTH_HISTORY_POINTS - 1 ? PROMO_TODAY : date.toISOString().slice(0, 10),
        total_assets: assets,
        total_liabilities: liabilities,
        net_worth: netWorth,
      };
    }),
  };
}

function getBalanceHistory(store) {
  const out = {};
  store.accounts.forEach((acct) => {
    const values = {};
    const finalBalance = latestBalance(acct, store);
    for (let i = 0; i < PROMO_NET_WORTH_HISTORY_POINTS; i += 1) {
      const t = i / (PROMO_NET_WORTH_HISTORY_POINTS - 1);
      const date = new Date(PROMO_NET_WORTH_HISTORY_END_MS - (PROMO_NET_WORTH_HISTORY_POINTS - 1 - i) * 86400000);
      const value = i === PROMO_NET_WORTH_HISTORY_POINTS - 1
        ? finalBalance
        : Math.round(finalBalance * promoNetWorthShapeBase(t) * (1 + PROMO_NET_WORTH_WALK[i]) * 100) / 100;
      values[i === PROMO_NET_WORTH_HISTORY_POINTS - 1 ? PROMO_TODAY : date.toISOString().slice(0, 10)] = value;
    }
    out[acct.id] = values;
  });
  return out;
}

function getImportStatus(store) {
  return {
    institutions: visibleInstitutions(store)
      .filter((inst) => !['manual_custom', 'real_estate', 'vehicles', 'valuables', 'private_investments', 'other_assets', 'manual', 'debt'].includes(inst.provider))
      .map((inst) => ({
        institution_id: inst.id,
        institution: inst.name,
        provider: inst.provider,
        status: 'incremental',
        status_label: 'Sync complete',
        history_status: 'complete',
        history_status_label: 'Backfill complete',
        current_fetch_status: null,
        current_fetch_label: null,
        current_fetch_mode: null,
        transaction_import_job_status: null,
        transaction_import_job_id: null,
        transaction_import_job_last_progress_at: null,
        transaction_import_job_last_progress_label: null,
        transaction_import_job_stale_recovery_count: 0,
        accounts: store.accounts.filter((acct) => acct.institution_id === inst.id).map((acct) => ({
          account_id: acct.id,
          account_name: acct.name,
          account_type: acct.account_type,
          is_liability: acct.is_liability,
          status: 'incremental',
          status_label: 'Sync complete',
          history_status: 'complete',
          history_status_label: 'Backfill complete',
          provider: inst.provider,
          account_external_id: `promo_${acct.id}`,
          backfill_start_date: '2022-06-15',
          backfill_end_date: PROMO_TODAY,
          completed_start_date: '2022-06-15',
          completed_end_date: PROMO_TODAY,
          data_start_date: '2022-06-15',
          data_end_date: PROMO_TODAY,
          completed_windows: 24,
          total_windows: 24,
          pending_windows: 0,
          failed_windows: 0,
          latest_incremental_start_date: '2026-06-01',
          latest_incremental_end_date: PROMO_TODAY,
          last_successful_fetch_at: providerLastSyncedAt(inst.provider),
          last_error: null,
          transaction_count: 160,
        })),
      })),
    generated_at: PROMO_NOW_ISO,
  };
}

function getCashFlow(store) {
  const currency = promoPrimaryCurrency(store);
  const inPrimary = (amount) => roundPromoAmount(fromCad(amount, currency));
  const income = inPrimary(9230);
  const expense = inPrimary(4765);
  return {
    period: { start: '2026-06-01', end: '2026-06-30', currency },
    totals: { income, expense, net: inPrimary(9230 - 4765) },
    expense_breakdown: [
      cashFlowRow(CAT.mortgage, inPrimary(2350), 1, 49.3, true),
      cashFlowRow(CAT.groceries, inPrimary(1030), 10, 21.6, true),
      cashFlowRow(CAT.insurance, inPrimary(520), 3, 10.9, true),
      cashFlowRow(CAT.gas, inPrimary(410), 5, 8.6, true),
    ],
    income_breakdown: [
      cashFlowRow(CAT.paycheck, inPrimary(6300), 2, 68.3),
      cashFlowRow(CAT.dividend, inPrimary(2930), 8, 31.7),
    ],
    needs_review: {
      category_id: null,
      transaction_count: 0,
    },
    investment_activity: {
      buys: inPrimary(-4389.76),
      sells: inPrimary(1200),
      dividends: inPrimary(0),
      interest: inPrimary(0),
      withholding_tax: inPrimary(0),
      expired: inPrimary(0),
      transfers: inPrimary(0),
      deposits: inPrimary(0),
      withdrawals: inPrimary(0),
      cc_payments: inPrimary(1320),
      loan_payments: inPrimary(2350),
      loan_advances: inPrimary(0),
      realized_pnl: inPrimary(0),
      breakdown: [],
    },
    trend_12_months: Array.from({ length: 12 }, (_, index) => ({
      month: new Date(Date.UTC(2025, 6 + index, 1)).toISOString().slice(0, 7),
      income: inPrimary(7100 + index * 175),
      expense: inPrimary(4300 + (index % 3) * 220),
      net: inPrimary(2800 + index * 120),
    })),
    recurring: [
      recurring(1, 'Mortgage', CAT.mortgage, -2350, 'Mortgage', 'Scotiabank', '2026-06-13', '2026-07-13', currency),
      recurring(2, 'Insurance', CAT.insurance, -185, 'Canada Life Policy', 'Canada Life', '2026-06-07', '2026-07-07', currency),
    ],
    previous_totals: {
      start: '2026-05-01',
      end: '2026-05-31',
      income: inPrimary(9080),
      expense: inPrimary(4520),
      net: inPrimary(4560),
    },
  };
}

function cashFlowRow(cat, amount, count, pct, withParent = false) {
  const parent = withParent ? SEED_CATEGORY_PARENTS.find((entry) => entry.id === cat.parent_id) : null;
  return {
    category_id: cat.id,
    name: cat.name,
    icon: cat.icon,
    icon_set: cat.icon_set,
    color_dark: cat.color_dark,
    color_light: cat.color_light,
    classification: cat.classification,
    seed_key: cat.seed_key,
    parent_id: cat.parent_id,
    parent_name: parent?.name || null,
    parent_color_dark: parent?.color_dark || null,
    parent_color_light: parent?.color_light || null,
    parent_icon: parent?.icon || null,
    parent_icon_set: parent?.icon_set || null,
    amount,
    percent_of_total: pct,
    transaction_count: count,
  };
}

function recurring(id, name, cat, amount, account, institution, lastDate, nextDate, primaryCurrency = 'CAD') {
  const amountPrimary = roundPromoAmount(convertPromoCurrency(amount, 'CAD', primaryCurrency));
  return {
    id,
    name,
    direction: 'outflow',
    category: {
      id: cat.id,
      name: cat.name,
      icon: cat.icon,
      icon_set: cat.icon_set,
      color_dark: cat.color_dark,
      color_light: cat.color_light,
    },
    cadence: 'monthly',
    interval_days: 30,
    amount,
    amount_min: amount,
    amount_max: amount,
    currency: 'CAD',
    amount_primary: amountPrimary,
    last_amount: amount,
    last_amount_primary: amountPrimary,
    account_name: account,
    institution_name: institution,
    last_seen_date: lastDate,
    next_expected_date: nextDate,
    occurrence_count: 12,
    confidence: 0.97,
    is_confirmed: true,
    merged_keys: [],
  };
}

function filterTransactions(store, query) {
  const params = new URLSearchParams(query);
  const catIds = params.get('category_ids');
  const requestedCurrency = String(params.get('convert_to') || '').trim().toUpperCase();
  const convertTo = SELECTABLE_CURRENCIES.includes(requestedCurrency) ? requestedCurrency : null;
  let rows = store.transactions || [];
  if (catIds) {
    const ids = catIds.split(',').map((value) => Number(value)).filter(Number.isFinite);
    if (ids.length) rows = rows.filter((tx) => tx.category && ids.includes(tx.category.id));
  }
  const limit = Math.max(1, Number(params.get('limit') || rows.length) || rows.length);
  const offset = Math.max(0, Number(params.get('offset') || 0) || 0);
  const sorted = [...rows].sort((left, right) => String(right.date || '').localeCompare(String(left.date || '')) || Number(right.id || 0) - Number(left.id || 0));
  const page = sorted.slice(offset, offset + limit).map((transaction) => {
    if (!convertTo) return transaction;
    return {
      ...transaction,
      amount_primary: roundPromoAmount(convertPromoCurrency(
        transaction.amount,
        transaction.currency,
        convertTo,
      )),
      realized_pnl_primary: transaction.realized_pnl == null
        ? null
        : roundPromoAmount(convertPromoCurrency(
          transaction.realized_pnl,
          transaction.currency,
          convertTo,
        )),
    };
  });
  return { transactions: page, total: sorted.length };
}

function getPerformance(store) {
  const endValue = visibleAccounts(store)
    .filter((acct) => ['margin', 'tfsa', 'rrsp', 'crypto'].includes(acct.account_type) && !acct.is_liability)
    .reduce((sum, acct) => sum + toCad(acct.balance, acct.currency), 0);
  const series = Array.from({ length: 12 }, (_, index) => {
    const date = new Date(Date.UTC(2025, 6 + index, 1)).toISOString().slice(0, 10);
    return {
      date,
      portfolio_value: Math.round(endValue * (0.78 + index * 0.02) * 100) / 100,
      portfolio_return_pct: Math.round((-4 + index * 1.6) * 100) / 100,
      sp500_return_pct: Math.round((-2 + index * 1.15) * 100) / 100,
      tsx_return_pct: Math.round((-1 + index * 0.82) * 100) / 100,
    };
  });
  return {
    currency: 'CAD',
    series,
    summary: {
      end_value: Math.round(endValue * 100) / 100,
      return_pct: 14.8,
      adjusted_change: Math.round(endValue * 0.148 * 100) / 100,
    },
    benchmarks: [
      { id: 'sp500', name: 'S&P 500', provider: 'fred', return_pct: 10.9, as_of_date: '2026-06-12' },
      { id: 'tsx', name: 'S&P/TSX', provider: 'fred', return_pct: 7.6, as_of_date: '2026-06-12' },
    ],
    market_data: { mode: 'keyless', has_key: false },
  };
}

function getPromoMarketDataMetadata(store) {
  const provider = PROMO_MARKET_DATA_PROVIDERS.some((entry) => entry.key === store.marketData.provider)
    ? store.marketData.provider
    : 'fmp';
  const providerLabel = PROMO_MARKET_DATA_PROVIDERS.find((entry) => entry.key === provider)?.label || 'FMP';
  const hasKey = Boolean(store.marketData.maskedKey);
  return {
    status: 'ok',
    mode: hasKey ? 'user_key' : 'keyless',
    provider,
    provider_label: providerLabel,
    available_providers: PROMO_MARKET_DATA_PROVIDERS,
    has_key: hasKey,
    masked_key: store.marketData.maskedKey,
    source: hasKey ? 'user' : 'none',
    config_revision: store.marketData.configRevision,
  };
}

function maskPromoMarketDataKey(apiKey) {
  const value = String(apiKey || '').trim();
  if (!value) return null;
  const visibleTail = value.slice(-4);
  return `${'•'.repeat(Math.max(4, value.length - visibleTail.length))}${visibleTail}`;
}

function promoStringHash(value) {
  let hash = 0;
  for (const character of String(value || '')) {
    hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
  }
  return hash;
}

function normalizePromoMarketWatchlist(rawItems, keyless = false) {
  const items = [];
  const seen = new Set();
  (Array.isArray(rawItems) ? rawItems : []).forEach((rawItem) => {
    const item = rawItem && typeof rawItem === 'object' ? rawItem : { id: rawItem };
    const itemId = String(item.id || '').trim();
    let normalized = null;
    if (PROMO_MARKET_TILE_BY_ID[itemId]) {
      normalized = { id: itemId };
    } else if (!keyless) {
      const symbol = String(item.symbol || itemId.replace(/^custom:/, '')).trim().toUpperCase();
      if (symbol) normalized = { id: `custom:${symbol}`, symbol, label: String(item.label || symbol).slice(0, 48) };
    }
    if (!normalized || seen.has(normalized.id)) return;
    if (keyless && !marketStripCatalog.default_watchlist.includes(normalized.id)) return;
    seen.add(normalized.id);
    items.push(normalized);
  });
  return (items.length ? items : PROMO_DEFAULT_MARKET_WATCHLIST)
    .slice(0, marketStripCatalog.max_tiles);
}

function promoMarketTileCatalogItem(item) {
  const known = PROMO_MARKET_TILE_BY_ID[item.id];
  if (known) return known;
  const symbol = String(item.symbol || item.id.replace(/^custom:/, '')).trim().toUpperCase();
  return {
    id: `custom:${symbol}`,
    label: item.label || symbol,
    symbol,
    fred_symbol: null,
    custom: true,
    precision: 2,
  };
}

function promoMarketTilePayload(item, marketData) {
  const tile = promoMarketTileCatalogItem(item);
  const fixture = PROMO_MARKET_TILE_VALUES[tile.id];
  const hash = promoStringHash(tile.id);
  const value = fixture?.value ?? Math.round((40 + (hash % 90000) / 100) * 100) / 100;
  const changePct = fixture?.change_pct ?? Math.round((((hash % 401) - 200) / 100) * 100) / 100;
  const change = fixture?.change ?? Math.round((value * changePct / 100) * 100) / 100;
  const points = fixture?.points || Array.from({ length: 18 }, (_, index) => {
    const progress = index / 17;
    const wave = Math.sin((index + (hash % 7)) * 0.9) * Math.max(Math.abs(change), value * 0.002);
    return Math.round((value - change + change * progress + wave) * (tile.precision === 4 ? 10000 : 100))
      / (tile.precision === 4 ? 10000 : 100);
  });
  if (!fixture?.points) points[points.length - 1] = value;
  const source = marketData.mode === 'keyless' ? (fixture?.source || 'fred') : marketData.provider;
  return {
    id: tile.id,
    label: tile.label,
    symbol: tile.symbol,
    value,
    change,
    change_pct: changePct,
    points,
    point_mode: 'snapshot',
    reference_value: fixture?.reference_value ?? Math.round((value - change) * 10000) / 10000,
    reference_label: 'Previous close',
    precision: tile.precision,
    as_of: PROMO_TODAY,
    source,
    source_label: marketData.mode === 'keyless' ? (source === 'coingecko' ? 'CoinGecko' : 'Federal Reserve') : marketData.provider_label,
    status: 'ok',
  };
}

function getPromoMarketStrip(store) {
  const marketData = getPromoMarketDataMetadata(store);
  const keyless = marketData.mode === 'keyless';
  const watchlist = normalizePromoMarketWatchlist(store.marketWatchlist, keyless);
  const availableTiles = (keyless
    ? marketStripCatalog.tiles.filter((tile) => marketStripCatalog.default_watchlist.includes(tile.id))
    : marketStripCatalog.tiles
  ).map(({ precision, ...tile }) => tile);
  return {
    status: 'ok',
    updated_at: PROMO_NOW_ISO,
    refresh_seconds: 900,
    market_data: {
      mode: marketData.mode,
      provider: marketData.provider,
      provider_label: marketData.provider_label,
      source: marketData.source,
      has_key: marketData.has_key,
      config_revision: marketData.config_revision,
    },
    tiles: watchlist.map((item) => promoMarketTilePayload(item, marketData)),
    watchlist,
    available_tiles: availableTiles,
  };
}

function searchPromoMarketSymbols(query) {
  const normalized = String(query || '').trim().toUpperCase();
  const results = marketStripCatalog.tiles
    .filter((tile) => !normalized || [tile.id, tile.label, tile.symbol, tile.fred_symbol]
      .filter(Boolean)
      .some((value) => String(value).toUpperCase().includes(normalized)))
    .map(({ precision, ...tile }) => tile);
  if (normalized) {
    results.push({
      id: `custom:${normalized}`,
      label: normalized,
      symbol: normalized,
      fred_symbol: null,
      custom: true,
    });
  }
  return { status: 'ok', results: results.slice(0, 12) };
}

function routeBody(store, route, query) {
  if (route === '/onboarding/status') return { completed: true };
  if (route === '/settings') return store.settings;
  if (route === '/settings/market-data') return getPromoMarketDataMetadata(store);
  if (route === '/networth') return getNetWorth(store);
  if (route === '/accounts') return visibleAccounts(store);
  if (route === '/institutions') return visibleInstitutions(store).map((inst) => institutionPayload(inst));
  if (route === '/institutions/all') return store.institutions.filter((inst) => inst.enabled !== false).map((inst) => institutionPayload(inst, true));
  if (route === '/institutions/scope') return scopeInstitutionPayloads(store);
  if (route === '/institutions/available') return Object.entries(providerCatalog)
    .filter(([provider, metadata]) => !String(provider).startsWith('$') && metadata?.implemented === true && metadata?.availableInAddList === true)
    .map(([provider, metadata]) => ({ provider, name: metadata.displayName || provider, implemented: true, category: metadata.category || null }));
  if (route === '/accounts/balance-history') return getBalanceHistory(store);
  if (route === '/accounts/transaction-import-status') return getImportStatus(store);
  if (route === '/categories') return SEED_CATEGORY_TAXONOMY;
  if (route === '/cash-flow') return getCashFlow(store);
  if (route === '/market-data/strip') return getPromoMarketStrip(store);
  if (route === '/market-data/search') return searchPromoMarketSymbols(new URLSearchParams(query).get('q'));
  if (route === '/market-data/news') return PROMO_MARKET_NEWS;
  if (route === '/fx-rates') return getFxRates(store);
  if (route === '/fx-rates/history') return getFxHistory(store);
  if (route === '/sync/activity') return { active: [] };
  if (route === '/sync/batches/active') return { batches: [] };
  if (route === '/transactions') return filterTransactions(store, query);
  if (route === '/investments/performance') return getPerformance(store);
  if (route.match(/^\/credentials\/[^/]+\/status$/)) return { status: 'not_found' };

  const institutionAccountsMatch = route.match(/^\/institutions\/(\d+)\/accounts$/);
  if (institutionAccountsMatch) {
    const institutionId = Number(institutionAccountsMatch[1]);
    return store.accounts.filter((acct) => acct.institution_id === institutionId).map((acct) => ({
      id: acct.id,
      name: acct.name,
      account_type: acct.account_type,
      currency: acct.currency,
      hidden: Boolean(acct.hidden),
      is_liability: Boolean(acct.is_liability),
      added_at: acct.added_at || PROMO_CREATED_AT,
    }));
  }
  const holdingsMatch = route.match(/^\/accounts\/(\d+)\/holdings$/);
  if (holdingsMatch) return store.holdingsByAccountId[String(holdingsMatch[1])] || [];
  const valueHistoryMatch = route.match(/^\/accounts\/(\d+)\/value-history$/);
  if (valueHistoryMatch) {
    const accountId = Number(valueHistoryMatch[1]);
    const accountRow = store.accounts.find((acct) => acct.id === accountId);
    return {
      status: accountRow ? 'ok' : 'error',
      account_id: accountId,
      currency: accountRow?.currency || 'CAD',
      points: store.valueHistoryByAccountId[String(accountId)] || [],
    };
  }
  if (route.startsWith('/sync/batch/')) {
    const results = Object.fromEntries(visibleInstitutions(store).map((inst) => [inst.provider, {
      status: 'ok',
      provider: inst.provider,
      institution_id: inst.id,
      accounts: store.accounts.filter((acct) => acct.institution_id === inst.id),
      message: `${inst.name} promo sync complete.`,
    }]));
    return { status: 'done', batch_id: route.split('/').pop(), results };
  }
  return undefined;
}

function parseBody(init) {
  if (!init?.body || typeof init.body !== 'string') return {};
  try {
    return JSON.parse(init.body);
  } catch (_) {
    return {};
  }
}

function updateTxAccountSnapshots(store, account) {
  store.transactions = (store.transactions || []).map((tx) => (
    tx.account_id === account.id
      ? { ...tx, account_name: account.name, account_type: account.account_type, institution_name: account.institution, institution_provider: account.provider }
      : tx
  ));
}

function writeBody(route, init) {
  const method = String(init?.method || 'GET').toUpperCase();
  const body = parseBody(init);

  if (route === '/onboarding/complete' && method === 'POST') {
    return { status: 'ok' };
  }

  if (route === '/settings' && method === 'POST') {
    return mutateStore((store) => {
      Object.entries(body).forEach(([key, value]) => {
        if (Object.prototype.hasOwnProperty.call(store.settings, key)) {
          store.settings[key] = value;
        }
      });
      if (body.primary_currency) store.settings.primary_currency = String(body.primary_currency).toUpperCase();
      return {
        status: 'ok',
        institution_id: Number(body.institution_id || 0) || null,
      };
    });
  }

  if (route === '/settings/market-data' && method === 'PUT') {
    const provider = String(body.provider || '').trim().toLowerCase();
    const apiKey = String(body.api_key || '').trim();
    if (!PROMO_MARKET_DATA_PROVIDERS.some((entry) => entry.key === provider)) {
      return { status: 'error', message: 'A market data provider is required' };
    }
    if (!apiKey) {
      return { status: 'error', message: 'API key is required' };
    }
    return mutateStore((store) => {
      store.marketData = {
        provider,
        maskedKey: maskPromoMarketDataKey(apiKey),
        configRevision: PROMO_NOW_ISO,
      };
      return getPromoMarketDataMetadata(store);
    });
  }

  if (route === '/settings/market-data' && method === 'DELETE') {
    return mutateStore((store) => {
      store.marketData = {
        provider: store.marketData.provider,
        maskedKey: null,
        configRevision: PROMO_NOW_ISO,
      };
      return getPromoMarketDataMetadata(store);
    });
  }

  if (route === '/market-data/strip/watchlist' && method === 'PUT') {
    return mutateStore((store) => {
      const keyless = getPromoMarketDataMetadata(store).mode === 'keyless';
      store.marketWatchlist = normalizePromoMarketWatchlist(body.items, keyless);
      return { status: 'ok', watchlist: store.marketWatchlist };
    });
  }

  if (route === '/sync/batch' && method === 'POST') {
    return { status: 'started', batch_id: 'promo-batch' };
  }

  if (route.match(/^\/sync\//)) {
    return { status: 'ok', message: 'Promo sync complete' };
  }

  const institutionSyncStatus = route.match(/^\/institutions\/(\d+)\/sync-status$/);
  if (institutionSyncStatus && method === 'PUT') {
    return mutateStore((store) => {
      const row = store.institutions.find((inst) => inst.id === Number(institutionSyncStatus[1]));
      if (!row) return { status: 'error', message: 'Institution not found' };
      row.sync_status = body.sync_status || row.sync_status || 'ok';
      return { status: 'ok', sync_status: row.sync_status };
    });
  }

  const institutionLogo = route.match(/^\/institutions\/(\d+)\/logo$/);
  if (institutionLogo && (method === 'POST' || method === 'DELETE')) {
    return mutateStore((store) => {
      const row = store.institutions.find((inst) => inst.id === Number(institutionLogo[1]));
      if (!row) return { status: 'error', message: 'Institution not found' };
      row.has_logo = method === 'POST';
      return { status: 'ok', has_logo: row.has_logo };
    });
  }

  const institutionHidden = route.match(/^\/institutions\/(\d+)\/hidden$/);
  if (institutionHidden && method === 'PUT') {
    return mutateStore((store) => {
      const row = store.institutions.find((inst) => inst.id === Number(institutionHidden[1]));
      if (!row) return { status: 'error', message: 'Institution not found' };
      row.hidden = Boolean(body.hidden);
      return { status: 'ok', hidden: row.hidden };
    });
  }

  const accountHidden = route.match(/^\/accounts\/(\d+)\/hidden$/);
  if (accountHidden && method === 'PUT') {
    return mutateStore((store) => {
      const row = store.accounts.find((acct) => acct.id === Number(accountHidden[1]));
      if (!row) return { status: 'error', message: 'Account not found' };
      row.hidden = Boolean(body.hidden);
      return { status: 'ok', hidden: row.hidden };
    });
  }

  const linkedLoans = route.match(/^\/accounts\/(\d+)\/linked-loans$/);
  if (linkedLoans && method === 'PUT') {
    return mutateStore((store) => {
      const assetId = Number(linkedLoans[1]);
      const ids = new Set((body.liability_account_ids || []).map(Number));
      store.accounts.forEach((acct) => {
        if (!acct.is_liability) return;
        if (acct.secured_asset_account_id === assetId && !ids.has(acct.id)) acct.secured_asset_account_id = null;
        if (ids.has(acct.id)) acct.secured_asset_account_id = assetId;
      });
      return { status: 'ok', account_id: assetId, linked_count: ids.size };
    });
  }

  const accountPatch = route.match(/^\/accounts\/(\d+)$/);
  if (accountPatch && method === 'PATCH') {
    return mutateStore((store) => {
      const row = store.accounts.find((acct) => acct.id === Number(accountPatch[1]));
      if (!row) return { status: 'error', message: 'Account not found' };
      if ('name' in body) row.name = String(body.name || row.name).trim() || row.name;
      if (body.account_type) row.account_type = String(body.account_type);
      if (body.currency && body.currency !== row.currency) {
        const cad = toCad(row.balance, row.currency);
        const oldCurrency = row.currency;
        row.currency = String(body.currency).toUpperCase();
        row.balance = Math.round(fromCad(cad, row.currency) * 100) / 100;
        const points = store.valueHistoryByAccountId[String(row.id)] || [];
        points.forEach((point) => {
          point.value = Math.round(fromCad(toCad(point.value, oldCurrency), row.currency) * 100) / 100;
        });
        if (row.purchase) {
          row.purchase.value = Math.round(fromCad(toCad(row.purchase.value, oldCurrency), row.currency) * 100) / 100;
        }
        if (row.opening_balance != null) {
          row.opening_balance = Math.round(fromCad(toCad(row.opening_balance, oldCurrency), row.currency) * 100) / 100;
        }
      }
      if ('opening_balance' in body) {
        row.opening_balance = Number(body.opening_balance || 0);
        row.opening_balance_date = body.opening_balance_date || PROMO_NOW_ISO;
        upsertHistoryPoint(store, row.id, row.opening_balance, row.opening_balance_date, { is_opening: true });
      }
      updateTxAccountSnapshots(store, row);
      return { status: 'ok', name: row.name, opening_balance: row.opening_balance };
    });
  }

  const accountValue = route.match(/^\/accounts\/(\d+)\/value$/);
  if (accountValue && method === 'POST') {
    return mutateStore((store) => {
      const row = store.accounts.find((acct) => acct.id === Number(accountValue[1]));
      if (!row) return { status: 'error', message: 'Account not found' };
      row.balance = Number(body.value || 0);
      upsertHistoryPoint(store, row.id, row.balance, body.date || PROMO_NOW_ISO);
      return { status: 'ok', account_id: row.id };
    });
  }

  const accountPurchase = route.match(/^\/accounts\/(\d+)\/purchase$/);
  if (accountPurchase && method === 'PATCH') {
    return mutateStore((store) => {
      const row = store.accounts.find((acct) => acct.id === Number(accountPurchase[1]));
      if (!row) return { status: 'error', message: 'Account not found' };
      row.purchase = {
        value: Number(body.value ?? 0),
        date: body.date || PROMO_NOW_ISO,
      };
      upsertHistoryPoint(store, row.id, row.purchase.value, row.purchase.date, { is_purchase: true });
      return { status: 'ok', account_id: row.id, purchase: row.purchase };
    });
  }

  const cashOpening = route.match(/^\/accounts\/(\d+)\/cash-opening$/);
  if (cashOpening && method === 'POST') {
    return mutateStore((store) => {
      const row = store.accounts.find((acct) => acct.id === Number(cashOpening[1]));
      if (!row) return { status: 'error', message: 'Account not found' };
      row.balance = Number(body.value || 0);
      row.cash_opening = { amount: row.balance, date: body.date || PROMO_NOW_ISO };
      upsertHistoryPoint(store, row.id, row.balance, row.cash_opening.date, { is_opening: true });
      return { status: 'ok', account_id: row.id };
    });
  }

  const valueDelete = route.match(/^\/accounts\/(\d+)\/value-history\/(\d+)$/);
  if (valueDelete && method === 'DELETE') {
    return mutateStore((store) => {
      const accountId = Number(valueDelete[1]);
      const pointId = Number(valueDelete[2]);
      const points = store.valueHistoryByAccountId[String(accountId)] || [];
      const removed = points.find((point) => point.id === pointId);
      store.valueHistoryByAccountId[String(accountId)] = points.filter((point) => point.id !== pointId);
      const row = store.accounts.find((acct) => acct.id === accountId);
      if (row && removed?.is_purchase) row.purchase = null;
      if (row && removed?.is_opening) {
        row.opening_balance = null;
        row.opening_balance_date = null;
      }
      if (row) row.balance = latestBalance(row, store);
      return { status: removed ? 'ok' : 'error', account_id: accountId, message: removed ? undefined : 'Value point not found' };
    });
  }

  if (route === '/accounts/group' && method === 'POST') {
    return mutateStore((store) => createPromoGroupAccount(store, body));
  }

  if (route === '/accounts/cash' && method === 'POST') {
    return mutateStore((store) => {
      const currency = String(body.currency || 'CAD').toUpperCase();
      let cash = store.accounts.find((acct) => acct.provider === 'manual' && acct.currency === currency);
      if (!cash) {
        const inst = store.institutions.find((row) => row.provider === 'manual');
        cash = account({
          id: store.nextAccountId++,
          institutionId: inst.id,
          institutionName: inst.name,
          provider: 'manual',
          name: `Cash (${currency})`,
          accountType: 'cash',
          currency,
          balance: Number(body.value || 0),
          extra: { last_synced: null, connected_at: null, has_transactions: true },
        });
        store.accounts.push(cash);
      }
      cash.balance = Number(body.value || 0);
      cash.cash_opening = { amount: cash.balance, date: body.date || PROMO_NOW_ISO };
      upsertHistoryPoint(store, cash.id, cash.balance, cash.cash_opening.date, { is_opening: true });
      return { status: 'ok', account_id: cash.id };
    });
  }

  if (route === '/institutions/manual' && method === 'POST') {
    return mutateStore((store) => {
      const name = String(body.name || 'Manual Institution').trim() || 'Manual Institution';
      const institution = {
        id: store.nextInstitutionId++,
        name,
        type: 'manual',
        provider: 'manual_custom',
        category: body.category || 'bank_brokerage',
        hidden: false,
        enabled: true,
        has_logo: false,
        sync_status: 'ok',
        added_at: PROMO_CREATED_AT,
      };
      store.institutions.push(institution);
      const createdAccounts = addManualAccounts(store, institution, body.accounts || []);
      return { status: 'ok', institution, accounts: createdAccounts };
    });
  }

  const addInstitutionAccounts = route.match(/^\/institutions\/(\d+)\/accounts$/);
  if (addInstitutionAccounts && method === 'POST') {
    return mutateStore((store) => {
      const institution = store.institutions.find((inst) => inst.id === Number(addInstitutionAccounts[1]));
      if (!institution) return { status: 'error', message: 'Institution not found' };
      const createdAccounts = addManualAccounts(store, institution, body.accounts || []);
      return { status: 'ok', accounts: createdAccounts };
    });
  }

  const accountDelete = route.match(/^\/accounts\/(\d+)$/);
  if (accountDelete && method === 'DELETE') {
    return mutateStore((store) => {
      const accountId = Number(accountDelete[1]);
      const row = store.accounts.find((acct) => acct.id === accountId);
      store.accounts = store.accounts.filter((acct) => acct.id !== accountId);
      delete store.valueHistoryByAccountId[String(accountId)];
      delete store.holdingsByAccountId[String(accountId)];
      store.transactions = (store.transactions || []).filter((tx) => tx.account_id !== accountId);
      store.accounts.forEach((acct) => {
        if (acct.secured_asset_account_id === accountId) acct.secured_asset_account_id = null;
      });
      return { status: row ? 'ok' : 'error', account_id: accountId, institution_removed: false, message: row ? undefined : 'Account not found' };
    });
  }

  const institutionDelete = route.match(/^\/institutions\/(\d+)$/);
  if (institutionDelete && method === 'DELETE') {
    return mutateStore((store) => {
      const institutionId = Number(institutionDelete[1]);
      const ids = new Set(store.accounts.filter((acct) => acct.institution_id === institutionId).map((acct) => acct.id));
      store.institutions = store.institutions.filter((inst) => inst.id !== institutionId);
      store.accounts = store.accounts.filter((acct) => !ids.has(acct.id));
      ids.forEach((id) => {
        delete store.valueHistoryByAccountId[String(id)];
        delete store.holdingsByAccountId[String(id)];
      });
      store.transactions = (store.transactions || []).filter((tx) => !ids.has(tx.account_id));
      return { status: 'ok' };
    });
  }

  if (route.match(/^\/credentials\/[^/]+$/) && (method === 'PUT' || method === 'DELETE')) {
    return { status: 'ok' };
  }

  return undefined;
}

function upsertHistoryPoint(store, accountId, value, date, flags = {}) {
  const key = String(accountId);
  const day = String(date || PROMO_NOW_ISO).slice(0, 10);
  const points = store.valueHistoryByAccountId[key] || [];
  const existing = points.find((point) => String(point.date || '').slice(0, 10) === day);
  if (existing) {
    existing.value = Number(value || 0);
    existing.date = date || PROMO_NOW_ISO;
    if (flags.is_purchase) existing.is_purchase = true;
    if (flags.is_opening) existing.is_opening = true;
  } else {
    points.push(datedPoint(store, accountId, value, date || PROMO_NOW_ISO, flags));
  }
  store.valueHistoryByAccountId[key] = sortHistory(points);
}

function createPromoGroupAccount(store, body) {
  const category = String(body.category || '').trim();
  const institution = store.institutions.find((inst) => inst.provider === category);
  if (!institution) return { status: 'error', message: 'Unknown net-worth group category' };
  const value = Number(body.value || body.purchase_value || 0);
  const row = account({
    id: store.nextAccountId++,
    institutionId: institution.id,
    institutionName: institution.name,
    provider: institution.provider,
    name: String(body.name || 'Promo Asset').trim() || 'Promo Asset',
    accountType: String(body.account_type || 'other_asset'),
    currency: String(body.currency || 'CAD').toUpperCase(),
    isLiability: category === 'debt',
    balance: value,
    extra: {
      last_synced: null,
      connected_at: null,
      purchase: body.purchase_value ? { value: Number(body.purchase_value), date: body.purchase_date || PROMO_NOW_ISO } : null,
    },
  });
  store.accounts.push(row);
  if (row.purchase) upsertHistoryPoint(store, row.id, row.purchase.value, row.purchase.date, { is_purchase: true });
  upsertHistoryPoint(store, row.id, value, body.date || PROMO_NOW_ISO);
  return { status: 'ok', account_id: row.id, institution_id: row.institution_id, name: row.name };
}

function addManualAccounts(store, institution, rows) {
  return rows.map((entry) => {
    const value = Number(entry.opening_balance ?? 0);
    const row = account({
      id: store.nextAccountId++,
      institutionId: institution.id,
      institutionName: institution.name,
      provider: institution.provider,
      name: String(entry.name || 'Account').trim() || 'Account',
      accountType: String(entry.account_type || 'chequing'),
      currency: String(entry.currency || 'CAD').toUpperCase(),
      isLiability: Boolean(entry.is_liability) || ['credit_card', 'line_of_credit', 'loc', 'loan', 'mortgage', 'student_loan', 'auto_loan', 'personal_loan', 'other_debt'].includes(String(entry.account_type || '').toLowerCase()),
      balance: value,
      extra: {
        last_synced: null,
        connected_at: null,
        opening_balance: value,
        opening_balance_date: entry.opening_balance_date || PROMO_NOW_ISO,
      },
    });
    store.accounts.push(row);
    upsertHistoryPoint(store, row.id, value, row.opening_balance_date, { is_opening: true });
    return row;
  });
}

function jsonResponse(body, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }));
}

function promoBodyForRequest(route, query, init) {
  const method = String(init?.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') {
    return writeBody(route, init);
  }
  return routeBody(readStore(), route, query);
}

function getPromoFetchState() {
  if (typeof window === 'undefined') return null;
  if (!window[PROMO_FETCH_STATE_KEY]) {
    window[PROMO_FETCH_STATE_KEY] = { bodyForRequest: promoBodyForRequest };
  }
  return window[PROMO_FETCH_STATE_KEY];
}

export function installPromoDemoFetch() {
  if (!import.meta.env.DEV) return;
  if (typeof window === 'undefined' || !window.fetch) return;
  setAppClockOverride(isPromoDemoActive() ? PROMO_NOW_ISO : null);
  const state = getPromoFetchState();
  state.bodyForRequest = promoBodyForRequest;
  let currentFetch = window.fetch;
  if (currentFetch[PROMO_FETCH_WRAPPER_KEY] && currentFetch[PROMO_FETCH_STATE_KEY]) return;
  if (currentFetch[PROMO_FETCH_WRAPPER_KEY] && currentFetch[PROMO_ORIGINAL_FETCH_KEY]) {
    currentFetch = currentFetch[PROMO_ORIGINAL_FETCH_KEY];
  }
  const fallbackFetch = currentFetch.bind(window);
  const promoFetch = (input, init) => {
    try {
      const url = typeof input === 'string' ? input : (input && input.url);
      if (isPromoDemoActive() && typeof url === 'string' && url.startsWith(API)) {
        const rest = url.slice(API.length);
        const qIdx = rest.indexOf('?');
        const route = qIdx >= 0 ? rest.slice(0, qIdx) : rest;
        const query = qIdx >= 0 ? rest.slice(qIdx + 1) : '';
        const method = String(init?.method || 'GET').toUpperCase();
        const body = state.bodyForRequest(route, query, init);
        if (body !== undefined) return jsonResponse(body);
        const message = `Promo demo does not support ${method} ${route}.`;
        return jsonResponse({ status: 'error', detail: message, message }, 501);
      }
    } catch (error) {
      return jsonResponse({ status: 'error', message: error?.message || 'Promo demo request failed' }, 500);
    }
    return fallbackFetch(input, init);
  };
  promoFetch[PROMO_FETCH_WRAPPER_KEY] = true;
  promoFetch[PROMO_ORIGINAL_FETCH_KEY] = fallbackFetch;
  promoFetch[PROMO_FETCH_STATE_KEY] = state;
  window.fetch = promoFetch;
}
