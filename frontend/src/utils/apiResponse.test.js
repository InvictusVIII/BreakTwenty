import {
  isPlainObject,
  readCashFlowResponse,
  readJsonResponse,
  readTransactionCollectionResponse,
} from './apiResponse';

function response(payload, { ok = true, status = 200 } = {}) {
  return { ok, status, json: vi.fn().mockResolvedValue(payload) };
}

describe('API response helpers', () => {
  it('returns validated JSON on success', async () => {
    await expect(readJsonResponse(response({ rows: [] }), {
      label: 'Rows',
      validate: (payload) => Array.isArray(payload.rows),
    })).resolves.toEqual({ rows: [] });
  });

  it('surfaces API detail on non-2xx JSON', async () => {
    await expect(readJsonResponse(response({ detail: 'Not authorized' }, { ok: false, status: 401 }), {
      label: 'Rows',
    })).rejects.toMatchObject({ name: 'ApiResponseError', message: 'Not authorized', status: 401 });
  });

  it('rejects malformed successful payloads', async () => {
    await expect(readJsonResponse(response({ rows: null }), {
      label: 'Rows',
      validate: (payload) => Array.isArray(payload.rows),
    })).rejects.toMatchObject({ name: 'ApiResponseError' });
  });

  it('recognizes only plain object payloads', () => {
    expect(isPlainObject({})).toBe(true);
    expect(isPlainObject([])).toBe(false);
    expect(isPlainObject(null)).toBe(false);
  });

  it('keeps an empty transaction result distinct from a failed response', async () => {
    await expect(readTransactionCollectionResponse(response({ transactions: [], total: 0 })))
      .resolves.toEqual({ transactions: [], total: 0 });
    await expect(readTransactionCollectionResponse(response(
      { detail: 'Transactions unavailable', transactions: [], total: 0 },
      { ok: false, status: 503 },
    ))).rejects.toMatchObject({ message: 'Transactions unavailable', status: 503 });
  });

  it('rejects transaction payloads outside the current collection contract', async () => {
    await expect(readTransactionCollectionResponse(response([{ id: 1 }])))
      .rejects.toMatchObject({ message: 'Transactions returned an unexpected payload.' });
    await expect(readTransactionCollectionResponse(response({ transactions: [] })))
      .rejects.toMatchObject({ message: 'Transactions returned an unexpected payload.' });
    await expect(readTransactionCollectionResponse(response({ transactions: [], total: '0' })))
      .rejects.toMatchObject({ message: 'Transactions returned an unexpected payload.' });
  });

  it('keeps a valid empty cash-flow payload distinct from an API error', async () => {
    const emptyCashFlow = {
      period: { start: null, end: null, currency: 'CAD' },
      totals: { income: 0, expense: 0, net: 0 },
      income_breakdown: [],
      expense_breakdown: [],
      needs_review: {
        category_id: null,
        transaction_count: 0,
      },
      investment_activity: {
        buys: 0,
        sells: 0,
        dividends: 0,
        interest: 0,
        withholding_tax: 0,
        expired: 0,
        transfers: 0,
        deposits: 0,
        withdrawals: 0,
        cc_payments: 0,
        loan_payments: 0,
        loan_advances: 0,
        realized_pnl: 0,
        breakdown: [],
      },
      trend_12_months: [],
      recurring: [],
      previous_totals: null,
    };
    await expect(readCashFlowResponse(response(emptyCashFlow))).resolves.toEqual(emptyCashFlow);
    await expect(readCashFlowResponse(response(
      { ...emptyCashFlow, detail: 'Cash flow unavailable' },
      { ok: false, status: 500 },
    ))).rejects.toMatchObject({ message: 'Cash flow unavailable', status: 500 });
    await expect(readCashFlowResponse(response({
      ...emptyCashFlow,
      trend_12_months: {},
    }))).rejects.toMatchObject({ message: 'Cash flow returned an unexpected payload.' });
    const missingRecurring = { ...emptyCashFlow };
    delete missingRecurring.recurring;
    await expect(readCashFlowResponse(response(missingRecurring)))
      .rejects.toMatchObject({ message: 'Cash flow returned an unexpected payload.' });
    await expect(readCashFlowResponse(response({
      ...emptyCashFlow,
      totals: {},
    }))).rejects.toMatchObject({ message: 'Cash flow returned an unexpected payload.' });
    await expect(readCashFlowResponse(response({
      ...emptyCashFlow,
      needs_review: { ...emptyCashFlow.needs_review, transaction_count: '0' },
    }))).rejects.toMatchObject({ message: 'Cash flow returned an unexpected payload.' });
    await expect(readCashFlowResponse(response({
      ...emptyCashFlow,
      investment_activity: { ...emptyCashFlow.investment_activity, buys: '0' },
    }))).rejects.toMatchObject({ message: 'Cash flow returned an unexpected payload.' });
  });
});
