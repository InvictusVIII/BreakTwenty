import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { API } from '../config';
import { getInstitutionLogoConfig } from '../constants/providers';
import providerCatalog from '../constants/providerCatalog.json';
import financeIcons from '../constants/financeIcons.json';
import { SELECTABLE_CURRENCIES } from '../constants/currencies';
import { readCashFlowResponse } from '../utils/apiResponse';
import { getAppClockOverride, getAppNow } from '../utils/appClock';
import {
  installPromoDemoFetch,
  isPromoDemoActive,
  PROMO_DEMO_NOW_ISO,
  setPromoDemoActive,
} from './promoDemoEnvironment';

const PROMO_STORAGE_KEYS = [
  'breaktwenty_promo_demo_active_v1',
  'breaktwenty_promo_demo_store_v1',
];

const MANUAL_TILE_PROVIDERS = [
  'manual_custom',
  'real_estate',
  'vehicles',
  'valuables',
  'private_investments',
  'other_assets',
  'manual',
  'debt',
];

const EXPECTED_SYNCABLE_PROVIDERS = Object.entries(providerCatalog)
  .filter(([, metadata]) => metadata.implemented === true && metadata.availableInAddList === true)
  .map(([provider]) => provider)
  .sort();

function resetPromoStorage() {
  PROMO_STORAGE_KEYS.forEach((key) => window.localStorage.removeItem(key));
}

describe('promo demo environment fetch shim', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-31T20:00:00.000Z'));
    resetPromoStorage();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ real: true })))));
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    setPromoDemoActive(false);
    resetPromoStorage();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('serves a separate promo environment with every syncable provider and every add tile family', async () => {
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const [institutionsResponse, accountsResponse] = await Promise.all([
      window.fetch(`${API}/institutions/all`),
      window.fetch(`${API}/accounts`),
    ]);
    const [institutions, accounts] = await Promise.all([
      institutionsResponse.json(),
      accountsResponse.json(),
    ]);

    const providers = institutions.map((institution) => institution.provider);
    expect(
      providers.filter((provider) => !MANUAL_TILE_PROVIDERS.includes(provider)).sort(),
    ).toEqual(EXPECTED_SYNCABLE_PROVIDERS);
    MANUAL_TILE_PROVIDERS.forEach((provider) => {
      expect(providers).toContain(provider);
    });

    const house = accounts.find((account) => account.provider === 'real_estate');
    const scotiaMortgage = accounts.find((account) => account.provider === 'scotiabank' && account.account_type === 'mortgage');
    expect(scotiaMortgage.secured_asset_account_id).toBe(house.id);
    expect(institutions.find((institution) => institution.provider === 'manual_custom').name).toBe('Canada Life');
    expect(getInstitutionLogoConfig('Canada Life')).toMatchObject({ asset: 'canada-life.png' });
  });

  it('freezes the application clock and all observed fixture dates at June 28, 2026', async () => {
    installPromoDemoFetch();
    setPromoDemoActive(true);

    expect(PROMO_DEMO_NOW_ISO).toBe('2026-06-28T16:00:00-04:00');
    expect(getAppClockOverride()).toBe(PROMO_DEMO_NOW_ISO);
    expect(getAppNow().toISOString()).toBe('2026-06-28T20:00:00.000Z');

    const responses = await Promise.all([
      window.fetch(`${API}/accounts`),
      window.fetch(`${API}/accounts/balance-history`),
      window.fetch(`${API}/accounts/transaction-import-status`),
      window.fetch(`${API}/networth`),
      window.fetch(`${API}/transactions`),
      window.fetch(`${API}/investments/performance`),
      window.fetch(`${API}/market-data/strip`),
      window.fetch(`${API}/market-data/news`),
    ]);
    const [accounts, balanceHistory, importStatus, networth, transactionPage, performance, marketStrip, news]
      = await Promise.all(responses.map((response) => response.json()));
    const cutoffDay = '2026-06-28';
    const cutoffMs = new Date(PROMO_DEMO_NOW_ISO).getTime();

    expect(networth.current.date).toBe(cutoffDay);
    expect(networth.history.at(-1).date).toBe(cutoffDay);
    expect(transactionPage.transactions[0].date).toBe(cutoffDay);
    expect(performance.series.at(-1).date <= cutoffDay).toBe(true);
    expect(marketStrip.updated_at).toBe(PROMO_DEMO_NOW_ISO);
    expect(marketStrip.tiles.every((tile) => tile.as_of <= cutoffDay)).toBe(true);
    expect(news.fetched_at).toBe(PROMO_DEMO_NOW_ISO);
    expect(news.articles.every((article) => article.published_at.slice(0, 10) <= cutoffDay)).toBe(true);
    expect(accounts
      .filter((account) => account.last_synced)
      .every((account) => new Date(account.last_synced).getTime() <= cutoffMs)).toBe(true);
    expect(Object.values(balanceHistory)
      .every((history) => Object.keys(history).at(-1) === cutoffDay)).toBe(true);
    expect(importStatus.generated_at).toBe(PROMO_DEMO_NOW_ISO);
    expect(importStatus.institutions
      .flatMap((institution) => institution.accounts)
      .every((account) => account.data_end_date <= cutoffDay && account.latest_incremental_end_date <= cutoffDay))
      .toBe(true);
  });

  it('re-bases only normal app currency values using the frozen June 28 FX snapshot', async () => {
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const [cadNetworth, nativeAccounts, marketStrip, nativeTransactions] = await Promise.all([
      window.fetch(`${API}/networth`).then((response) => response.json()),
      window.fetch(`${API}/accounts`).then((response) => response.json()),
      window.fetch(`${API}/market-data/strip`).then((response) => response.json()),
      window.fetch(`${API}/transactions`).then((response) => response.json()),
    ]);

    for (const currency of SELECTABLE_CURRENCIES) {
      await window.fetch(`${API}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ primary_currency: currency }),
      });
      const fx = await window.fetch(`${API}/fx-rates`).then((response) => response.json());
      expect(fx.base).toBe(currency);
      expect(fx.rates[currency]).toBe(1);
      expect(Object.keys(fx.rates)).toEqual(SELECTABLE_CURRENCIES);
    }

    await window.fetch(`${API}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ primary_currency: 'BTC' }),
    });
    const [btcFx, fxHistory, btcNetworth, btcCashFlow, btcTransactions, accountsAfter, marketAfter] = await Promise.all([
      window.fetch(`${API}/fx-rates`).then((response) => response.json()),
      window.fetch(`${API}/fx-rates/history`).then((response) => response.json()),
      window.fetch(`${API}/networth`).then((response) => response.json()),
      window.fetch(`${API}/cash-flow`).then((response) => response.json()),
      window.fetch(`${API}/transactions?convert_to=BTC`).then((response) => response.json()),
      window.fetch(`${API}/accounts`).then((response) => response.json()),
      window.fetch(`${API}/market-data/strip`).then((response) => response.json()),
    ]);

    const cadPerBtc = 84369.830586;
    expect(btcFx).toMatchObject({ base: 'BTC', rates: { BTC: 1, CAD: cadPerBtc } });
    expect(btcFx.rates.USD).toBeCloseTo(59474.01, 8);
    expect(fxHistory.primary).toBe('BTC');
    expect(Object.keys(fxHistory.rates)).toEqual(SELECTABLE_CURRENCIES.filter((currency) => currency !== 'CAD'));
    expect(fxHistory.rates.BTC['2026-06-28']).toBe(cadPerBtc);
    expect(btcNetworth.current.currency).toBe('BTC');
    expect(btcNetworth.current.net_worth).toBeCloseTo(cadNetworth.current.net_worth / cadPerBtc, 7);
    expect(btcCashFlow.period.currency).toBe('BTC');
    expect(btcCashFlow.totals.income).toBeCloseTo(9230 / cadPerBtc, 7);
    expect(btcCashFlow.recurring[0].currency).toBe('CAD');
    expect(btcCashFlow.recurring[0].amount).toBe(-2350);
    expect(btcCashFlow.recurring[0].amount_primary).toBeCloseTo(-2350 / cadPerBtc, 7);

    const nativeUsdTransaction = nativeTransactions.transactions.find((transaction) => transaction.currency === 'USD');
    const btcUsdTransaction = btcTransactions.transactions.find((transaction) => transaction.id === nativeUsdTransaction.id);
    expect(btcUsdTransaction.amount).toBe(nativeUsdTransaction.amount);
    expect(btcUsdTransaction.currency).toBe('USD');
    expect(btcUsdTransaction.amount_primary).toBeCloseTo(
      nativeUsdTransaction.amount * 1.4186 / cadPerBtc,
      7,
    );

    // Native account units and the separately sourced market strip are not
    // primary-currency values in the real app, so the Promo shim leaves them alone.
    expect(accountsAfter).toEqual(nativeAccounts);
    expect(marketAfter).toEqual(marketStrip);
  });

  it('recovers the seeded promo dataset when stored promo visibility would render empty or stale', async () => {
    const realFetch = window.fetch;
    window.localStorage.setItem('breaktwenty_promo_demo_store_v1', JSON.stringify({
      version: 7,
      institutions: [{ id: 1, name: 'Hidden Promo', provider: 'manual', enabled: true, hidden: true }],
      accounts: [{ id: 1, institution_id: 1, name: 'Hidden Cash', provider: 'manual', hidden: false }],
    }));
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const [institutionsResponse, accountsResponse] = await Promise.all([
      window.fetch(`${API}/institutions/all`),
      window.fetch(`${API}/accounts`),
    ]);
    const [institutions, accounts] = await Promise.all([
      institutionsResponse.json(),
      accountsResponse.json(),
    ]);

    expect(institutions.find((institution) => institution.provider === 'rbc')).toBeTruthy();
    expect(accounts.length).toBeGreaterThan(1);
    expect(realFetch).not.toHaveBeenCalled();
  });

  it('rejects visible stored promo data that does not match the current seed identity', async () => {
    window.localStorage.setItem('breaktwenty_promo_demo_store_v1', JSON.stringify({
      version: 7,
      seed_id: 'dev-snapshot',
      institutions: [{ id: 1, name: 'Dev Bank', provider: 'manual', enabled: true, hidden: false }],
      accounts: [{ id: 1, institution_id: 1, name: 'Dev Chequing', provider: 'manual', hidden: false, balance: 123 }],
    }));
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const accountsResponse = await window.fetch(`${API}/accounts`);
    const accounts = await accountsResponse.json();

    expect(accounts.find((account) => account.name === 'Dev Chequing')).toBeFalsy();
    expect(accounts.find((account) => account.provider === 'real_estate').balance).toBe(790000);
    expect(JSON.parse(window.localStorage.getItem('breaktwenty_promo_demo_store_v1')).seed_id)
      .toBe('promo-frozen-snapshot-2026-06-28');
  });

  it('serves the full canonical category taxonomy and valid icons for transaction fixtures', async () => {
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const [categoriesResponse, transactionsResponse] = await Promise.all([
      window.fetch(`${API}/categories`),
      window.fetch(`${API}/transactions`),
    ]);
    const [{ categories }, { transactions }] = await Promise.all([
      categoriesResponse.json(),
      transactionsResponse.json(),
    ]);
    const categoriesBySeedKey = Object.fromEntries(
      categories.map((category) => [category.seed_key, category]),
    );
    const financeIconSlugs = new Set(financeIcons.icons.map((icon) => icon.slug));

    expect(categories).toHaveLength(110);
    expect(categoriesBySeedKey.groceries).toMatchObject({ icon: '🛒', icon_set: 'noto' });
    expect(categoriesBySeedKey.mortgage).toMatchObject({ icon: '🏡', icon_set: 'noto' });
    expect(categoriesBySeedKey.insurance).toMatchObject({ icon: '🏚️', parent_id: categoriesBySeedKey.housing.id });
    expect(categoriesBySeedKey.expired).toMatchObject({
      icon: '⌛',
      icon_set: 'fluent',
      parent_id: categoriesBySeedKey.investments.id,
    });
    expect(categoriesBySeedKey.withheld).toMatchObject({ icon: 'tax-form', parent_id: categoriesBySeedKey.taxes.id });
    categories
      .filter((category) => category.icon_set === 'finance')
      .forEach((category) => expect(financeIconSlugs).toContain(category.icon));
    transactions.forEach((transaction) => {
      expect(categoriesBySeedKey[transaction.category.seed_key]).toEqual(transaction.category);
    });
  });

  it('refreshes persisted dummy transaction categories from the current frontend taxonomy', async () => {
    installPromoDemoFetch();
    setPromoDemoActive(true);
    const stored = JSON.parse(window.localStorage.getItem('breaktwenty_promo_demo_store_v1'));
    stored.transactions[0].category = {
      ...stored.transactions[0].category,
      color_dark: '#000000',
      color_light: '#ffffff',
    };
    window.localStorage.setItem('breaktwenty_promo_demo_store_v1', JSON.stringify(stored));

    setPromoDemoActive(false);
    setPromoDemoActive(true);

    const [categoriesResponse, transactionsResponse] = await Promise.all([
      window.fetch(`${API}/categories`),
      window.fetch(`${API}/transactions`),
    ]);
    const [{ categories }, { transactions }] = await Promise.all([
      categoriesResponse.json(),
      transactionsResponse.json(),
    ]);
    const canonical = categories.find(
      (category) => category.seed_key === stored.transactions[0].category.seed_key,
    );
    const refreshed = transactions.find((transaction) => transaction.id === stored.transactions[0].id);

    expect(refreshed.category).toEqual(canonical);
  });

  it('seeds promo sync recency, cash holdings, option holdings, and denser history for screenshots', async () => {
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const [institutionsResponse, accountsResponse, networthResponse, balanceHistoryResponse] = await Promise.all([
      window.fetch(`${API}/institutions/all`),
      window.fetch(`${API}/accounts`),
      window.fetch(`${API}/networth`),
      window.fetch(`${API}/accounts/balance-history`),
    ]);
    const [institutions, accounts, networth, balanceHistory] = await Promise.all([
      institutionsResponse.json(),
      accountsResponse.json(),
      networthResponse.json(),
      balanceHistoryResponse.json(),
    ]);

    expect(institutions.find((institution) => institution.provider === 'td').sync_status).toBe('auth_required');
    expect(institutions.find((institution) => institution.provider === 'eqbank').sync_status).toBe('auth_required');
    expect(accounts.find((account) => account.provider === 'rbc').last_synced).toBe('2026-06-28T20:00:00.000Z');
    expect(accounts.find((account) => account.provider === 'td').last_synced).toBe('2026-06-26T20:00:00.000Z');
    expect(accounts.find((account) => account.provider === 'eqbank').last_synced).toBe('2026-06-21T20:00:00.000Z');
    expect(networth.history).toHaveLength(1500);
    expect(networth.current).toMatchObject({
      total_assets: 1495046.02,
      total_liabilities: 517800,
      net_worth: 977246.02,
      currency: 'CAD',
    });
    expect(networth.history.at(-1).net_worth).toBe(networth.current.net_worth);
    expect(networth.history.at(-1).date).toBe('2026-06-28');
    expect(Math.min(...networth.history.map((point) => point.net_worth))).toBeLessThan(networth.current.net_worth * 0.7);
    const house = accounts.find((account) => account.provider === 'real_estate');
    const houseHistory = balanceHistory[String(house.id)];
    const houseDates = Object.keys(houseHistory);
    const houseValues = Object.values(houseHistory).map(Number);
    expect(houseDates).toHaveLength(1500);
    expect(houseDates.at(-1)).toBe('2026-06-28');
    expect(houseValues.at(-1)).toBe(house.balance);
    expect(Math.min(...houseValues)).toBeLessThan(house.balance * 0.7);

    const accountIds = accounts.map((account) => account.id);
    const holdingsByAccount = await Promise.all(accountIds.map(async (accountId) => {
      const response = await window.fetch(`${API}/accounts/${accountId}/holdings`);
      return response.json();
    }));
    const holdings = holdingsByAccount.flat();
    const screenshotReserve = holdings.find((holding) => holding.symbol === 'SGOV');
    expect(holdings.some((holding) => holding.instrument_kind === 'cash' && holding.symbol === 'CAD')).toBe(true);
    expect(holdings.some((holding) => holding.instrument_kind === 'option' && holding.contract_multiplier === 100 && holding.symbol.includes('31JUL26'))).toBe(true);
    expect(holdings.some((holding) => holding.instrument_kind === 'option' && Number(holding.quantity) < 0)).toBe(true);
    expect(screenshotReserve).toMatchObject({
      name: 'iShares 0-3 Month Treasury Bond ETF',
      currency: 'USD',
      instrument_kind: 'etf',
    });
    expect(screenshotReserve.market_value * 1.4186).toBeCloseTo(7358.88115388, 8);
    const moomoo = accounts.find((account) => account.provider === 'moomoo');
    expect(moomoo.balance - 22600).toBeCloseTo(screenshotReserve.market_value, 8);
  });

  it('aligns promo Food & Drink and dividend cash-flow totals with seeded transactions', async () => {
    installPromoDemoFetch();
    setPromoDemoActive(true);
    const categories = (await (await window.fetch(`${API}/categories`)).json()).categories;
    const categoryId = (seedKey) => categories.find((category) => category.seed_key === seedKey).id;
    const groceriesId = categoryId('groceries');
    const dividendId = categoryId('dividend');
    const interestId = categoryId('interest');

    const [cashFlowResponse, groceryResponse, dividendResponse, interestResponse] = await Promise.all([
      window.fetch(`${API}/cash-flow`),
      window.fetch(`${API}/transactions?category_ids=${groceriesId}`),
      window.fetch(`${API}/transactions?category_ids=${dividendId}`),
      window.fetch(`${API}/transactions?category_ids=${interestId}`),
    ]);
    const [cashFlow, groceryRows, dividendRows, interestRows] = await Promise.all([
      readCashFlowResponse(cashFlowResponse),
      groceryResponse.json(),
      dividendResponse.json(),
      interestResponse.json(),
    ]);
    const groceryTotal = groceryRows.transactions.reduce((sum, tx) => sum + Math.abs(Number(tx.amount_primary || tx.amount || 0)), 0);
    const juneDividendTotal = dividendRows.transactions
      .filter((tx) => String(tx.date).startsWith('2026-06'))
      .reduce((sum, tx) => sum + Number(tx.amount_primary || tx.amount || 0), 0);

    expect(groceryRows.total).toBe(10);
    expect(Math.round(groceryTotal * 100) / 100).toBe(cashFlow.expense_breakdown.find((row) => row.category_id === groceriesId).amount);
    expect(Math.round(juneDividendTotal * 100) / 100).toBe(cashFlow.income_breakdown.find((row) => row.category_id === dividendId).amount);
    expect(interestRows.total).toBeGreaterThan(6);
  });

  it('persists promo value edits in local storage without calling the real backend', async () => {
    const realFetch = window.fetch;
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const editResponse = await window.fetch(`${API}/accounts/401/value`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: 812345, date: '2026-06-28' }),
    });
    expect(await editResponse.json()).toMatchObject({ status: 'ok', account_id: 401 });

    setPromoDemoActive(false);
    setPromoDemoActive(true);
    const accountsResponse = await window.fetch(`${API}/accounts`);
    const accounts = await accountsResponse.json();

    expect(accounts.find((account) => account.id === 401).balance).toBe(812345);
    expect(realFetch).not.toHaveBeenCalled();
  });

  it('mirrors the current settings and credential response contracts', async () => {
    const realFetch = window.fetch;
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const onboardingStatusResponse = await window.fetch(`${API}/onboarding/status`);
    const onboardingCompleteResponse = await window.fetch(`${API}/onboarding/complete`, { method: 'POST' });
    const settingsResponse = await window.fetch(`${API}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_timezone: 'America/Vancouver',
        institution_id: 301,
      }),
    });
    const missingCredentialResponse = await window.fetch(`${API}/credentials/rbc/status?institution_id=301`);
    const saveCredentialResponse = await window.fetch(`${API}/credentials/rbc`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: 'promo-user',
        password: 'promo-password',
        institution_id: 301,
      }),
    });
    const deleteCredentialResponse = await window.fetch(`${API}/credentials/rbc?institution_id=301`, {
      method: 'DELETE',
    });

    expect(await onboardingStatusResponse.json()).toEqual({ completed: true });
    expect(await onboardingCompleteResponse.json()).toEqual({ status: 'ok' });
    expect(await settingsResponse.json()).toEqual({ status: 'ok', institution_id: 301 });
    expect(await missingCredentialResponse.json()).toEqual({ status: 'not_found' });
    expect(await saveCredentialResponse.json()).toEqual({ status: 'ok' });
    expect(await deleteCredentialResponse.json()).toEqual({ status: 'ok' });
    expect(realFetch).not.toHaveBeenCalled();
  });

  it('keeps current market-data settings reads and writes inside promo mode', async () => {
    const realFetch = window.fetch;
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const initialResponse = await window.fetch(`${API}/settings/market-data`);
    expect(await initialResponse.json()).toEqual({
      status: 'ok',
      mode: 'keyless',
      provider: 'fmp',
      provider_label: 'FMP',
      available_providers: [
        { key: 'fmp', label: 'FMP' },
        { key: 'polygon_massive', label: 'Polygon / Massive' },
        { key: 'twelve_data', label: 'Twelve Data' },
      ],
      has_key: false,
      masked_key: null,
      source: 'none',
      config_revision: '',
    });

    const saveResponse = await window.fetch(`${API}/settings/market-data`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'twelve_data', api_key: 'promo-secret-1234' }),
    });
    expect(await saveResponse.json()).toMatchObject({
      status: 'ok',
      mode: 'user_key',
      provider: 'twelve_data',
      provider_label: 'Twelve Data',
      has_key: true,
      masked_key: `${'•'.repeat(13)}1234`,
      source: 'user',
      config_revision: '2026-06-28T16:00:00-04:00',
    });

    const savedResponse = await window.fetch(`${API}/settings/market-data`);
    expect(await savedResponse.json()).toMatchObject({
      mode: 'user_key',
      provider: 'twelve_data',
      has_key: true,
    });

    const deleteResponse = await window.fetch(`${API}/settings/market-data`, { method: 'DELETE' });
    expect(await deleteResponse.json()).toMatchObject({
      status: 'ok',
      mode: 'keyless',
      provider: 'twelve_data',
      has_key: false,
      masked_key: null,
      source: 'none',
    });
    expect(realFetch).not.toHaveBeenCalled();
    expect(window.localStorage.getItem('breaktwenty_promo_demo_store_v1')).not.toContain('promo-secret-1234');
  });

  it('keeps market strip reads, watchlist saves, and symbol search frozen inside promo mode', async () => {
    const realFetch = window.fetch;
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const stripResponse = await window.fetch(`${API}/market-data/strip`);
    const searchResponse = await window.fetch(`${API}/market-data/search?q=QQQ`);
    const watchlistResponse = await window.fetch(`${API}/market-data/strip/watchlist`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: [{ id: 'sp500' }, { id: 'nasdaq' }] }),
    });

    const strip = await stripResponse.json();
    const search = await searchResponse.json();
    const watchlist = await watchlistResponse.json();
    expect(strip).toMatchObject({
      status: 'ok',
      updated_at: '2026-06-28T16:00:00-04:00',
      watchlist: [{ id: 'sp500' }, { id: 'nasdaq' }, { id: 'dow' }, { id: 'usd_cad' }, { id: 'bitcoin' }],
    });
    expect(strip.tiles).toHaveLength(5);
    expect(strip.tiles.every((tile) => tile.as_of === '2026-06-28')).toBe(true);
    expect(strip.tiles.find((tile) => tile.id === 'sp500')).toMatchObject({
      value: 7354.02,
      reference_value: 7357.49,
      change: -3.47,
      change_pct: -0.05,
      points: [7312.74, 7361.98, 7374.04, 7361.52, 7375.14, 7355.99, 7349.41, 7337.81, 7354.02],
    });
    expect(strip.tiles.find((tile) => tile.id === 'nasdaq')).toMatchObject({
      value: 25297.62,
      change: -60.98,
      change_pct: -0.24,
    });
    expect(strip.tiles.find((tile) => tile.id === 'dow')).toMatchObject({
      value: 51876.11,
      change: -44.51,
      change_pct: -0.09,
    });
    expect(strip.tiles.find((tile) => tile.id === 'usd_cad')).toMatchObject({
      value: 1.4182,
      change: -0.0009,
      change_pct: -0.06,
    });
    expect(strip.tiles.find((tile) => tile.id === 'bitcoin')).toMatchObject({
      value: 59494.61,
      reference_value: 59953.44,
      change: -458.84,
      change_pct: -0.77,
    });
    expect(strip.tiles.find((tile) => tile.id === 'bitcoin').points).toHaveLength(25);
    expect(search.results).toContainEqual(expect.objectContaining({ id: 'custom:QQQ', symbol: 'QQQ' }));
    expect(watchlist).toEqual({ status: 'ok', watchlist: [{ id: 'sp500' }, { id: 'nasdaq' }] });
    const updatedStrip = await (await window.fetch(`${API}/market-data/strip`)).json();
    expect(updatedStrip.tiles.map((tile) => tile.id)).toEqual(['sp500', 'nasdaq']);
    expect(realFetch).not.toHaveBeenCalled();
  });

  it('persists settings-modal edit payloads for promo balances, cash, purchases, and linked loans', async () => {
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const initialAccountsResponse = await window.fetch(`${API}/accounts`);
    const initialAccounts = await initialAccountsResponse.json();
    const rbcChequing = initialAccounts.find((account) => account.provider === 'rbc' && account.account_type === 'chequing');
    const house = initialAccounts.find((account) => account.provider === 'real_estate');
    const familyLoan = initialAccounts.find((account) => account.provider === 'debt');
    const cashCad = initialAccounts.find((account) => account.provider === 'manual' && account.currency === 'CAD');

    await window.fetch(`${API}/accounts/${rbcChequing.id}/value`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: 14444, date: '2026-06-21' }),
    });
    await window.fetch(`${API}/accounts/${cashCad.id}/cash-opening`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: 2200, date: '2026-01-15' }),
    });
    await window.fetch(`${API}/accounts/${house.id}/purchase`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: 650000, date: '2021-05-15' }),
    });
    await window.fetch(`${API}/accounts/${house.id}/linked-loans`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ liability_account_ids: [familyLoan.id] }),
    });
    await window.fetch(`${API}/institutions/${house.institution_id}/sync-status`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sync_status: 'auth_required' }),
    });
    await window.fetch(`${API}/institutions/${house.institution_id}/logo`, {
      method: 'POST',
    });

    const accountsResponse = await window.fetch(`${API}/accounts`);
    const accounts = await accountsResponse.json();
    const institutionsResponse = await window.fetch(`${API}/institutions/all`);
    const institutions = await institutionsResponse.json();

    expect(accounts.find((account) => account.id === rbcChequing.id).balance).toBe(14444);
    expect(accounts.find((account) => account.id === cashCad.id).cash_opening).toMatchObject({ amount: 2200, date: '2026-01-15' });
    expect(accounts.find((account) => account.id === house.id).purchase).toMatchObject({ value: 650000, date: '2021-05-15' });
    expect(accounts.find((account) => account.id === familyLoan.id).secured_asset_account_id).toBe(house.id);
    expect(institutions.find((institution) => institution.id === house.institution_id)).toMatchObject({
      sync_status: 'auth_required',
      has_logo: true,
    });
  });

  it('keeps handled transaction reads local and rejects unsupported transaction writes locally', async () => {
    const realFetch = window.fetch;
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const listResponse = await window.fetch(`${API}/transactions?limit=1&offset=0`);
    const listBody = await listResponse.json();
    const transactionId = listBody.transactions[0].id;
    const editResponse = await window.fetch(`${API}/transactions/${transactionId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_description: 'Never write through' }),
    });
    const editBody = await editResponse.json();

    expect(listResponse.status).toBe(200);
    expect(listBody.transactions).toHaveLength(1);
    expect(editResponse.status).toBe(501);
    expect(editBody).toMatchObject({
      status: 'error',
      detail: `Promo demo does not support PATCH /transactions/${transactionId}.`,
    });
    expect(realFetch).not.toHaveBeenCalled();
  });

  it('cannot activate or install the promo environment outside development mode', () => {
    const realFetch = window.fetch;
    window.localStorage.setItem(PROMO_STORAGE_KEYS[0], '1');
    vi.stubEnv('DEV', false);

    installPromoDemoFetch();
    setPromoDemoActive(true);

    expect(isPromoDemoActive()).toBe(false);
    expect(window.fetch).toBe(realFetch);
  });

  it('falls through to the real fetch path while promo mode is off', async () => {
    installPromoDemoFetch();
    setPromoDemoActive(false);

    const response = await window.fetch(`${API}/accounts`);
    const body = await response.json();

    expect(body).toEqual({ real: true });
  });
});
