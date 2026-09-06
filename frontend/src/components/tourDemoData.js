import { API } from '../config';
import {
  getSeedCategory,
  SEED_CATEGORY_PARENTS,
  SEED_CATEGORY_TAXONOMY,
} from '../constants/seedCategoryTaxonomy';

// Tour "demo mode". While active, the app's own API calls are answered with a small,
// consistent DUMMY dataset so every tour page renders populated — even on a brand-new
// (empty) account. Deactivated when the tour finishes (`setTourDemoActive`), after which a
// refetch restores the user's real data. Nothing is persisted; it only shapes in-flight
// responses while the flag is on. Fixture shapes mirror the real endpoints' response models.
//
// Institutions set `has_logo: false` and use bundled-catalog names so InstitutionLogo loads
// the bundled asset by name. account_type values map to the app's badge tones (real estate →
// `primary_residence` for the amber "physical" badge). Categories come from the generated
// frontend mirror of SEED_TAXONOMY so their icons and per-theme colors stay current.
const TOUR_DEMO_FETCH_STATE_KEY = '__breaktwentyTourDemoFetchState';
const TOUR_DEMO_ORIGINAL_FETCH_KEY = '__breaktwentyTourDemoOriginalFetch';
const TOUR_DEMO_FETCH_WRAPPER_KEY = '__breaktwentyTourDemoFetchWrapper';
export const TOUR_DEMO_NOW_ISO = '2026-06-15T12:57:00-04:00';
export const TOUR_DEMO_CASH_FLOW_ANCHOR_DATE = '2026-06-15';
const TOUR_DEMO_NOW_MS = new Date(TOUR_DEMO_NOW_ISO).getTime();
const TOUR_DEMO_ADDED_AT = '2026-01-01T12:00:00.000Z';
function getSharedDemoFetchState() {
  if (typeof window === 'undefined') {
    return null;
  }
  if (!window[TOUR_DEMO_FETCH_STATE_KEY]) {
    window[TOUR_DEMO_FETCH_STATE_KEY] = {
      active: false,
      installed: false,
      realFetch: null,
      demoBodyForRoute: null,
    };
  }
  return window[TOUR_DEMO_FETCH_STATE_KEY];
}
let demoActive = false;
export function setTourDemoActive(on) {
  demoActive = !!on;
  const state = getSharedDemoFetchState();
  if (state) state.active = demoActive;
}
export function isTourDemoActive() {
  const state = getSharedDemoFetchState();
  return state ? !!state.active : demoActive;
}

// Tour hint: lets a step ask a page to enter a specific sub-view (e.g. Holdings → Income tab
// with a month pre-selected). Pages read `getTourHint()` on mount and on the event.
let tourHint = null;
export function setTourHint(hint) {
  tourHint = hint;
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('breaktwenty-tour-hint'));
}
export function getTourHint() { return tourHint; }

// ---- Institutions (synced: TD/Scotiabank/Questrade/Wealthsimple/Amex; manual: Real Estate/Cash) ----
const INSTITUTIONS = [
  { id: 101, name: 'TD', type: 'bank', provider: 'td', category: null, has_logo: false, sync_status: 'ok' },
  { id: 102, name: 'Scotiabank', type: 'bank', provider: 'scotiabank', category: null, has_logo: false, sync_status: 'ok' },
  { id: 103, name: 'Questrade', type: 'brokerage', provider: 'questrade', category: null, has_logo: false, sync_status: 'ok' },
  { id: 104, name: 'Wealthsimple', type: 'brokerage', provider: 'wealthsimple', category: null, has_logo: false, sync_status: 'ok' },
  { id: 105, name: 'Real Estate', type: 'manual', provider: 'real_estate', category: 'real_estate', has_logo: false, sync_status: 'ok' },
  { id: 106, name: 'Cash', type: 'manual', provider: 'manual', category: null, has_logo: false, sync_status: 'ok' },
  { id: 107, name: 'American Express', type: 'credit_card', provider: 'amex', category: null, has_logo: false, sync_status: 'ok' },
].map((institution) => ({ ...institution, added_at: TOUR_DEMO_ADDED_AT }));

function acct(id, instId, instName, provider, name, type, currency, isLiability, balance, extra = {}) {
  return {
    id, institution: instName, institution_id: instId, provider, name, account_type: type,
    currency, is_liability: isLiability, balance, last_synced: extra.last_synced || null,
    added_at: TOUR_DEMO_ADDED_AT,
    connected_at: ['manual', 'real_estate'].includes(provider) ? null : TOUR_DEMO_ADDED_AT,
    is_imported: false,
    purchase: extra.purchase || null, has_transactions: extra.has_transactions || false,
    cash_opening: extra.cash_opening || null, opening_balance: null, opening_balance_date: null,
    secured_asset_account_id: null,
  };
}
function syncedAtMinutesAgo(minutes) {
  return new Date(TOUR_DEMO_NOW_MS - minutes * 60000).toISOString();
}
const SYNCED_NOW = syncedAtMinutesAgo(0);
const SYNCED_15_MINUTES_AGO = syncedAtMinutesAgo(15);
const SYNCED_2_HOURS_AGO = syncedAtMinutesAgo(120);
const SYNCED_8_MINUTES_AGO = syncedAtMinutesAgo(8);
const ACCOUNTS = [
  acct(1, 101, 'TD', 'td', 'Line of Credit', 'line_of_credit', 'CAD', true, 5200.00, { last_synced: SYNCED_NOW, has_transactions: true }),
  acct(2, 101, 'TD', 'td', 'Chequing', 'chequing', 'CAD', false, 18400.00, { last_synced: SYNCED_NOW, has_transactions: true }),
  acct(3, 102, 'Scotiabank', 'scotiabank', 'Momentum Visa', 'credit_card', 'CAD', true, 3388.40, { last_synced: SYNCED_15_MINUTES_AGO, has_transactions: true }),
  acct(4, 102, 'Scotiabank', 'scotiabank', 'Chequing', 'chequing', 'CAD', false, 0.00, { last_synced: SYNCED_15_MINUTES_AGO, has_transactions: true }),
  acct(5, 103, 'Questrade', 'questrade', 'Margin', 'margin', 'CAD', false, 58940.20, { last_synced: SYNCED_2_HOURS_AGO, has_transactions: true }),
  acct(6, 103, 'Questrade', 'questrade', 'Cash', 'cash', 'CAD', false, 22410.00, { last_synced: SYNCED_2_HOURS_AGO, has_transactions: true }),
  acct(7, 104, 'Wealthsimple', 'wealthsimple', 'TFSA', 'tfsa', 'CAD', false, 24210.75, { last_synced: SYNCED_8_MINUTES_AGO, has_transactions: true }),
  acct(8, 105, 'Real Estate', 'real_estate', 'Primary Residence', 'primary_residence', 'CAD', false, 612000.00, { purchase: { value: 540000.00, date: '2021-05-01T12:00:00Z' } }),
  acct(9, 106, 'Cash', 'manual', 'Cash (CAD)', 'cash', 'CAD', false, 1200.00, { has_transactions: true, cash_opening: { amount: 1000.00, date: '2026-01-01T12:00:00Z' } }),
  acct(10, 106, 'Cash', 'manual', 'Cash (USD)', 'cash', 'USD', false, 800.00, { has_transactions: true }),
  acct(11, 106, 'Cash', 'manual', 'Cash (EUR)', 'cash', 'EUR', false, 400.00, { has_transactions: true }),
  acct(12, 107, 'American Express', 'amex', 'Cobalt Card', 'credit_card', 'CAD', true, 1284.62, { last_synced: SYNCED_15_MINUTES_AGO, has_transactions: true }),
  acct(13, 104, 'Wealthsimple', 'wealthsimple', 'RRSP', 'rrsp', 'CAD', false, 10000.00, { last_synced: SYNCED_8_MINUTES_AGO, has_transactions: true }),
];
const INSTITUTION_ACCOUNTS = INSTITUTIONS.reduce((byInstitution, institution) => {
  byInstitution[institution.id] = ACCOUNTS
    .filter((account) => account.institution_id === institution.id)
    .map((account) => ({
      id: account.id,
      name: account.name,
      account_type: account.account_type,
      currency: account.currency,
      hidden: false,
      is_liability: Boolean(account.is_liability),
      added_at: '2026-01-01T12:00:00.000Z',
    }));
  return byInstitution;
}, {});
const SCOPE_INSTITUTIONS = INSTITUTIONS.map((institution) => {
  const accounts = ACCOUNTS
    .filter((account) => account.institution_id === institution.id)
    .map((account) => ({
      ...account,
      institution: institution.name,
      institution_id: institution.id,
      provider: institution.provider,
      hidden: false,
    }));
  return {
    ...institution,
    hidden: false,
    key: `institution-${institution.id}`,
    accounts,
    accountIds: accounts.map((account) => account.id),
  };
});

const MARKET_STRIP = {
  status: 'ok',
  updated_at: TOUR_DEMO_NOW_ISO,
  refresh_seconds: 900,
  market_data: { mode: 'keyless', has_key: false },
  watchlist: [],
  available_tiles: [],
  tiles: [
    { id: 'sp500', label: 'S&P 500', value: 6025.14, change: 31.75, change_pct: 0.42, precision: 2, source: 'fred', as_of: '2026-06-12', point_mode: 'daily', points: [5890, 5912, 5904, 5942, 5968, 5993, 6025.14] },
    { id: 'nasdaq', label: 'NASDAQ', value: 19884.91, change: 74.72, change_pct: 0.29, precision: 2, source: 'fred', as_of: '2026-06-12', point_mode: 'daily', points: [19380, 19410, 19520, 19670, 19715, 19810, 19884.91] },
    { id: 'dow', label: 'DOW', value: 43812.67, change: 149.60, change_pct: 0.29, precision: 2, source: 'fred', as_of: '2026-06-12', point_mode: 'daily', points: [43020, 43110, 43280, 43460, 43610, 43663, 43812.67] },
    { id: 'usdcad', label: 'USD/CAD', value: 1.3718, change: -0.0023, change_pct: -0.16, precision: 4, source: 'fred', as_of: '2026-06-12', point_mode: 'daily', points: [1.382, 1.379, 1.376, 1.374, 1.373, 1.3725, 1.3718] },
    { id: 'btc-usd', label: 'Bitcoin USD', value: 106420.55, change: -46.31, change_pct: -0.07, precision: 2, source: 'coingecko', as_of: '2026-06-15', point_mode: 'intraday', points: [105820, 106120, 106450, 106300, 106610, 106480, 106420.55] },
  ],
};

const MARKET_NEWS = {
  status: 'ok',
  articles: [
    { id: 'tour-sec-muni', title: 'SEC Office of Municipal Securities Updates FAQs for Registration of Municipal Advisors', source: 'Securities and Exchange Commission', published_at: '2026-06-14T14:20:00-04:00', url: 'https://www.sec.gov/' },
    { id: 'tour-bea-affiliates', title: 'Activities of U.S. Affiliates of Foreign Multinational Enterprises, 2024', source: 'Bureau of Economic Analysis', published_at: '2026-06-13T10:00:00-04:00', url: 'https://www.bea.gov/' },
    { id: 'tour-fed-taskforces', title: 'Federal Reserve announces the leadership and objectives of its task forces to advance the conduct of monetary policy', source: 'Federal Reserve', published_at: '2026-06-12T12:30:00-04:00', url: 'https://www.federalreserve.gov/' },
  ],
};

const FX_RATES = {
  base: 'CAD',
  rates: { CAD: 1, USD: 1.3718, EUR: 1.5650 },
};

const FX_HISTORY = {
  rates: {
    USD: {
      '2022-05-08': 1.2850,
      '2023-01-01': 1.3550,
      '2024-01-01': 1.3260,
      '2025-01-01': 1.4380,
      '2026-06-15': 1.3718,
    },
    EUR: {
      '2022-05-08': 1.3500,
      '2023-01-01': 1.4450,
      '2024-01-01': 1.4600,
      '2025-01-01': 1.4910,
      '2026-06-15': 1.5650,
    },
  },
};

const SYNC_ACTIVITY = { active: [] };

const NETWORTH = {
  current: { total_assets: 748830.95, total_liabilities: 9873.02, net_worth: 738957.93, currency: 'CAD', date: null },
  history: [],
};

// 1500 DAILY points (~4 years, ending today) for net worth and per-account histories. A smooth shape (two early
// hills → deeper middle trough → late climb to the current value) provides the trend, and a seeded
// MEAN-REVERTING RANDOM WALK — shared across accounts so the sum carries it — provides organic,
// real-looking movement (runs and pullbacks, not a repeating sine ripple). The walk is tapered
// to ~0 over the final weeks so each series still lands on the account's current balance.
const HIST_POINTS = 1500;
const HIST_END_MS = Date.UTC(2026, 5, 15); // June 15, 2026 (most recent point)
function shapeBase(t) {
  const pts = [[0, 0.86], [0.08, 0.98], [0.17, 0.84], [0.27, 0.93], [0.45, 0.72], [0.58, 0.55], [0.72, 0.62], [0.84, 0.70], [1, 1]];
  for (let k = 1; k < pts.length; k += 1) {
    if (t <= pts[k][0]) {
      const t0 = pts[k - 1][0]; const v0 = pts[k - 1][1];
      const t1 = pts[k][0]; const v1 = pts[k][1];
      const f = (t - t0) / (t1 - t0);
      return v0 + (v1 - v0) * (f * f * (3 - 2 * f));
    }
  }
  return 1;
}
const NET_WORTH_WALK = (() => {
  let a = 0x9e3779b9; // mulberry32 — deterministic, seeded
  const rng = () => {
    a = (a + 0x6d2b79f5) | 0;
    let x = Math.imul(a ^ (a >>> 15), 1 | a);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
  const walk = []; let slow = 0; let fast = 0;
  for (let i = 0; i < HIST_POINTS; i += 1) {
    slow = slow * 0.99 + (rng() - 0.5) * 0.04; // broad, steep hills (high persistence)
    fast = fast * 0.80 + (rng() - 0.5) * 0.045; // daily spikes (low persistence)
    walk.push((slow + fast) * Math.min(1, (1 - i / (HIST_POINTS - 1)) * 60));
  }
  return walk;
})();
function genNetworthHistory(current) {
  const out = [];
  for (let i = 0; i < HIST_POINTS; i += 1) {
    const date = new Date(HIST_END_MS - (HIST_POINTS - 1 - i) * 86400000).toISOString().slice(0, 10);
    const factor = shapeBase(i / (HIST_POINTS - 1)) * (1 + NET_WORTH_WALK[i]);
    const totalAssets = Math.round(current.total_assets * factor * 100) / 100;
    const totalLiabilities = Math.round(current.total_liabilities * factor * 100) / 100;
    out.push({
      date,
      total_assets: totalAssets,
      total_liabilities: totalLiabilities,
      net_worth: Math.round((totalAssets - Math.abs(totalLiabilities)) * 100) / 100,
    });
  }
  return out;
}
NETWORTH.history = genNetworthHistory(NETWORTH.current);
function genBalanceHistory(finalBalance) {
  const out = {};
  for (let i = 0; i < HIST_POINTS; i += 1) {
    const date = new Date(HIST_END_MS - (HIST_POINTS - 1 - i) * 86400000).toISOString().slice(0, 10);
    out[date] = Math.round(finalBalance * shapeBase(i / (HIST_POINTS - 1)) * (1 + NET_WORTH_WALK[i]) * 100) / 100;
  }
  return out;
}
const BALANCE_HISTORY = {
  1: genBalanceHistory(5200.00),
  2: genBalanceHistory(18400.00),
  3: genBalanceHistory(3388.40),
  4: genBalanceHistory(0.00),
  5: genBalanceHistory(58940.20),
  6: genBalanceHistory(22410.00),
  7: genBalanceHistory(24210.75),
  8: genBalanceHistory(612000.00),
  9: genBalanceHistory(1200.00),
  10: genBalanceHistory(800.00),
  11: genBalanceHistory(400.00),
  12: genBalanceHistory(1284.62),
  13: genBalanceHistory(10000.00),
};

// Holdings per investment account: 5 = Questrade Margin, 6 = Questrade Cash, 7 = Wealthsimple TFSA, 13 = Wealthsimple RRSP.
const HOLDINGS = {
  5: [
    { id: 501, symbol: 'VFV.TO', name: 'Vanguard S&P 500 Index ETF', quantity: 50, market_value: 3740.20, average_cost: 3116.83, last_price: 74.80, contract_multiplier: null, change_pct: 0.8, daily_pnl: 29.00, currency: 'CAD', sector: 'ETF', instrument_kind: 'etf' },
    { id: 502, symbol: 'AAPL', name: 'Apple Inc.', quantity: 36, market_value: 7000.00, average_cost: 7085.02, last_price: 194.44, contract_multiplier: null, change_pct: -0.4, daily_pnl: -28.00, currency: 'USD', sector: 'Technology', instrument_kind: 'equity' },
    { id: 503, symbol: 'HHIS.TO', name: 'Harvest Diversified High Income Shares ETF', quantity: 900, market_value: 15000.00, average_cost: 10000.00, last_price: 16.67, contract_multiplier: null, change_pct: 0.4, daily_pnl: 60.00, currency: 'CAD', sector: 'ETF', instrument_kind: 'etf' },
    { id: 504, symbol: 'HYLD.TO', name: 'Hamilton Enhanced U.S. Covered Call ETF', quantity: 1100, market_value: 13000.00, average_cost: 8965.52, last_price: 11.82, contract_multiplier: null, change_pct: 0.6, daily_pnl: 78.00, currency: 'CAD', sector: 'ETF', instrument_kind: 'etf' },
    { id: 505, symbol: 'BANK.TO', name: 'Evolve Canadian Banks and Lifecos Enhanced Yield Index Fund', quantity: 500, market_value: 9500.00, average_cost: 7600.00, last_price: 19.00, contract_multiplier: null, change_pct: 0.3, daily_pnl: 28.50, currency: 'CAD', sector: 'Financial Services', instrument_kind: 'etf' },
    { id: 506, symbol: 'RY.TO', name: 'Royal Bank of Canada', quantity: 50, market_value: 8200.00, average_cost: 6307.69, last_price: 164.00, contract_multiplier: null, change_pct: 0.5, daily_pnl: 41.00, currency: 'CAD', sector: 'Financial Services', instrument_kind: 'equity' },
    { id: 507, symbol: 'CAD', name: 'Canadian Dollar Cash', quantity: 2500.00, market_value: 2500.00, average_cost: 1.00, last_price: 1.00, contract_multiplier: null, change_pct: 0.0, daily_pnl: 0.00, currency: 'CAD', sector: 'Cash', instrument_kind: 'cash' },
  ],
  6: [
    { id: 601, symbol: 'HYLD.TO', name: 'Hamilton Enhanced U.S. Covered Call ETF', quantity: 1100, market_value: 12910.00, average_cost: 9562.96, last_price: 11.74, contract_multiplier: null, change_pct: 0.5, daily_pnl: 64.55, currency: 'CAD', sector: 'ETF', instrument_kind: 'etf' },
    { id: 602, symbol: 'BANK.TO', name: 'Evolve Canadian Banks and Lifecos Enhanced Yield Index Fund', quantity: 500, market_value: 9500.00, average_cost: 7600.00, last_price: 19.00, contract_multiplier: null, change_pct: 0.3, daily_pnl: 28.50, currency: 'CAD', sector: 'Financial Services', instrument_kind: 'etf' },
  ],
  7: [
    { id: 701, symbol: 'VDY.TO', name: 'Vanguard FTSE Canadian High Dividend Yield ETF', quantity: 212, market_value: 9200.00, average_cost: 6344.83, last_price: 43.40, contract_multiplier: null, change_pct: 0.5, daily_pnl: 46.00, currency: 'CAD', sector: 'ETF', instrument_kind: 'etf' },
    { id: 702, symbol: 'XEQT.TO', name: 'iShares Core Equity ETF Portfolio', quantity: 284, market_value: 7000.00, average_cost: 7368.42, last_price: 24.65, contract_multiplier: null, change_pct: -0.3, daily_pnl: -21.00, currency: 'CAD', sector: 'ETF', instrument_kind: 'etf' },
    { id: 703, symbol: 'NVDA', name: 'NVIDIA Corp.', quantity: 4, market_value: 2800.00, average_cost: 2731.71, last_price: 700.00, contract_multiplier: null, change_pct: 1.4, daily_pnl: 39.20, currency: 'USD', sector: 'Technology', instrument_kind: 'equity' },
    { id: 704, symbol: 'BNS.TO', name: 'Bank of Nova Scotia', quantity: 66.8, market_value: 5210.75, average_cost: 4008.27, last_price: 78.00, contract_multiplier: null, change_pct: 0.4, daily_pnl: 20.84, currency: 'CAD', sector: 'Financial Services', instrument_kind: 'equity' },
  ],
  13: [
    { id: 1301, symbol: 'RY.TO', name: 'Royal Bank of Canada', quantity: 60, market_value: 10000.00, average_cost: 7692.31, last_price: 166.67, contract_multiplier: null, change_pct: 0.5, daily_pnl: 50.00, currency: 'CAD', sector: 'Financial Services', instrument_kind: 'equity' },
  ],
};

// ---- Categories (current frontend SEED_TAXONOMY mirror; transaction values remain dummy) ----
const CAT = {
  paycheck: getSeedCategory('paycheck'),
  dividend: getSeedCategory('dividend'),
  interest: getSeedCategory('interest'),
  groceries: getSeedCategory('groceries'),
  gas: getSeedCategory('gas'),
  streaming: getSeedCategory('streaming'),
  fitness: getSeedCategory('fitness'),
  apps: getSeedCategory('apps_and_saas'),
  internet: getSeedCategory('internet'),
  rent: getSeedCategory('rent'),
  phone: getSeedCategory('phone'),
  utilities: getSeedCategory('utilities'),
  interestCharged: getSeedCategory('interest_paid'),
  withholdingTax: getSeedCategory('withheld'),
  insurance: getSeedCategory('insurance'),
  buy: getSeedCategory('buy'),
  sell: getSeedCategory('sell'),
};
const parentOf = (parentId) => SEED_CATEGORY_PARENTS.find((parent) => parent.id === parentId) || null;

// ---- Transactions (shared by Dashboard recent, Transactions page, Investments income) ----
// Account-relevant + varied institutions/badges; recent dates so buy/sell/dividend show in
// the Dashboard recent list; older dividends feed the Income dividend chart across months.
const ACCT_META = {
  td_chequing: { id: 2, name: 'Chequing', type: 'chequing', inst: 'TD', provider: 'td' },
  td_loc: { id: 1, name: 'Line of Credit', type: 'line_of_credit', inst: 'TD', provider: 'td' },
  scotia_cheq: { id: 4, name: 'Chequing', type: 'chequing', inst: 'Scotiabank', provider: 'scotiabank' },
  scotia_visa: { id: 3, name: 'Momentum Visa', type: 'credit_card', inst: 'Scotiabank', provider: 'scotiabank' },
  amex_cobalt: { id: 12, name: 'Cobalt Card', type: 'credit_card', inst: 'American Express', provider: 'amex' },
  q_margin: { id: 5, name: 'Margin', type: 'margin', inst: 'Questrade', provider: 'questrade' },
  q_cash: { id: 6, name: 'Cash', type: 'cash', inst: 'Questrade', provider: 'questrade' },
  ws_tfsa: { id: 7, name: 'TFSA', type: 'tfsa', inst: 'Wealthsimple', provider: 'wealthsimple' },
  ws_rrsp: { id: 13, name: 'RRSP', type: 'rrsp', inst: 'Wealthsimple', provider: 'wealthsimple' },
  cash_cad: { id: 9, name: 'Cash (CAD)', type: 'cash', inst: 'Cash', provider: 'manual' },
};
function tx(id, date, account, description, amount, category, opts = {}) {
  const a = ACCT_META[account];
  return {
    id, account_id: a.id, account_name: a.name, account_type: a.type,
    institution_name: a.inst, institution_provider: a.provider,
    date, type: opts.type || (amount >= 0 ? 'deposit' : 'debit'),
    symbol: opts.symbol || null, description, user_description: null, display_description: description,
    user_notes: null, manual_kind: null, amount, currency: opts.currency || 'CAD',
    amount_primary: opts.amount_primary != null ? opts.amount_primary : amount,
    quantity: opts.quantity || null, price: opts.price || null, commission: opts.commission || null,
    category_source: 'auto', category,
  };
}
const TX_LIST = [
  tx(1001, '2026-06-12', 'td_chequing', 'Payroll Deposit', 2950.00, CAT.paycheck),
  tx(1002, '2026-06-11', 'q_margin', 'BUY 30 AAPL', -5859.00, CAT.buy, { type: 'buy', symbol: 'AAPL', quantity: 30, price: 195.30, commission: 4.95, currency: 'USD', amount_primary: -8027.00 }),
  tx(1003, '2026-06-10', 'ws_rrsp', 'RY.TO Dividend', 720.00, CAT.dividend, { type: 'dividend', symbol: 'RY.TO', quantity: 60 }),
  tx(1004, '2026-06-10', 'q_cash', 'HYLD.TO Distribution', 1510.00, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 1100 }),
  tx(1005, '2026-06-10', 'ws_tfsa', 'VDY.TO Distribution', 949.25, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO', quantity: 212 }),
  tx(1006, '2026-06-09', 'amex_cobalt', 'Farm Boy', -89.24, CAT.groceries),
  tx(1007, '2026-06-01', 'ws_tfsa', 'NVDA Dividend', 4.20, CAT.dividend, { type: 'dividend', symbol: 'NVDA', quantity: 4, currency: 'USD', amount_primary: 5.75 }),
  tx(1008, '2026-06-01', 'ws_tfsa', 'NVDA Foreign Withholding Tax', -0.63, CAT.withholdingTax, { type: 'withholding_tax', symbol: 'NVDA', currency: 'USD', amount_primary: -0.86 }),
  tx(1009, '2026-06-06', 'cash_cad', 'Costco', -142.83, CAT.groceries),
  tx(1010, '2026-06-08', 'q_margin', 'SELL 15 TSLA', 3724.50, CAT.sell, { type: 'sell', symbol: 'TSLA', quantity: 15, price: 248.30, commission: 4.95, currency: 'USD', amount_primary: 5102.00 }),
  tx(1011, '2026-06-07', 'scotia_visa', 'Petro-Canada', -64.20, CAT.gas),
  tx(1012, '2026-06-05', 'scotia_visa', 'Spotify', -11.29, CAT.streaming),
  tx(1013, '2026-06-04', 'td_chequing', 'FreshCo', -76.18, CAT.groceries),
  tx(1014, '2026-06-03', 'amex_cobalt', 'Metro', -121.55, CAT.groceries),
  tx(1015, '2026-06-02', 'cash_cad', 'Longo\'s', -64.90, CAT.groceries),
  tx(1016, '2026-06-01', 'scotia_visa', 'No Frills', -105.30, CAT.groceries),
  tx(1017, '2026-06-03', 'scotia_visa', 'Rogers', -89.00, CAT.internet),
  tx(1018, '2026-06-02', 'td_loc', 'Interest Charged', -38.50, CAT.interestCharged),
  tx(1019, '2026-06-01', 'q_cash', 'Interest', 4.55, CAT.interest, { type: 'interest' }),
  tx(1022, '2026-05-01', 'ws_tfsa', 'VDY.TO Distribution', 660.00, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO', quantity: 212 }),
  tx(1023, '2026-05-01', 'ws_rrsp', 'RY.TO Dividend', 700.00, CAT.dividend, { type: 'dividend', symbol: 'RY.TO', quantity: 60 }),
  tx(1024, '2026-05-01', 'ws_tfsa', 'BNS.TO Dividend', 620.00, CAT.dividend, { type: 'dividend', symbol: 'BNS.TO', quantity: 66.8 }),
  tx(1025, '2026-05-01', 'q_cash', 'HYLD.TO Distribution', 850.00, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 1100 }),
  tx(1026, '2026-05-01', 'q_margin', 'BANK.TO Distribution', 180.00, CAT.dividend, { type: 'dividend', symbol: 'BANK.TO', quantity: 500 }),
  tx(1027, '2026-04-01', 'q_margin', 'HHIS.TO Distribution', 780.00, CAT.dividend, { type: 'dividend', symbol: 'HHIS.TO', quantity: 900 }),
  tx(1028, '2026-04-01', 'q_margin', 'HYLD.TO Distribution', 820.00, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 1100 }),
  tx(1029, '2026-04-01', 'q_cash', 'BANK.TO Distribution', 560.00, CAT.dividend, { type: 'dividend', symbol: 'BANK.TO', quantity: 500 }),
  tx(1030, '2026-04-01', 'ws_rrsp', 'RY.TO Dividend', 674.25, CAT.dividend, { type: 'dividend', symbol: 'RY.TO', quantity: 60 }),
  tx(1031, '2026-04-01', 'ws_tfsa', 'NVDA Dividend', 4.20, CAT.dividend, { type: 'dividend', symbol: 'NVDA', quantity: 4, currency: 'USD', amount_primary: 5.75 }),
  tx(1032, '2026-04-01', 'ws_tfsa', 'NVDA Foreign Withholding Tax', -0.63, CAT.withholdingTax, { type: 'withholding_tax', symbol: 'NVDA', currency: 'USD', amount_primary: -0.86 }),
  tx(1033, '2026-03-02', 'ws_tfsa', 'VDY.TO Distribution', 620.00, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO', quantity: 212 }),
  tx(1034, '2026-03-02', 'ws_tfsa', 'BNS.TO Dividend', 590.00, CAT.dividend, { type: 'dividend', symbol: 'BNS.TO', quantity: 66.8 }),
  tx(1035, '2026-03-02', 'q_margin', 'HYLD.TO Distribution', 810.00, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 1100 }),
  tx(1036, '2026-03-02', 'q_margin', 'BANK.TO Distribution', 660.00, CAT.dividend, { type: 'dividend', symbol: 'BANK.TO', quantity: 500 }),
  tx(1037, '2026-02-02', 'q_margin', 'HHIS.TO Distribution', 725.00, CAT.dividend, { type: 'dividend', symbol: 'HHIS.TO', quantity: 900 }),
  tx(1038, '2026-02-02', 'q_cash', 'HYLD.TO Distribution', 780.00, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 1100 }),
  tx(1039, '2026-02-02', 'q_margin', 'BANK.TO Distribution', 520.00, CAT.dividend, { type: 'dividend', symbol: 'BANK.TO', quantity: 500 }),
  tx(1040, '2026-02-02', 'ws_rrsp', 'RY.TO Dividend', 510.00, CAT.dividend, { type: 'dividend', symbol: 'RY.TO', quantity: 60 }),
  tx(1041, '2026-01-02', 'ws_tfsa', 'VDY.TO Distribution', 360.00, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO', quantity: 212 }),
  tx(1042, '2026-01-02', 'ws_tfsa', 'BNS.TO Dividend', 300.00, CAT.dividend, { type: 'dividend', symbol: 'BNS.TO', quantity: 66.8 }),
  tx(1043, '2026-01-02', 'q_cash', 'HYLD.TO Distribution', 430.00, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 1100 }),
  tx(1044, '2026-01-02', 'q_margin', 'BANK.TO Distribution', 404.25, CAT.dividend, { type: 'dividend', symbol: 'BANK.TO', quantity: 500 }),
  tx(1045, '2026-01-02', 'ws_tfsa', 'NVDA Dividend', 4.20, CAT.dividend, { type: 'dividend', symbol: 'NVDA', quantity: 4, currency: 'USD', amount_primary: 5.75 }),
  tx(1046, '2026-01-02', 'ws_tfsa', 'NVDA Foreign Withholding Tax', -0.63, CAT.withholdingTax, { type: 'withholding_tax', symbol: 'NVDA', currency: 'USD', amount_primary: -0.86 }),
  tx(1047, '2025-12-01', 'q_margin', 'HHIS.TO Distribution', 200.00, CAT.dividend, { type: 'dividend', symbol: 'HHIS.TO', quantity: 900 }),
  tx(1048, '2025-12-01', 'q_margin', 'HYLD.TO Distribution', 180.00, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 1100 }),
  tx(1049, '2025-12-01', 'q_margin', 'BANK.TO Distribution', 120.00, CAT.dividend, { type: 'dividend', symbol: 'BANK.TO', quantity: 500 }),
  tx(1050, '2025-12-01', 'ws_rrsp', 'RY.TO Dividend', 100.00, CAT.dividend, { type: 'dividend', symbol: 'RY.TO', quantity: 60 }),
  tx(1051, '2025-11-03', 'ws_tfsa', 'VDY.TO Distribution', 180.00, CAT.dividend, { type: 'dividend', symbol: 'VDY.TO', quantity: 212 }),
  tx(1052, '2025-11-03', 'ws_tfsa', 'BNS.TO Dividend', 150.00, CAT.dividend, { type: 'dividend', symbol: 'BNS.TO', quantity: 66.8 }),
  tx(1053, '2025-11-03', 'q_margin', 'HYLD.TO Distribution', 130.00, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 1100 }),
  tx(1054, '2025-11-03', 'q_margin', 'BANK.TO Distribution', 84.25, CAT.dividend, { type: 'dividend', symbol: 'BANK.TO', quantity: 500 }),
  tx(1055, '2025-11-03', 'ws_tfsa', 'NVDA Dividend', 4.20, CAT.dividend, { type: 'dividend', symbol: 'NVDA', quantity: 4, currency: 'USD', amount_primary: 5.75 }),
  tx(1056, '2025-11-03', 'ws_tfsa', 'NVDA Foreign Withholding Tax', -0.63, CAT.withholdingTax, { type: 'withholding_tax', symbol: 'NVDA', currency: 'USD', amount_primary: -0.86 }),
  tx(1057, '2025-10-01', 'q_margin', 'HHIS.TO Distribution', 160.00, CAT.dividend, { type: 'dividend', symbol: 'HHIS.TO', quantity: 900 }),
  tx(1058, '2025-10-01', 'q_margin', 'HYLD.TO Distribution', 170.00, CAT.dividend, { type: 'dividend', symbol: 'HYLD.TO', quantity: 1100 }),
  tx(1059, '2025-10-01', 'q_margin', 'BANK.TO Distribution', 100.00, CAT.dividend, { type: 'dividend', symbol: 'BANK.TO', quantity: 500 }),
  tx(1060, '2025-10-01', 'ws_rrsp', 'RY.TO Dividend', 70.00, CAT.dividend, { type: 'dividend', symbol: 'RY.TO', quantity: 60 }),
];
// ---- Transaction import status (only synced institutions; manual ones excluded) ----
function importInst(instId, name, provider, accounts) {
  return {
    institution_id: instId, institution: name, provider, status: 'incremental', status_label: 'Sync complete',
    history_status: 'complete', history_status_label: 'Backfill complete', current_fetch_status: null,
    current_fetch_label: null, current_fetch_mode: null, transaction_import_job_status: null,
    transaction_import_job_id: null, transaction_import_job_last_progress_at: null,
    transaction_import_job_last_progress_label: null, transaction_import_job_stale_recovery_count: 0,
    accounts,
  };
}
function importAcct(accountId, name, type, provider, isLiability) {
  return {
    account_id: accountId, account_name: name, account_type: type, is_liability: isLiability,
    status: 'incremental', status_label: 'Sync complete', history_status: 'complete',
    history_status_label: 'Backfill complete', current_fetch_status: null, current_fetch_label: null,
    current_fetch_mode: null, provider, account_external_id: `demo_${accountId}`,
    backfill_start_date: '2022-06-15', backfill_end_date: '2026-06-15', completed_start_date: '2022-06-15',
    completed_end_date: '2026-06-15', data_start_date: '2022-06-15', data_end_date: '2026-06-14',
    completed_windows: 24, total_windows: 24, pending_windows: 0, failed_windows: 0,
    current_window_start_date: null, current_window_end_date: null,
    latest_incremental_start_date: '2026-06-01', latest_incremental_end_date: '2026-06-15',
    last_successful_fetch_at: SYNCED_15_MINUTES_AGO, last_error: null, transaction_count: 240,
  };
}
const IMPORT_STATUS = {
  institutions: [
    importInst(101, 'TD', 'td', [importAcct(1, 'Line of Credit', 'line_of_credit', 'td', true), importAcct(2, 'Chequing', 'chequing', 'td', false)]),
    importInst(102, 'Scotiabank', 'scotiabank', [importAcct(3, 'Momentum Visa', 'credit_card', 'scotiabank', true), importAcct(4, 'Chequing', 'chequing', 'scotiabank', false)]),
    importInst(103, 'Questrade', 'questrade', [importAcct(5, 'Margin', 'margin', 'questrade', false), importAcct(6, 'Cash', 'cash', 'questrade', false)]),
    importInst(104, 'Wealthsimple', 'wealthsimple', [importAcct(7, 'TFSA', 'tfsa', 'wealthsimple', false), importAcct(13, 'RRSP', 'rrsp', 'wealthsimple', false)]),
    importInst(107, 'American Express', 'amex', [importAcct(12, 'Cobalt Card', 'credit_card', 'amex', true)]),
  ],
  generated_at: SYNCED_NOW,
};

// ---- Cash flow (June 2026; demo income/spend/recurring) ----
// Expense rows carry parent fields (spending breakdown groups by parent — without them rows
// render as "Uncategorized"). Income rows list leaves, so they don't need parents.
function cfRow(cat, amount, count, pct, withParent = false) {
  const p = withParent ? parentOf(cat.parent_id) : null;
  return {
    category_id: cat.id, name: cat.name, icon: cat.icon, icon_set: cat.icon_set,
    color_dark: cat.color_dark, color_light: cat.color_light,
    classification: cat.classification, seed_key: cat.seed_key, parent_id: cat.parent_id,
    parent_name: p ? p.name : null,
    parent_color_dark: p ? p.color_dark : null,
    parent_color_light: p ? p.color_light : null,
    parent_icon: p ? p.icon : null,
    parent_icon_set: p ? p.icon_set : null, amount, percent_of_total: pct, transaction_count: count,
  };
}
function recurring(id, name, cat, cadence, amount, account, institution, lastDate, nextDate, opts = {}) {
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
    cadence, interval_days: opts.interval_days || (cadence === 'biweekly' ? 14.0 : cadence === 'semimonthly' ? 15.0 : 30.0), amount, amount_min: amount, amount_max: amount, currency: 'CAD',
    amount_primary: amount, last_amount: amount, last_amount_primary: amount, account_name: account,
    institution_name: institution, last_seen_date: lastDate, next_expected_date: nextDate,
    occurrence_count: opts.occurrence_count || 12, confidence: 0.97, is_confirmed: true, merged_keys: [],
  };
}
const CASH_FLOW = {
  period: { start: '2026-06-01', end: '2026-06-30', currency: 'CAD' },
  totals: { income: 9089.55, expense: 2439.36, net: 6650.19 },
  expense_breakdown: [
    cfRow(CAT.rent, 1436.00, 1, 58.9, true),
    cfRow(CAT.groceries, 600.00, 6, 24.6, true),
    cfRow(CAT.gas, 240.00, 3, 9.8, true),
    cfRow(CAT.internet, 89.00, 1, 3.6, true),
    cfRow(CAT.interestCharged, 38.50, 1, 1.6, true),
    cfRow(CAT.streaming, 35.00, 2, 1.4, true),
    cfRow(CAT.withholdingTax, 0.86, 1, 0.0, true),
  ],
  income_breakdown: [
    cfRow(CAT.paycheck, 5900.00, 2, 64.9),
    cfRow(CAT.dividend, 3185.00, 6, 35.0),
    cfRow(CAT.interest, 4.55, 1, 0.1),
  ],
  needs_review: {
    category_id: null, transaction_count: 0,
  },
  investment_activity: {
    buys: -8027.00, sells: 5102.00, dividends: 0.00, interest: 0.00, withholding_tax: 0.00,
    expired: 0.00, transfers: 0.00, deposits: 0.00, withdrawals: 0.00, cc_payments: 320.00,
    loan_payments: 0.00, loan_advances: 0.00, realized_pnl: 0.00, breakdown: [],
  },
  trend_12_months: [
    { month: '2025-07', income: 5650.00, expense: 3120.00, net: 2530.00 },
    { month: '2025-08', income: 5900.00, expense: 2845.00, net: 3055.00 },
    { month: '2025-09', income: 5750.00, expense: 3375.00, net: 2375.00 },
    { month: '2025-10', income: 6400.00, expense: 2960.00, net: 3440.00 },
    { month: '2025-11', income: 6450.00, expense: 4120.00, net: 2330.00 },
    { month: '2025-12', income: 6500.00, expense: 4680.00, net: 1820.00 },
    { month: '2026-01', income: 7400.00, expense: 2600.00, net: 4800.00 },
    { month: '2026-02', income: 8435.00, expense: 2300.00, net: 6135.00 },
    { month: '2026-03', income: 8580.00, expense: 2500.00, net: 6080.00 },
    { month: '2026-04', income: 8740.00, expense: 2450.00, net: 6290.00 },
    { month: '2026-05', income: 8910.00, expense: 2748.90, net: 6161.10 },
    { month: '2026-06', income: 9089.55, expense: 2439.36, net: 6650.19 },
  ],
  recurring: [
    recurring(1, 'Netflix', CAT.streaming, 'monthly', -16.99, 'Chequing', 'TD', '2026-06-08', '2026-07-08'),
    recurring(2, 'Rogers', CAT.internet, 'monthly', -89.00, 'Momentum Visa', 'Scotiabank', '2026-06-03', '2026-07-03'),
    recurring(3, 'Spotify', CAT.streaming, 'semimonthly', -5.65, 'Momentum Visa', 'Scotiabank', '2026-06-20', '2026-07-05', { occurrence_count: 24 }),
    recurring(4, 'Rent', CAT.rent, 'monthly', -1436.00, 'Chequing', 'Scotiabank', '2026-06-01', '2026-07-01'),
    recurring(5, 'Bell Mobility', CAT.phone, 'monthly', -62.40, 'Momentum Visa', 'Scotiabank', '2026-06-11', '2026-07-11'),
    recurring(6, 'Toronto Hydro', CAT.utilities, 'monthly', -92.15, 'Chequing', 'Scotiabank', '2026-06-10', '2026-07-10'),
    recurring(7, 'GoodLife Fitness', CAT.fitness, 'biweekly', -29.99, 'Cobalt Card', 'American Express', '2026-06-12', '2026-06-26', { occurrence_count: 26 }),
    recurring(8, 'iCloud', CAT.apps, 'monthly', -3.99, 'Cobalt Card', 'American Express', '2026-06-04', '2026-07-04'),
    recurring(9, 'Tenant Insurance', CAT.insurance, 'monthly', -34.75, 'Chequing', 'TD', '2026-06-09', '2026-07-09'),
  ],
  previous_totals: { start: '2026-05-01', end: '2026-05-31', income: 8910.00, expense: 2748.90, net: 6161.10 },
};

const FIXTURES = {
  '/networth': () => NETWORTH,
  '/accounts': () => ACCOUNTS,
  '/institutions': () => INSTITUTIONS,
  '/institutions/all': () => INSTITUTIONS.map((institution) => ({
    ...institution,
    hidden: false,
    added_at: TOUR_DEMO_ADDED_AT,
  })),
  '/institutions/scope': () => SCOPE_INSTITUTIONS,
  '/accounts/balance-history': () => BALANCE_HISTORY,
  '/accounts/transaction-import-status': () => IMPORT_STATUS,
  '/categories': () => SEED_CATEGORY_TAXONOMY,
  '/cash-flow': () => CASH_FLOW,
  '/market-data/strip': () => MARKET_STRIP,
  '/market-data/news': () => MARKET_NEWS,
  '/fx-rates': () => FX_RATES,
  '/fx-rates/history': () => FX_HISTORY,
  '/sync/activity': () => SYNC_ACTIVITY,
};

// `/transactions` mirrors the real paged endpoint closely enough for the tour:
// category filtering for Cash Flow handoffs, simple search, and limit/offset.
function filterTransactions(query) {
  const params = new URLSearchParams(query);
  const catIds = params.get('category_ids');
  let rows = TX_LIST;
  if (catIds) {
    const ids = catIds.split(',').map((s) => parseInt(s, 10)).filter((n) => !Number.isNaN(n));
    if (ids.length) {
      rows = rows.filter((t) => t.category && ids.includes(t.category.id));
    }
  }
  const search = String(params.get('search') || '').trim().toLowerCase();
  if (search) {
    rows = rows.filter((t) => [
      t.symbol,
      t.description,
      t.user_description,
      t.account_name,
      t.institution_name,
      t.type,
      t.category?.name,
      t.date,
    ].filter(Boolean).join(' ').toLowerCase().includes(search));
  }
  rows = [...rows].sort((left, right) => {
    const dateCompare = String(right.date || '').localeCompare(String(left.date || ''));
    return dateCompare || ((right.id || 0) - (left.id || 0));
  });
  const limit = Math.max(1, parseInt(params.get('limit') || String(rows.length), 10) || rows.length);
  const offset = Math.max(0, parseInt(params.get('offset') || '0', 10) || 0);
  return { transactions: rows.slice(offset, offset + limit), total: rows.length };
}

function demoBodyForRoute(route, query) {
  if (FIXTURES[route]) return FIXTURES[route]();
  if (route === '/transactions') return filterTransactions(query);
  const institutionAccountsMatch = route.match(/^\/institutions\/(\d+)\/accounts$/);
  if (institutionAccountsMatch) return INSTITUTION_ACCOUNTS[institutionAccountsMatch[1]] || [];
  const holdingsMatch = route.match(/^\/accounts\/(\d+)\/holdings$/);
  if (holdingsMatch) return HOLDINGS[holdingsMatch[1]] || [];
  return undefined;
}

export function installTourDemoFetch() {
  const state = getSharedDemoFetchState();
  if (!state || !window.fetch) return;
  state.demoBodyForRoute = demoBodyForRoute;

  const currentFetch = window.fetch;
  if (state.installed && state.realFetch && currentFetch !== state.realFetch) {
    return;
  }
  const originalFetch = window[TOUR_DEMO_ORIGINAL_FETCH_KEY]
    || state.realFetch
    || currentFetch[TOUR_DEMO_ORIGINAL_FETCH_KEY]
    || currentFetch.bind(window);
  window[TOUR_DEMO_ORIGINAL_FETCH_KEY] = originalFetch;
  state.realFetch = originalFetch;

  if (currentFetch[TOUR_DEMO_FETCH_WRAPPER_KEY]) {
    state.installed = true;
    return;
  }

  state.installed = true;
  const tourDemoFetch = (input, init) => {
    try {
      const url = typeof input === 'string' ? input : (input && input.url);
      if (state.active && typeof url === 'string' && url.startsWith(API)) {
        const rest = url.slice(API.length);
        const qIdx = rest.indexOf('?');
        const route = qIdx >= 0 ? rest.slice(0, qIdx) : rest;
        const body = state.demoBodyForRoute
          ? state.demoBodyForRoute(route, qIdx >= 0 ? rest.slice(qIdx + 1) : '')
          : undefined;
        if (body !== undefined) {
          return Promise.resolve(new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }));
        }
      }
    } catch (_) {
      // fall through to the real network on any matching error
    }
    return state.realFetch(input, init);
  };
  tourDemoFetch[TOUR_DEMO_FETCH_WRAPPER_KEY] = true;
  tourDemoFetch[TOUR_DEMO_ORIGINAL_FETCH_KEY] = originalFetch;
  window.fetch = tourDemoFetch;
}
