import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { API } from '../config';
import { getSeedCategory } from '../constants/seedCategoryTaxonomy';
import { readCashFlowResponse } from '../utils/apiResponse';
import { installPromoDemoFetch, setPromoDemoActive } from './promoDemoEnvironment';
import { installTourDemoFetch, setTourDemoActive } from './tourDemoData';

const TOUR_DEMO_WINDOW_KEYS = [
  '__breaktwentyTourDemoFetchState',
  '__breaktwentyTourDemoOriginalFetch',
  '__breaktwentyTourDemoFetchWrapper',
];

function resetTourDemoFetchGlobals() {
  TOUR_DEMO_WINDOW_KEYS.forEach((key) => {
    delete window[key];
  });
  window.localStorage.removeItem('breaktwenty_promo_demo_active_v1');
  window.localStorage.removeItem('breaktwenty_promo_demo_store_v1');
}

describe('tour demo fetch shim', () => {
  beforeEach(() => {
    resetTourDemoFetchGlobals();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ real: true })))));
  });

  afterEach(() => {
    setTourDemoActive(false);
    setPromoDemoActive(false);
    resetTourDemoFetchGlobals();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('serves populated balance history while the welcome tour is active', async () => {
    installTourDemoFetch();
    setTourDemoActive(true);

    const response = await window.fetch(`${API}/accounts/balance-history`);
    const body = await response.json();

    expect(Object.keys(body).length).toBeGreaterThan(0);
    expect(Object.keys(body['8']).length).toBeGreaterThan(1000);
    expect(body['8']['2026-06-15']).toBe(612000);
  });

  it('serves the current cash-flow response contract', async () => {
    installTourDemoFetch();
    setTourDemoActive(true);

    const cashFlow = await readCashFlowResponse(await window.fetch(`${API}/cash-flow`));

    expect(cashFlow.investment_activity).toMatchObject({
      loan_advances: expect.any(Number),
      realized_pnl: expect.any(Number),
    });
  });

  it('covers app companion endpoints needed for the populated tour shell', async () => {
    installTourDemoFetch();
    setTourDemoActive(true);

    const [scopeResponse, accountResponse, syncResponse, fxResponse] = await Promise.all([
      window.fetch(`${API}/institutions/all`),
      window.fetch(`${API}/institutions/101/accounts`),
      window.fetch(`${API}/sync/activity`),
      window.fetch(`${API}/fx-rates/history`),
    ]);

    const [scope, accounts, sync, fxHistory] = await Promise.all([
      scopeResponse.json(),
      accountResponse.json(),
      syncResponse.json(),
      fxResponse.json(),
    ]);

    expect(scope.map((institution) => institution.id)).toContain(101);
    expect(accounts.map((account) => account.id)).toEqual([1, 2]);
    expect(sync).toEqual({ active: [] });
    expect(fxHistory.rates.USD['2026-06-15']).toBe(1.3718);
  });

  it.each([
    {
      route: '/networth',
      assertContract: (body) => {
        expect(body.current).toEqual({
          total_assets: expect.any(Number),
          total_liabilities: expect.any(Number),
          net_worth: expect.any(Number),
          currency: 'CAD',
          date: null,
        });
        expect(body.history).toHaveLength(1500);
        expect(body.history.at(-1)).toMatchObject({
          date: '2026-06-15',
          total_assets: expect.any(Number),
          total_liabilities: expect.any(Number),
          net_worth: body.current.net_worth,
        });
      },
    },
    {
      route: '/accounts',
      assertContract: (body) => {
        expect(body).toEqual(expect.arrayContaining([
          expect.objectContaining({
            id: 5,
            institution: 'Questrade',
            institution_id: 103,
            provider: 'questrade',
            name: 'Margin',
            account_type: 'margin',
            currency: 'CAD',
            is_liability: false,
            balance: expect.any(Number),
            last_synced: expect.any(String),
            added_at: expect.any(String),
            connected_at: expect.any(String),
            is_imported: false,
            has_transactions: true,
          }),
        ]));
      },
    },
    {
      route: '/institutions',
      assertContract: (body) => {
        expect(body).toEqual(expect.arrayContaining([
          {
            id: 101,
            name: 'TD',
            type: 'bank',
            provider: 'td',
            category: null,
            has_logo: false,
            sync_status: 'ok',
            added_at: expect.any(String),
          },
        ]));
      },
    },
    {
      route: '/accounts/transaction-import-status',
      assertContract: (body) => {
        expect(body.generated_at).toEqual(expect.any(String));
        expect(body.institutions).toEqual(expect.arrayContaining([
          expect.objectContaining({
            institution_id: 101,
            institution: 'TD',
            provider: 'td',
            status: 'incremental',
            history_status: 'complete',
            transaction_import_job_status: null,
            transaction_import_job_id: null,
            accounts: expect.arrayContaining([
              expect.objectContaining({
                account_id: 2,
                account_name: 'Chequing',
                account_type: 'chequing',
                provider: 'td',
                status: 'incremental',
                history_status: 'complete',
                current_fetch_status: null,
                transaction_count: expect.any(Number),
              }),
            ]),
          }),
        ]));
      },
    },
    {
      route: '/accounts/5/holdings',
      assertContract: (body) => {
        expect(body).toEqual(expect.arrayContaining([
          expect.objectContaining({
            id: 501,
            symbol: 'VFV.TO',
            name: expect.any(String),
            quantity: expect.any(Number),
            market_value: expect.any(Number),
            average_cost: expect.any(Number),
            last_price: expect.any(Number),
            contract_multiplier: null,
            change_pct: expect.any(Number),
            daily_pnl: expect.any(Number),
            currency: 'CAD',
            sector: 'ETF',
            instrument_kind: 'etf',
          }),
        ]));
      },
    },
  ])('serves the current $route response contract', async ({ route, assertContract }) => {
    installTourDemoFetch();
    setTourDemoActive(true);

    const response = await window.fetch(`${API}${route}`);
    assertContract(await response.json());
  });

  it('serves the paged transaction-table contract with stable ordering', async () => {
    installTourDemoFetch();
    setTourDemoActive(true);

    const [firstResponse, secondResponse] = await Promise.all([
      window.fetch(`${API}/transactions?limit=3&offset=0`),
      window.fetch(`${API}/transactions?limit=3&offset=3`),
    ]);
    const [firstPage, secondPage] = await Promise.all([
      firstResponse.json(),
      secondResponse.json(),
    ]);

    expect(firstPage.total).toBeGreaterThan(6);
    expect(secondPage.total).toBe(firstPage.total);
    expect(firstPage.transactions).toHaveLength(3);
    expect(secondPage.transactions).toHaveLength(3);
    expect(firstPage.transactions.map((transaction) => transaction.id)).not.toEqual(
      secondPage.transactions.map((transaction) => transaction.id),
    );
    expect(firstPage.transactions[0]).toMatchObject({
      id: expect.any(Number),
      date: expect.any(String),
      amount: expect.any(Number),
      currency: expect.any(String),
      description: expect.any(String),
      account_name: expect.any(String),
      institution_name: expect.any(String),
    });
    expect(
      firstPage.transactions.map((transaction) => transaction.date),
    ).toEqual(
      firstPage.transactions.map((transaction) => transaction.date).sort().reverse(),
    );
  });

  it('serves current frontend taxonomy metadata on every dummy transaction', async () => {
    installTourDemoFetch();
    setTourDemoActive(true);

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

    expect(categories).toHaveLength(110);
    transactions.forEach((transaction) => {
      expect(categoriesBySeedKey[transaction.category.seed_key]).toEqual(transaction.category);
    });
  });

  it.each([
    {
      filter: `category_ids=${getSeedCategory('groceries').id}`,
      expectedTotal: 6,
      assertRows: (transactions) => {
        expect(transactions.every(
          (transaction) => transaction.category?.id === getSeedCategory('groceries').id,
        )).toBe(true);
      },
    },
    {
      filter: 'search=AmErIcAn%20ExPrEsS',
      expectedTotal: 2,
      assertRows: (transactions) => {
        expect(transactions.every(
          (transaction) => transaction.institution_name === 'American Express',
        )).toBe(true);
      },
    },
  ])('filters transactions for $filter', async ({ filter, expectedTotal, assertRows }) => {
    installTourDemoFetch();
    setTourDemoActive(true);

    const response = await window.fetch(`${API}/transactions?${filter}`);
    const body = await response.json();

    expect(body.total).toBe(expectedTotal);
    expect(body.transactions).toHaveLength(expectedTotal);
    assertRows(body.transactions);
  });

  it('falls through to the real fetch for an unhandled API route while demo mode is active', async () => {
    const realFetch = window.fetch;
    const init = { headers: { Accept: 'application/json' } };
    installTourDemoFetch();
    setTourDemoActive(true);

    const response = await window.fetch(`${API}/settings/unhandled-by-tour`, init);

    expect(await response.json()).toEqual({ real: true });
    expect(realFetch).toHaveBeenCalledWith(`${API}/settings/unhandled-by-tour`, init);
  });

  it('switches between demo and real fetch without stacking wrappers', async () => {
    const realFetch = window.fetch;
    installTourDemoFetch();
    const installedFetch = window.fetch;
    installTourDemoFetch();
    expect(window.fetch).toBe(installedFetch);

    setTourDemoActive(true);
    const demoResponse = await window.fetch(`${API}/accounts`);
    expect(await demoResponse.json()).toEqual(expect.arrayContaining([
      expect.objectContaining({ institution: 'TD' }),
    ]));
    expect(realFetch).not.toHaveBeenCalled();

    setTourDemoActive(false);
    const realResponse = await window.fetch(`${API}/accounts`);
    expect(await realResponse.json()).toEqual({ real: true });
    expect(realFetch).toHaveBeenCalledOnce();

    setTourDemoActive(true);
    const resumedDemoResponse = await window.fetch(`${API}/accounts`);
    expect(Array.isArray(await resumedDemoResponse.json())).toBe(true);
    expect(realFetch).toHaveBeenCalledOnce();
  });

  it('does not replace an outer promo wrapper when the tour installer runs again', async () => {
    const realFetch = window.fetch;
    installTourDemoFetch();
    installPromoDemoFetch();
    setPromoDemoActive(true);
    const promoFetch = window.fetch;

    installTourDemoFetch();

    expect(window.fetch).toBe(promoFetch);
    const response = await window.fetch(`${API}/accounts`);
    const accounts = await response.json();
    expect(accounts).toEqual(expect.arrayContaining([
      expect.objectContaining({ provider: 'real_estate', balance: 790000 }),
    ]));
    expect(realFetch).not.toHaveBeenCalled();
  });
});
