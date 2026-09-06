function isMissingNumericValue(value) {
  return value === null || value === undefined || Number.isNaN(Number(value));
}

function currencyFormatter(currency = 'CAD', options = {}) {
  return new Intl.NumberFormat('en-CA', {
    style: 'currency',
    currency,
    ...options,
  });
}

// Crypto codes have no ICU narrow symbol (ICU renders the bare code, e.g. "BTC"),
// so map the common ones to their glyphs and format the number ourselves.
const CRYPTO_CURRENCY_SYMBOLS = { BTC: '₿', ETH: 'Ξ' };

export function cryptoCurrencySymbol(currency) {
  return CRYPTO_CURRENCY_SYMBOLS[String(currency || '').trim().toUpperCase()] || null;
}

// Intl 'compact' notation tops out at "T" (trillion), so 1e17 prints as "109,618.65T" —
// commas plus a suffix, which reads like a small number (and made a correctly-summed
// institution total look un-dominated by its largest account). Extend with
// quadrillion-and-up suffixes so an extreme magnitude stays legible (e.g. "$109.6P").
// Returns null below 1e15 so normal values keep ICU's K/M/B/T compaction.
const COMPACT_BIG_UNITS = [
  { value: 1e24, suffix: 'Y' },
  { value: 1e21, suffix: 'Z' },
  { value: 1e18, suffix: 'E' },
  { value: 1e15, suffix: 'P' },
];
function bigCompactBody(abs) {
  const unit = COMPACT_BIG_UNITS.find((u) => abs >= u.value);
  if (!unit) return null;
  return `${(abs / unit.value).toLocaleString('en-CA', { minimumFractionDigits: 0, maximumFractionDigits: 2 })}${unit.suffix}`;
}

function formatCryptoMoney(amount, symbol, { compact = false } = {}) {
  const num = Number(amount);
  const big = compact ? bigCompactBody(Math.abs(num)) : null;
  const body = big || new Intl.NumberFormat('en-CA', {
    notation: compact ? 'compact' : 'standard',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(Math.abs(num));
  return `${num < 0 ? '-' : ''}${symbol}${body}`;
}

// Some locales prefix a narrow currency symbol with a 2-letter country code
// (e.g. "US$" for USD, "CA$" for CAD). Strip that leading code while preserving
// real letter-bearing symbols like "Kč" (CZK) and "CHF" — only drop two
// uppercase letters when they immediately precede a non-letter glyph.
export function stripCurrencyCodePrefix(symbol) {
  return String(symbol).replace(/^[A-Z]{2}(?=[^\w\s])/, '');
}

export function formatMoney(amount, currency = 'CAD') {
  const crypto = cryptoCurrencySymbol(currency);
  if (crypto) return formatCryptoMoney(amount, crypto);
  // `narrowSymbol` is the only display mode that resolves to the real symbol
  // (e.g. CZK → "Kč", with ICU's natural trailing space) across both Node and
  // Chromium ICU; the default `symbol` mode renders CZK as the ISO code "CZK".
  return currencyFormatter(currency, {
    currencyDisplay: 'narrowSymbol',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(amount));
}

export function formatMoneyOrDash(amount, currency = 'CAD') {
  if (isMissingNumericValue(amount)) {
    return '—';
  }
  return formatMoney(Number(amount), currency);
}

export function formatMoneyNarrow(amount, currency = 'CAD') {
  return formatMoney(amount, currency);
}

export function formatMoneyNarrowOrDash(amount, currency = 'CAD') {
  if (isMissingNumericValue(amount)) {
    return '—';
  }
  return formatMoneyNarrow(Number(amount), currency);
}

export function formatTableMoney(amount, currency = 'CAD') {
  if (isMissingNumericValue(amount)) {
    return '—';
  }
  const crypto = cryptoCurrencySymbol(currency);
  if (crypto) return formatCryptoMoney(amount, crypto);

  return currencyFormatter(currency, {
    currencyDisplay: 'narrowSymbol',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).formatToParts(Number(amount))
    .map((part) => (part.type === 'currency' ? stripCurrencyCodePrefix(part.value) : part.value))
    .join('');
}

export function formatSignedMoneyNarrow(amount, currency = 'CAD') {
  if (isMissingNumericValue(amount)) {
    return '—';
  }

  const value = Number(amount);
  const formatted = formatMoneyNarrow(Math.abs(value), currency);
  if (Math.abs(value) < 0.005) {
    return formatted;
  }
  return `${value > 0 ? '+' : '-'}${formatted}`;
}

export function currencySymbolFor(currency) {
  const crypto = cryptoCurrencySymbol(currency);
  if (crypto) return crypto;
  try {
    const parts = new Intl.NumberFormat('en-CA', {
      style: 'currency',
      currency,
      currencyDisplay: 'narrowSymbol',
    }).formatToParts(0);
    const symbolPart = parts.find((part) => part.type === 'currency');
    if (symbolPart && symbolPart.value) {
      return stripCurrencyCodePrefix(symbolPart.value);
    }
  } catch (err) {
    // Fall through to default
  }
  return '$';
}

export function formatSignedDollarAmount(amount, currency = 'CAD') {
  const numericAmount = Number(amount);
  if (!Number.isFinite(numericAmount)) return '—';
  const formatted = Math.abs(numericAmount).toLocaleString('en-CA', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = numericAmount < 0 ? '-' : numericAmount > 0 ? '+' : '';
  const symbol = currencySymbolFor(currency);
  // Letter-based symbols (e.g. "Kč", "CHF") take a separating space, matching
  // ICU narrowSymbol; glyphs ($, €, £) glue to the number. \p{L} matches the
  // accented "č", which an ASCII-only [A-Za-z] test would miss.
  const spacer = /\p{L}$/u.test(symbol) ? ' ' : '';
  return `${sign}${symbol}${spacer}${formatted}`;
}

export function formatCompactMoney(amount, currency = 'CAD') {
  const crypto = cryptoCurrencySymbol(currency);
  if (crypto) return formatCryptoMoney(amount, crypto, { compact: true });
  const value = Number(amount);
  const big = bigCompactBody(Math.abs(value));
  if (big) {
    // Past ICU's trillion ceiling: build it ourselves so it reads "$109.6P", not
    // "$109,618.65T". Match formatMoney's spacing (letter symbols take a space).
    const symbol = currencySymbolFor(currency);
    const spacer = /\p{L}$/u.test(symbol) ? ' ' : '';
    return `${value < 0 ? '-' : ''}${symbol}${spacer}${big}`;
  }
  return currencyFormatter(currency, {
    notation: 'compact',
    currencyDisplay: 'narrowSymbol',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatSignedCompactMoney(amount, currency = 'CAD') {
  if (isMissingNumericValue(amount)) {
    return '—';
  }
  const value = Number(amount);
  const formatted = formatCompactMoney(Math.abs(value), currency);
  if (Math.abs(value) < 0.005) {
    return formatted;
  }
  return `${value > 0 ? '+' : '-'}${formatted}`;
}

export function formatQuantity(quantity) {
  if (isMissingNumericValue(quantity)) {
    return '—';
  }

  return new Intl.NumberFormat('en-CA', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 4,
  }).format(Number(quantity));
}

export function formatCompactNumber(amount) {
  if (isMissingNumericValue(amount)) {
    return '—';
  }

  return new Intl.NumberFormat('en-CA', {
    notation: 'compact',
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  }).format(Number(amount));
}

export function formatPercent(percent) {
  if (isMissingNumericValue(percent)) {
    return '—';
  }
  return `${Number(percent).toFixed(1)}%`;
}

export function formatSignedPercent(percent) {
  if (isMissingNumericValue(percent)) {
    return '—';
  }
  const value = Number(percent);
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}%`;
}
