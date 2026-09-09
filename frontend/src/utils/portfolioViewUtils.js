import { cryptoCurrencySymbol, formatMoney, stripCurrencyCodePrefix } from './format';
import { ACCOUNT_TYPE_LABELS } from '../constants/providers';
import { getAppliedBreakTwentyChartColors } from '../theme/applyTheme';
import { getAppNow } from './appClock';
import { persistentStorage } from './persistentStorage';

const PORTFOLIO_CHART_COLORS_STORAGE_KEY = 'breaktwenty_dashboard_chart_colors_v2';
export const DEFAULT_PORTFOLIO_NET_WORTH_COLOR = getAppliedBreakTwentyChartColors().netWorth;
export const EMPTY_PORTFOLIO_CHART_COLORS = {};

export const cloneScopeInstitutions = (institutionList) => institutionList.map((institution) => ({
  ...institution,
  accountIds: [...institution.accountIds],
  accounts: (institution.accounts || []).map((account) => ({ ...account })),
}));
export function normalizeChartColor(value, fallback = null) {
  const color = String(value || '').trim();
  return /^#[0-9a-f]{6}$/i.test(color) ? color.toLowerCase() : fallback;
}

export function resolvePortfolioNetWorthChartColor(portfolioChartColors, themeChartColors = getAppliedBreakTwentyChartColors()) {
  return normalizeChartColor(portfolioChartColors?.netWorth, themeChartColors.netWorth);
}

export function clampColorValue(value, min = 0, max = 1) {
  return Math.min(max, Math.max(min, value));
}

export function hexToRgb(hex) {
  const normalizedColor = normalizeChartColor(hex, DEFAULT_PORTFOLIO_NET_WORTH_COLOR);
  const raw = normalizedColor.replace('#', '');
  return {
    r: parseInt(raw.slice(0, 2), 16),
    g: parseInt(raw.slice(2, 4), 16),
    b: parseInt(raw.slice(4, 6), 16),
  };
}

function rgbToHex(r, g, b) {
  return `#${[r, g, b].map((value) => Math.round(value).toString(16).padStart(2, '0')).join('')}`;
}

export function rgbToHsv({ r, g, b }) {
  const red = r / 255;
  const green = g / 255;
  const blue = b / 255;
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  const delta = max - min;
  let hue = 0;

  if (delta !== 0) {
    if (max === red) {
      hue = 60 * (((green - blue) / delta) % 6);
    } else if (max === green) {
      hue = 60 * (((blue - red) / delta) + 2);
    } else {
      hue = 60 * (((red - green) / delta) + 4);
    }
  }

  return {
    h: hue < 0 ? hue + 360 : hue,
    s: max === 0 ? 0 : delta / max,
    v: max,
  };
}

export function hsvToHex(h, s, v) {
  const hue = ((h % 360) + 360) % 360;
  const chroma = v * s;
  const x = chroma * (1 - Math.abs(((hue / 60) % 2) - 1));
  const m = v - chroma;
  let red = 0;
  let green = 0;
  let blue = 0;

  if (hue < 60) {
    red = chroma;
    green = x;
  } else if (hue < 120) {
    red = x;
    green = chroma;
  } else if (hue < 180) {
    green = chroma;
    blue = x;
  } else if (hue < 240) {
    green = x;
    blue = chroma;
  } else if (hue < 300) {
    red = x;
    blue = chroma;
  } else {
    red = chroma;
    blue = x;
  }

  return rgbToHex((red + m) * 255, (green + m) * 255, (blue + m) * 255);
}

export function loadPortfolioChartColors() {
  try {
    const parsed = JSON.parse(persistentStorage.getItem(PORTFOLIO_CHART_COLORS_STORAGE_KEY) || '{}');
    const normalizeNetWorthColor = (color) => normalizeChartColor(color);
    const normalizeColorMap = (source) => {
      const colors = {};

      Object.entries(source || {}).forEach(([key, color]) => {
        const normalizedColor = normalizeChartColor(color);
        if (key && normalizedColor) {
          colors[String(key)] = normalizedColor;
        }
      });

      return colors;
    };
    return {
      netWorth: normalizeNetWorthColor(parsed?.netWorth),
      assetInstitutions: normalizeColorMap(parsed?.assetInstitutions),
      liabilityInstitutions: normalizeColorMap(parsed?.liabilityInstitutions),
      liabilityTypes: normalizeColorMap(parsed?.liabilityTypes),
    };
  } catch {
    return {
      netWorth: null,
      assetInstitutions: {},
      liabilityInstitutions: {},
      liabilityTypes: {},
    };
  }
}

export function persistPortfolioChartColors(colors) {
  try {
    persistentStorage.setItem(PORTFOLIO_CHART_COLORS_STORAGE_KEY, JSON.stringify(colors));
  } catch (err) {
    void err;
  }
}

export function getPortfolioPaletteColor(palette, index) {
  return palette[index % palette.length];
}

export function getAllocationInstitutionColorKeys(items, legendRowLimit = Infinity, otherInstitutionsColorKey = null) {
  const keys = items.map((item) => item.key);
  if (otherInstitutionsColorKey && items.length > legendRowLimit) {
    keys.push(otherInstitutionsColorKey);
  }
  return keys;
}

export function formatPortfolioAccountTypeLabel(type) {
  return ACCOUNT_TYPE_LABELS[type] || String(type || 'other').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

export const PORTFOLIO_TIMEFRAMES = [
  { label: '1D', days: 1, menuLabel: '1D', triggerLabel: '1 Day' },
  { label: '5D', days: 5, menuLabel: '5D', triggerLabel: '5 Days' },
  { label: '30D', days: 30, menuLabel: '30D', triggerLabel: '30 Days' },
  { label: '90D', days: 90, menuLabel: '90D', triggerLabel: '90 Days' },
  { label: '6M', days: 180, menuLabel: '6M', triggerLabel: '6 Months' },
  { label: 'YTD', days: 'ytd', menuLabel: 'YTD', triggerLabel: 'Year to Date' },
  { label: '1Y', days: 365, menuLabel: '1Y', triggerLabel: '1 Year' },
  { label: 'All', days: null, menuLabel: 'All', triggerLabel: 'All Time' },
];
export const DEFAULT_PORTFOLIO_TIMEFRAME = 'All';
export const PORTFOLIO_CUSTOM_TIMEFRAME_KEY = 'CUSTOM';

const TIMEFRAME_DAYS = PORTFOLIO_TIMEFRAMES.reduce((acc, timeframe) => {
  acc[timeframe.label] = timeframe.days;
  return acc;
}, {});

function findDateOnOrBefore(dates, cutoffStr) {
  if (!cutoffStr) return null;
  for (let i = dates.length - 1; i >= 0; i--) {
    if (dates[i] <= cutoffStr) return dates[i];
  }
  return null;
}

function dateKey(value) {
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) return null;
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
  }
  return value ? String(value).slice(0, 10) : null;
}

export function getAccountBalanceAtDate(accountId, balanceHistory, cutoffDate, fallback = 0) {
  const cutoffKey = dateKey(cutoffDate);
  if (!cutoffKey) return fallback;
  const history = balanceHistory[String(accountId)];
  const dates = history ? Object.keys(history).sort() : [];
  if (dates.length === 0) return fallback;
  const balanceKey = findDateOnOrBefore(dates, cutoffKey);
  return balanceKey ? Number(history[balanceKey]) || 0 : fallback;
}

export function getAccountDisplayBalance(account, balanceHistory, timeframe, customDateRange = null) {
  const currentBalance = Number(account?.balance) || 0;
  if (timeframe !== PORTFOLIO_CUSTOM_TIMEFRAME_KEY) return currentBalance;
  const endDate = dateKey(customDateRange?.end || customDateRange?.start);
  if (!endDate) return currentBalance;
  return getAccountBalanceAtDate(account?.id, balanceHistory, endDate, 0);
}

export const CHANGE_BASELINE_EPSILON = 0.005;

export function isEffectivelyZeroMoney(value) {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) && Math.abs(numberValue) < CHANGE_BASELINE_EPSILON;
}

export function normalizeMoneyDelta(value) {
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue)) return null;
  return isEffectivelyZeroMoney(numberValue) ? 0 : numberValue;
}

export function changePercentFromBaseline(diff, startBalance) {
  const numericDiff = normalizeMoneyDelta(diff);
  const numericStart = Number(startBalance);
  if (numericDiff === null || !Number.isFinite(numericStart)) return null;
  if (isEffectivelyZeroMoney(numericStart)) {
    return numericDiff === 0 ? 0 : null;
  }
  return Number(((numericDiff / Math.abs(numericStart)) * 100).toFixed(1));
}

export function getAggregateHistoryMetricChange(history, metricKey) {
  if (!Array.isArray(history) || history.length === 0) return null;
  const start = normalizeMoneyDelta(history[0]?.[metricKey]);
  const end = normalizeMoneyDelta(history[history.length - 1]?.[metricKey]);
  if (start === null || end === null) return null;
  const diff = normalizeMoneyDelta(end - start);
  if (diff === null) return null;
  return {
    diff,
    pct: changePercentFromBaseline(diff, start),
    positive: diff >= 0,
    start,
    end,
  };
}

function zeroChange(startDate = null, endDate = null) {
  return {
    diff: 0,
    pct: 0,
    positive: true,
    startBalance: 0,
    endBalance: 0,
    startDate: dateKey(startDate),
    endDate: dateKey(endDate),
  };
}

function buildAccountChange(startBalance, endBalance, isLiability, startDate = null, endDate = null) {
  const start = normalizeMoneyDelta(startBalance);
  const end = normalizeMoneyDelta(endBalance);
  if (start === null || end === null) return null;
  const rawDiff = end - start;
  const diff = normalizeMoneyDelta(isLiability ? -rawDiff : rawDiff);
  const pct = changePercentFromBaseline(diff, start);
  return {
    diff,
    pct,
    positive: diff >= 0,
    startBalance: start,
    endBalance: end,
    startDate: dateKey(startDate),
    endDate: dateKey(endDate),
  };
}

export function getAccountChange(accountId, balanceHistory, timeframe, currentBalance, isLiability = false, customDateRange = null, isImported = false) {
  // Per-account change = the account's own balance series over the window (graph-delta: value at the
  // window end − value at the window start, falling back to its first recorded value when the window
  // opens before the account exists). Statement-imported (defunct) accounts stay graph context only:
  // return "—" (never change-tracked, dropped from rollups).
  if (isImported) return null;
  const history = balanceHistory[String(accountId)];
  const dates = history ? Object.keys(history).sort() : [];
  if (dates.length === 0) {
    // No recorded history (e.g. every value point was deleted from an asset): a known
    // current balance has simply not changed — show $0.00 rather than a dead "—".
    return Number.isFinite(currentBalance) ? buildAccountChange(currentBalance, currentBalance, isLiability, null, getAppNow()) : null;
  }
  if (timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY) {
    const requestedStart = dateKey(customDateRange?.start) || dates[0];
    const endDate = dateKey(customDateRange?.end);
    const priorStartKey = findDateOnOrBefore(dates, requestedStart);
    const startKey = priorStartKey || dates[0];
    const displayedStartDate = priorStartKey ? requestedStart : startKey;
    const endKey = endDate ? findDateOnOrBefore(dates, endDate) : null;
    if (endDate && (!endKey || endKey < startKey)) return zeroChange(requestedStart, endDate);
    const startBalance = history[startKey];
    const endBalance = endKey ? history[endKey] : currentBalance;
    return buildAccountChange(startBalance, endBalance, isLiability, displayedStartDate, endDate || getAppNow());
  }
  const days = TIMEFRAME_DAYS[timeframe];
  if (days === null) {
    const startKey = dates[0];
    const startBalance = history[startKey];
    return buildAccountChange(startBalance, currentBalance, isLiability, startKey, getAppNow());
  }
  let cutoffStr;
  if (days === "ytd") {
    cutoffStr = getAppNow().getFullYear() + "-01-01";
  } else {
    const cutoffDate = getAppNow();
    if (days === 1) {
      cutoffDate.setDate(cutoffDate.getDate() - 1);
    } else {
      cutoffDate.setDate(cutoffDate.getDate() - days);
    }
    cutoffStr = cutoffDate.getFullYear() + '-' + String(cutoffDate.getMonth() + 1).padStart(2, '0') + '-' + String(cutoffDate.getDate()).padStart(2, '0');
  }
  const priorStartKey = findDateOnOrBefore(dates, cutoffStr);
  const startKey = priorStartKey || dates[0];
  const startBalance = history[startKey];
  return buildAccountChange(startBalance, currentBalance, isLiability, priorStartKey ? cutoffStr : startKey, getAppNow());
}

export function getAccountGroupChangeSummary(groupAccounts, balanceHistory, timeframe, customDateRange, convertAtDate, getFallbackBalance = null, fallbackEndDate = null) {
  const summary = groupAccounts.reduce((current, account) => {
    if (account?.is_imported) {
      return {
        ...current,
        hasData: true,
        hasStartData: true,
        hasEndData: true,
      };
    }

    const change = getAccountChange(account.id, balanceHistory, timeframe, account.balance, account.is_liability, customDateRange, account.is_imported);
    if (change) {
      const startValue = account.is_liability ? -change.startBalance : change.startBalance;
      const endValue = account.is_liability ? -change.endBalance : change.endBalance;
      const convertedStart = convertAtDate(startValue, account.currency, change.startDate);
      const convertedEnd = convertAtDate(endValue, account.currency, change.endDate);
      const startDate = change.startDate || null;
      const sharedStartDate = current.startDate === undefined
        ? startDate
        : current.startDate === startDate
          ? current.startDate
          : null;
      const endDate = change.endDate || null;
      const sharedEndDate = current.endDate === undefined
        ? endDate
        : current.endDate === endDate
          ? current.endDate
          : null;
      return {
        diff: current.diff + (convertedEnd - convertedStart),
        start: current.start + convertedStart,
        end: current.end + convertedEnd,
        startDate: sharedStartDate,
        endDate: sharedEndDate,
        hasData: true,
        hasStartData: true,
        hasEndData: true,
        hasFallbackOnlyData: current.hasFallbackOnlyData,
      };
    }
    if (typeof getFallbackBalance === 'function') {
      const fallbackBalance = Number(getFallbackBalance(account));
      if (Number.isFinite(fallbackBalance)) {
        const fallbackValue = account.is_liability ? -fallbackBalance : fallbackBalance;
        const convertedEnd = convertAtDate(fallbackValue, account.currency, fallbackEndDate);
        const endDate = fallbackEndDate || null;
        const sharedEndDate = current.endDate === undefined
          ? endDate
          : current.endDate === endDate
            ? current.endDate
            : null;
        return {
          ...current,
          end: current.end + convertedEnd,
          endDate: sharedEndDate,
          hasEndData: true,
          hasFallbackOnlyData: true,
        };
      }
    }
    return current;
  }, { diff: 0, start: 0, end: 0, startDate: undefined, endDate: undefined, hasData: false, hasStartData: false, hasEndData: false, hasFallbackOnlyData: false });
  if (summary.startDate === undefined) {
    summary.startDate = null;
  }
  if (summary.endDate === undefined) {
    summary.endDate = null;
  }
  return summary;
}

export function formatOverviewMoney(amount, currency = 'CAD') {
  return formatMoney(Number(amount) || 0, currency);
}

export function formatOverviewChangeValue(change, currency = 'CAD') {
  if (!change) return '—';
  return `${change.diff >= 0 ? '+' : '-'}${formatOverviewMoney(Math.abs(change.diff), currency)}`;
}

// Signed change percent, full form: "+186.5%" / "-64.3%". The sign matches the (good-direction)
// diff, so colour + sign together carry the direction — no ▲/▼ needed.
export function formatChangePercentFull(pct) {
  if (pct === null || pct === undefined || pct === '') return null;
  const n = Number(pct);
  if (!Number.isFinite(n)) return null;
  return `${n >= 0 ? '+' : '-'}${Math.abs(n).toFixed(1)}%`;
}

// Compact form so a runaway ratio (a tiny baseline) can shrink in its box — FitMoney shows this and
// surfaces the full value on hover: "+29,900.0%" -> "+29.9K%". No "×N" folding, just compaction.
export function formatChangePercentCompact(pct) {
  if (pct === null || pct === undefined || pct === '') return null;
  const n = Number(pct);
  if (!Number.isFinite(n)) return null;
  return `${n >= 0 ? '+' : '-'}${new Intl.NumberFormat('en-CA', { notation: 'compact', maximumFractionDigits: 1 }).format(Math.abs(n))}%`;
}

export function parseSyncedAt(dateStr) {
  if (!dateStr) return null;
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(dateStr) ? dateStr : `${dateStr}Z`;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function timeAgo(dateStr) {
  if (!dateStr) return null;
  const parsed = parseSyncedAt(dateStr);
  if (!parsed) return null;
  const now = getAppNow();
  const diffMs = now - parsed;
  const mins = Math.floor(diffMs / 60000);
  const hours = Math.floor(diffMs / 3600000);
  const days = Math.floor(diffMs / 86400000);

  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  if (hours < 24) return `${hours}h ago`;
  if (days === 1) return '1d ago';
  return `${days}d ago`;
}

export function formatMoneyParts(amount, currency = 'CAD', { compact = false } = {}) {
  if (amount === null || amount === undefined || Number.isNaN(Number(amount))) {
    return null;
  }

  const crypto = cryptoCurrencySymbol(currency);
  if (crypto) {
    return {
      currencySymbol: crypto,
      amountText: new Intl.NumberFormat('en-CA', compact
        ? { notation: 'compact', maximumFractionDigits: 2 }
        : { minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(Math.abs(Number(amount))),
    };
  }

  const parts = new Intl.NumberFormat('en-CA', {
    style: 'currency',
    currency,
    currencyDisplay: 'narrowSymbol',
    ...(compact
      ? { notation: 'compact', maximumFractionDigits: 1 }
      : { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  }).formatToParts(Number(amount));

  const currencySymbol = parts
    .filter((part) => part.type === 'currency')
    .map((part) => stripCurrencyCodePrefix(part.value))
    .join('')
    .trim() || '$';

  const amountText = parts
    .filter((part) => part.type !== 'currency')
    .map((part) => part.value)
    .join('')
    .trim();

  return {
    currencySymbol,
    amountText,
  };
}
