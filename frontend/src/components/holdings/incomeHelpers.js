/**
 * Income-history helpers used by the ECharts income chart, income detail drawer,
 * and the main Holdings page. Extracted from pages/Holdings.js during Stage 4.
 *
 * These functions are pure data/formatting helpers — they take inputs and return
 * derived state, with no React or DOM dependencies. Keeping them here lets the page
 * component and extracted chart/detail components share the same logic without duplication.
 */
import { formatShortDateValue as formatShortDateLabel, toLocalDateValue } from '../../utils/date';
import {
  INCOME_AGGREGATION_CONFIG,
  INCOME_CUSTOM_TIMELINE_KEY,
  INCOME_TIMELINE_PRESETS,
} from './holdingsIncomeConstants';
import { getAppNow } from '../../utils/appClock';

function getIncomeTimelineStartDate(preset) {
  if (!preset?.days && !preset?.ytd) return null;

  const now = getAppNow();
  if (preset.ytd) {
    return toLocalDateValue(new Date(now.getFullYear(), 0, 1));
  }

  const d = getAppNow();
  d.setDate(d.getDate() - preset.days);
  return toLocalDateValue(d);
}

export function getTodayDateValue() {
  return toLocalDateValue(getAppNow());
}

export function getIncomeTimelineBounds(timelineKey, customDateRange) {
  if (timelineKey === INCOME_CUSTOM_TIMELINE_KEY) {
    return {
      startDate: customDateRange.start || null,
      endDate: customDateRange.end || null,
    };
  }

  const preset = INCOME_TIMELINE_PRESETS.find((item) => item.key === timelineKey);
  return {
    startDate: getIncomeTimelineStartDate(preset),
    endDate: null,
  };
}

export function getIncomeTimelineSummary(timelineKey, customDateRange) {
  if (timelineKey === INCOME_CUSTOM_TIMELINE_KEY) {
    const { start, end } = customDateRange;
    if (start && end) {
      return `${formatShortDateLabel(start)} - ${formatShortDateLabel(end)}`;
    }
    if (start) return `From ${formatShortDateLabel(start)}`;
    if (end) return `Until ${formatShortDateLabel(end)}`;
    return 'Custom Range';
  }

  return INCOME_TIMELINE_PRESETS.find((item) => item.key === timelineKey)?.triggerLabel || 'Timeline';
}

function getMonthIndexFromDateValue(dateValue) {
  if (!dateValue) return null;

  const [yearValue, monthValue] = dateValue.slice(0, 10).split('-').map(Number);
  if (!Number.isFinite(yearValue) || !Number.isFinite(monthValue)) {
    return null;
  }

  return (yearValue * 12) + (monthValue - 1);
}

export function getInclusiveMonthSpan(startDate, endDate) {
  const startIndex = getMonthIndexFromDateValue(startDate);
  const endIndex = getMonthIndexFromDateValue(endDate);

  if (startIndex === null || endIndex === null) {
    return 1;
  }

  return Math.max(1, Math.abs(endIndex - startIndex) + 1);
}

export function getIncomeAggregationKey(timelineBounds, referenceTransactions) {
  const today = getTodayDateValue();
  const referenceDateValues = Array.isArray(referenceTransactions)
    ? referenceTransactions
      .map((tx) => (tx?.date ? tx.date.slice(0, 10) : null))
      .filter(Boolean)
      .sort()
    : [];

  const earliestReferenceDate = referenceDateValues[0] || timelineBounds.startDate || timelineBounds.endDate || today;
  const latestReferenceDate = referenceDateValues[referenceDateValues.length - 1] || timelineBounds.endDate || today;
  const startDate = timelineBounds.startDate || earliestReferenceDate;
  const endDate = timelineBounds.endDate || (timelineBounds.startDate ? today : latestReferenceDate);
  const monthSpan = getInclusiveMonthSpan(startDate, endDate);

  if (monthSpan <= 24) return 'monthly';
  if (monthSpan <= 72) return 'quarterly';
  return 'yearly';
}

export function getIncomeAggregationUnitLabel(aggregationKey, count) {
  const config = INCOME_AGGREGATION_CONFIG[aggregationKey] || INCOME_AGGREGATION_CONFIG.monthly;
  return count === 1 ? config.singular : config.plural;
}

export function getIncomeBucketMeta(dateValue, aggregationKey) {
  if (!dateValue) {
    return null;
  }

  const [yearValue, monthValue] = dateValue.slice(0, 10).split('-').map(Number);
  if (!Number.isFinite(yearValue) || !Number.isFinite(monthValue)) {
    return null;
  }

  const monthIndex = monthValue - 1;

  if (aggregationKey === 'yearly') {
    return {
      key: `${yearValue}`,
      label: `${yearValue}`,
    };
  }

  if (aggregationKey === 'quarterly') {
    const quarter = Math.floor(monthIndex / 3) + 1;
    return {
      key: `${yearValue}-Q${quarter}`,
      label: `Q${quarter} ${yearValue}`,
    };
  }

  const shortMonth = new Date(yearValue, monthIndex, 1).toLocaleString('en-US', { month: 'short' });
  return {
    key: `${yearValue}-${String(monthValue).padStart(2, '0')}`,
    label: `${shortMonth} ${String(yearValue).slice(2)}`,
  };
}

export function getDividendPositionSymbolFromEventTarget(target) {
  let node = target;

  while (node) {
    if (typeof node.getAttribute === 'function') {
      const symbol = node.getAttribute('data-dividend-position-symbol');
      if (symbol) {
        return symbol;
      }
    }

    node = node.parentNode;
  }

  return null;
}

export function getIncomeHistoryAxisStep(maxMagnitude, targetTickCount) {
  const roughStep = maxMagnitude / Math.max(1, targetTickCount);
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const normalizedStep = roughStep / magnitude;

  let stepMultiplier = 1;
  if (normalizedStep <= 1) stepMultiplier = 1;
  else if (normalizedStep <= 2) stepMultiplier = 2;
  else if (normalizedStep <= 2.5) stepMultiplier = 2.5;
  else if (normalizedStep <= 5) stepMultiplier = 5;
  else stepMultiplier = 10;

  return stepMultiplier * magnitude;
}
