function payloadMessage(payload) {
  if (typeof payload === 'string') return payload.trim();
  if (!payload || typeof payload !== 'object') return '';
  const detail = payload.detail;
  if (typeof detail === 'string') return detail.trim();
  if (Array.isArray(detail)) {
    return detail
      .map((entry) => (typeof entry?.msg === 'string' ? entry.msg.trim() : ''))
      .filter(Boolean)
      .join('; ');
  }
  return String(payload.message || '').trim();
}

class ApiResponseError extends Error {
  constructor(message, { status = 0, payload = null } = {}) {
    super(message);
    this.name = 'ApiResponseError';
    this.status = status;
    this.payload = payload;
  }
}

export async function readJsonResponse(response, {
  label = 'Request',
  validate,
} = {}) {
  if (!response || typeof response.json !== 'function') {
    throw new ApiResponseError(`${label} returned no response.`);
  }

  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new ApiResponseError(`${label} returned invalid JSON.`, { status: response.status || 0 });
  }

  if (response.ok === false) {
    throw new ApiResponseError(
      payloadMessage(payload) || `${label} failed${response.status ? ` (${response.status})` : ''}.`,
      { status: response.status || 0, payload },
    );
  }

  if (validate && !validate(payload)) {
    throw new ApiResponseError(`${label} returned an unexpected payload.`, {
      status: response.status || 0,
      payload,
    });
  }

  return payload;
}

export async function readTransactionCollectionResponse(response, {
  label = 'Transactions',
} = {}) {
  const payload = await readJsonResponse(response, {
    label,
    validate: (value) => (
      isPlainObject(value)
      && Array.isArray(value.transactions)
      && Number.isInteger(value.total)
      && value.total >= 0
    ),
  });
  return payload;
}

export async function readCashFlowResponse(response, { label = 'Cash flow' } = {}) {
  const hasFiniteNumbers = (value, keys) => (
    isPlainObject(value)
    && keys.every((key) => Number.isFinite(value[key]))
  );
  return readJsonResponse(response, {
    label,
    validate: (value) => (
      isPlainObject(value)
      && isPlainObject(value.period)
      && typeof value.period.currency === 'string'
      && value.period.currency.trim() !== ''
      && hasFiniteNumbers(value.totals, ['income', 'expense', 'net'])
      && Array.isArray(value.income_breakdown)
      && Array.isArray(value.expense_breakdown)
      && isPlainObject(value.needs_review)
      && Number.isInteger(value.needs_review.transaction_count)
      && value.needs_review.transaction_count >= 0
      && (value.needs_review.category_id === null
        || Number.isInteger(value.needs_review.category_id))
      && hasFiniteNumbers(value.investment_activity, [
        'buys',
        'sells',
        'dividends',
        'interest',
        'withholding_tax',
        'expired',
        'transfers',
        'deposits',
        'withdrawals',
        'cc_payments',
        'loan_payments',
        'loan_advances',
        'realized_pnl',
      ])
      && Array.isArray(value.investment_activity.breakdown)
      && Array.isArray(value.trend_12_months)
      && Array.isArray(value.recurring)
      && (
        value.previous_totals === null
        || hasFiniteNumbers(value.previous_totals, ['income', 'expense', 'net'])
      )
    ),
  });
}

export function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
