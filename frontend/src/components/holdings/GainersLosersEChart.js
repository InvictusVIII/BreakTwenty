import React, { useMemo } from 'react';
import { getAppliedBreakTwentyTypography } from '../../theme/applyTheme';
import { formatCompactNumber, formatPercent, formatSignedPercent } from '../../utils/format';
import { escapeHtml } from '../../utils/html';
import { getBalancesHidden } from '../../hooks/useBalancesHidden';
import { useTheme } from '../../appState';
import {
  HOLDINGS_CHART_TOOLTIP_STYLE,
  formatMoneyNarrow,
  formatSignedMoneyNarrow,
  formatShareCount,
} from './holdingsTooltipUtils';
import { getChartTickText } from './holdingsIncomeConstants';
import EChart from '../charts/EChart';

const TOOLTIP_WRAP_STYLE = [
  `background:${HOLDINGS_CHART_TOOLTIP_STYLE.background}`,
  `border:${HOLDINGS_CHART_TOOLTIP_STYLE.border}`,
  'border-radius:var(--tooltip-radius)',
  `box-shadow:${HOLDINGS_CHART_TOOLTIP_STYLE.boxShadow}`,
].join(';');

function formatGainersLosersTick(value, showByValue) {
  if (showByValue) return getBalancesHidden() ? '***' : formatCompactNumber(value);
  return formatPercent(value);
}

// Same content as the GainersLosersTooltip React component.
function buildTooltipHtml(entry, currency) {
  if (!entry) return '';
  const pctClass = (Number(entry.gainLossPct) || 0) >= 0 ? 'is-positive' : 'is-negative';
  const valClass = (Number(entry.gainLossValue) || 0) >= 0 ? 'is-positive' : 'is-negative';
  let html = `<div style="${TOOLTIP_WRAP_STYLE}"><div class="holdings-pnl-tooltip">`;
  html += `<div class="holdings-pnl-tooltip-label">${escapeHtml(entry.symbol)}</div>`;
  if (entry.name) html += `<div class="holdings-pnl-tooltip-subtitle">${escapeHtml(entry.name)}</div>`;
  html += `<div class="holdings-pnl-tooltip-row"><span>P&amp;L %</span><span class="${pctClass}">${escapeHtml(formatSignedPercent(entry.gainLossPct))}</span></div>`;
  html += `<div class="holdings-pnl-tooltip-row"><span>P&amp;L $</span><span class="${valClass}">${escapeHtml(formatSignedMoneyNarrow(entry.gainLossValue, currency))}</span></div>`;
  html += `<div class="holdings-pnl-tooltip-row"><span>Market Value</span><span>${escapeHtml(formatMoneyNarrow(entry.marketValue, currency))}</span></div>`;
  if (Array.isArray(entry.accountBreakdown) && entry.accountBreakdown.length > 0) {
    html += '<div class="holdings-pnl-tooltip-divider"></div><div class="holdings-pnl-tooltip-account-list">';
    entry.accountBreakdown.forEach((account) => {
      html += `<div class="holdings-pnl-tooltip-account-row"><span>${escapeHtml(account.label)}</span><span>${escapeHtml(formatShareCount(account.quantity))}</span></div>`;
    });
    html += '</div>';
  }
  html += '</div></div>';
  return html;
}

function buildOption(rows, { showByValue, axisConfig, primaryCurrency, chartColors }) {
  const breaktwentyTypography = getAppliedBreakTwentyTypography();
  const chartTickText = getChartTickText();
  const tickText = {
    color: chartColors.tick,
    fontFamily: chartTickText.fontFamily,
    fontSize: chartTickText.fontSize,
    fontWeight: chartTickText.fontWeight,
  };
  const step = Array.isArray(axisConfig?.ticks) && axisConfig.ticks.length > 1
    ? Math.abs(axisConfig.ticks[1] - axisConfig.ticks[0])
    : undefined;
  return {
    grid: { top: 42, right: 18, bottom: 72, left: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      appendToBody: true,
      backgroundColor: 'transparent',
      borderWidth: 0,
      padding: 0,
      extraCssText: 'box-shadow:none;',
      axisPointer: { type: 'shadow', shadowStyle: { color: chartColors.cursorFill } },
      formatter: (params) => {
        const index = Array.isArray(params) ? params[0]?.dataIndex : params?.dataIndex;
        return buildTooltipHtml(rows[index], primaryCurrency);
      },
    },
    xAxis: {
      type: 'category',
      data: rows.map((row) => row.symbol),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { ...tickText, rotate: 38, interval: 0, margin: 14 },
    },
    yAxis: {
      type: 'value',
      min: axisConfig?.axisMin,
      max: axisConfig?.axisMax,
      interval: step,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { ...tickText, formatter: (value) => formatGainersLosersTick(value, showByValue) },
      splitLine: { lineStyle: { color: chartColors.subtleGrid } },
    },
    series: [{
      type: 'bar',
      barMaxWidth: 72,
      data: rows.map((row) => {
        const numericValue = Number(row.metricValue);
        return {
          value: numericValue,
          itemStyle: { color: row.fill },
          label: { position: numericValue < 0 ? 'bottom' : 'top' },
        };
      }),
      label: {
        show: true,
        distance: 8,
        color: chartColors.dataLabel,
        fontFamily: breaktwentyTypography.fontFamilies.sans,
        fontSize: breaktwentyTypography.chart.pieLabelFontSize,
        fontWeight: breaktwentyTypography.fontWeights.bold,
        formatter: (params) => formatGainersLosersTick(Number(params.value), showByValue),
      },
      markLine: {
        silent: true,
        symbol: 'none',
        lineStyle: { color: chartColors.subtleGrid, width: 1 },
        label: { show: false },
        data: [{ yAxis: 0 }],
      },
    }],
  };
}

export default function GainersLosersEChart({ rows = [], showByValue, axisConfig, primaryCurrency, height = '100%' }) {
  const { chartColors } = useTheme();
  const option = useMemo(
    () => buildOption(rows, { showByValue, axisConfig, primaryCurrency, chartColors }),
    [axisConfig, chartColors, primaryCurrency, rows, showByValue],
  );
  return <EChart option={option} height={height} />;
}
