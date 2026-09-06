// Shared currency helpers.
//
// `makeCurrencyConverter` powers all cross-currency conversion through the
// primary base (whose `/fx-rates` rate is 1). `resolveGroupDisplayCurrency`
// picks the currency for an Accounts-page group: its sole currency when uniform,
// otherwise the primary. `NATIVE_VIEW_CURRENCY` is the sentinel meaning "show
// each row/position in its own currency".

export const NATIVE_VIEW_CURRENCY = 'NATIVE';

// Returns convert(amount, fromCurrency, toCurrency) using cross-rates through
// the primary base: amount / rate[from] * rate[to]. Missing rates fall back to
// the original amount.
export function makeCurrencyConverter(primaryCurrency, fxRates) {
  const primary = String(primaryCurrency || 'CAD').trim().toUpperCase();
  const rates = fxRates || {};
  return (amount, fromCurrency, toCurrency) => {
    const numeric = Number(amount);
    if (!Number.isFinite(numeric)) return 0;
    const from = String(fromCurrency || primary).trim().toUpperCase();
    const to = String(toCurrency || primary).trim().toUpperCase();
    if (from === to) return numeric;
    const fromRate = from === primary ? 1 : rates[from];
    const toRate = to === primary ? 1 : rates[to];
    if (!fromRate || !toRate) return numeric;
    return (numeric / fromRate) * toRate;
  };
}

export function buildFxHistoryIndex(fxHistory) {
  const index = {};
  Object.entries(fxHistory || {}).forEach(([currency, byDate]) => {
    const dates = Object.keys(byDate || {}).sort();
    index[String(currency || '').trim().toUpperCase()] = {
      dates,
      cadPerUnit: dates.map((day) => Number(byDate[day])),
    };
  });
  return index;
}

// CAD value of 1 unit of `currency` on `date`, using the nearest prior stored
// historical rate and falling back to the current `/fx-rates` cross-rate.
export function cadPerUnitAtDate(fxHistoryIndex, currency, date, currentRates) {
  const cur = String(currency || 'CAD').trim().toUpperCase();
  if (cur === 'CAD') return 1;
  const entry = fxHistoryIndex?.[cur];
  const key = date ? String(date).slice(0, 10) : null;
  if (key && entry?.dates?.length) {
    const { dates, cadPerUnit } = entry;
    let lo = 0;
    let hi = dates.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (dates[mid] <= key) lo = mid + 1;
      else hi = mid;
    }
    const idx = lo - 1;
    const historicalRate = idx >= 0 ? cadPerUnit[idx] : cadPerUnit[0];
    if (historicalRate) return historicalRate;
  }
  const rate = currentRates?.[cur];
  if (!rate) return 1;
  const cadPerPrimary = currentRates?.CAD;
  return cadPerPrimary ? cadPerPrimary / rate : 1 / rate;
}

export function makeHistoricalCurrencyConverter(primaryCurrency, fxRates, fxHistoryIndex) {
  const primary = String(primaryCurrency || 'CAD').trim().toUpperCase();
  const currentRates = fxRates || {};
  const historyIndex = fxHistoryIndex || {};
  return (amount, fromCurrency, toCurrency, date) => {
    const numeric = Number(amount);
    if (!Number.isFinite(numeric)) return 0;
    const from = String(fromCurrency || primary).trim().toUpperCase();
    const to = String(toCurrency || primary).trim().toUpperCase();
    if (from === to) return numeric;
    const cadFrom = cadPerUnitAtDate(historyIndex, from, date, currentRates);
    const cadTo = cadPerUnitAtDate(historyIndex, to, date, currentRates);
    if (!cadTo) return numeric;
    return (numeric * cadFrom) / cadTo;
  };
}

// Display currency for a group of items: the group's sole currency when every
// item shares one, otherwise the primary currency. For a specific (non-Native)
// view currency, that currency is used directly.
export function resolveGroupDisplayCurrency(groupItems, viewCurrency, primaryCurrency) {
  if (viewCurrency && viewCurrency !== NATIVE_VIEW_CURRENCY) return viewCurrency;
  const currencies = new Set();
  (groupItems || []).forEach((item) => {
    const code = String(item.currency || '').trim().toUpperCase();
    if (code) currencies.add(code);
  });
  if (currencies.size === 1) return [...currencies][0];
  return primaryCurrency;
}
