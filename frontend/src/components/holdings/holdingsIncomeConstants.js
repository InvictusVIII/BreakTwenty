/**
 * Income-history visual constants shared between the Holdings page and the
 * extracted income/dividend tooltips and chart components.
 *
 * Single source of truth for the income-chart tooltip surface and timeline
 * constants, so the page and its sub-components can't drift apart.
 */
import { getAppliedBreakTwentyChartText } from '../../theme/applyTheme';

export const INCOME_HISTORY_TOOLTIP_STYLE = {
  background: 'var(--tooltip-bg)',
  border: '1px solid var(--tooltip-border-color)',
  borderRadius: 'var(--tooltip-radius)',
  boxShadow: 'var(--tooltip-shadow)',
};

export const INCOME_TIMELINE_PRESETS = [
  { key: '1D', label: '1D', triggerLabel: '1 Day', days: 1 },
  { key: '5D', label: '5D', triggerLabel: '5 Days', days: 5 },
  { key: '30D', label: '30D', triggerLabel: '30 Days', days: 30 },
  { key: '90D', label: '90D', triggerLabel: '90 Days', days: 90 },
  { key: '6M', label: '6M', triggerLabel: '6 Months', days: 180 },
  { key: 'YTD', label: 'YTD', triggerLabel: 'Year to Date', days: null, ytd: true },
  { key: '1Y', label: '1Y', triggerLabel: '1 Year', days: 365 },
  { key: 'All', label: 'All', triggerLabel: 'All Time', days: null },
];

export const INCOME_CUSTOM_TIMELINE_KEY = 'CUSTOM';

export const INCOME_DETAIL_TRAY_TRANSITION_MS = 360;

export const INCOME_AGGREGATION_CONFIG = {
  monthly: { label: 'Monthly', singular: 'month', plural: 'months' },
  quarterly: { label: 'Quarterly', singular: 'quarter', plural: 'quarters' },
  yearly: { label: 'Yearly', singular: 'year', plural: 'years' },
};

// Generic chart axis/tick typography — used by income chart and the main Holdings
// page's other charts. Kept here for now alongside the income constants; could
// move to its own module if more chart-typography constants accumulate.
export function getChartTickText() {
  return getAppliedBreakTwentyChartText().denseTick;
}

export function getChartAxisLabelText() {
  return getAppliedBreakTwentyChartText().axisLabel;
}
