import React, { useMemo } from 'react';
import { getAppliedBreakTwentyTypography } from '../../theme/applyTheme';
import { formatCompactNumber } from '../../utils/format';
import { escapeHtml } from '../../utils/html';
import { useTheme } from '../../appState';
import {
  HOLDINGS_CHART_TOOLTIP_STYLE,
  formatMoneyNarrow,
  formatShareCount,
} from './holdingsTooltipUtils';
import { getChartAxisLabelText, getChartTickText } from './holdingsIncomeConstants';
import EChart from '../charts/EChart';

const TOOLTIP_WRAP_STYLE = [
  `background:${HOLDINGS_CHART_TOOLTIP_STYLE.background}`,
  `border:${HOLDINGS_CHART_TOOLTIP_STYLE.border}`,
  'border-radius:var(--tooltip-radius)',
  `box-shadow:${HOLDINGS_CHART_TOOLTIP_STYLE.boxShadow}`,
].join(';');

// Same content as the TopHoldingTooltip React component.
function buildTooltipHtml(entry, currency) {
  if (!entry) return '';
  let html = `<div style="${TOOLTIP_WRAP_STYLE}"><div class="holdings-pnl-tooltip">`;
  html += `<div class="holdings-pnl-tooltip-label">${escapeHtml(entry.symbol)}</div>`;
  if (entry.name) html += `<div class="holdings-pnl-tooltip-subtitle">${escapeHtml(entry.name)}</div>`;
  html += `<div class="holdings-pnl-tooltip-row"><span>Market Value</span><span>${escapeHtml(formatMoneyNarrow(entry.value, currency))}</span></div>`;
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

function buildOption(rows, { primaryCurrency, balancesHidden, chartColors }) {
  const breaktwentyTypography = getAppliedBreakTwentyTypography();
  const chartTickText = getChartTickText();
  const chartAxisLabelText = getChartAxisLabelText();
  const tickText = {
    color: chartColors.tick,
    fontFamily: chartTickText.fontFamily,
    fontSize: chartTickText.fontSize,
    fontWeight: chartTickText.fontWeight,
  };
  return {
    grid: { top: 34, right: 18, bottom: 58, left: 6, containLabel: true },
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
      name: 'Market Value ($)',
      nameLocation: 'middle',
      nameRotate: 90,
      nameGap: 54,
      nameTextStyle: {
        color: chartColors.tick,
        fontFamily: chartAxisLabelText.fontFamily,
        fontSize: chartAxisLabelText.fontSize,
        fontWeight: chartAxisLabelText.fontWeight,
      },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { ...tickText, formatter: (value) => (balancesHidden ? '***' : formatCompactNumber(value)) },
      splitLine: { lineStyle: { color: chartColors.subtleGrid } },
    },
    series: [{
      type: 'bar',
      barMaxWidth: 72,
      data: rows.map((row) => ({ value: row.value, itemStyle: { color: row.fill } })),
      label: {
        show: true,
        position: 'top',
        distance: 8,
        color: chartColors.dataLabel,
        fontFamily: breaktwentyTypography.fontFamilies.sans,
        fontSize: breaktwentyTypography.chart.pieLabelFontSize,
        fontWeight: breaktwentyTypography.fontWeights.bold,
        formatter: (params) => (balancesHidden ? '***' : formatCompactNumber(Number(params.value))),
      },
    }],
  };
}

export default function TopHoldingEChart({ rows = [], primaryCurrency, balancesHidden = false, height = '100%' }) {
  const { chartColors } = useTheme();
  const option = useMemo(
    () => buildOption(rows, { primaryCurrency, balancesHidden, chartColors }),
    [balancesHidden, chartColors, primaryCurrency, rows],
  );
  return <EChart option={option} height={height} />;
}
