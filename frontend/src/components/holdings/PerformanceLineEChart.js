import React, { useMemo } from 'react';
import { formatShortDateValue } from '../../utils/date';
import { formatSignedPercent } from '../../utils/format';
import { escapeHtml } from '../../utils/html';
import { useTheme } from '../../appState';
import { HOLDINGS_CHART_TOOLTIP_STYLE } from './holdingsTooltipUtils';
import { getChartTickText } from './holdingsIncomeConstants';
import EChart from '../charts/EChart';

const TOOLTIP_WRAP_STYLE = [
  `background:${HOLDINGS_CHART_TOOLTIP_STYLE.background}`,
  `border:${HOLDINGS_CHART_TOOLTIP_STYLE.border}`,
  'border-radius:var(--tooltip-radius)',
  `box-shadow:${HOLDINGS_CHART_TOOLTIP_STYLE.boxShadow}`,
].join(';');

// Same content as the PerformanceReturnTooltip React component.
function buildTooltipHtml(params) {
  if (!Array.isArray(params) || params.length === 0) return '';
  const visible = params.filter((entry) => entry.value !== null && entry.value !== undefined && !Number.isNaN(Number(entry.value)));
  if (visible.length === 0) return '';
  let html = `<div style="${TOOLTIP_WRAP_STYLE}"><div class="performance-tooltip">`;
  html += `<div class="performance-tooltip-label">${escapeHtml(formatShortDateValue(params[0].axisValue))}</div>`;
  visible.forEach((entry) => {
    html += '<div class="performance-tooltip-row">'
      + `<span class="performance-tooltip-name" style="color:${entry.color}">${escapeHtml(entry.seriesName)}</span>`
      + `<span class="performance-tooltip-value">${escapeHtml(formatSignedPercent(entry.value))}</span></div>`;
  });
  html += '</div></div>';
  return html;
}

function buildOption(data, lines, chartColors) {
  const chartTickText = getChartTickText();
  const tickText = {
    color: chartColors.tick,
    fontFamily: chartTickText.fontFamily,
    fontSize: chartTickText.fontSize,
    fontWeight: chartTickText.fontWeight,
  };
  return {
    grid: { top: 18, right: 22, bottom: 18, left: 4, containLabel: true },
    tooltip: {
      trigger: 'axis',
      appendToBody: true,
      backgroundColor: 'transparent',
      borderWidth: 0,
      padding: 0,
      extraCssText: 'box-shadow:none;',
      axisPointer: { type: 'line', lineStyle: { color: chartColors.subtleGrid, type: 'dashed' } },
      formatter: buildTooltipHtml,
    },
    xAxis: {
      type: 'category',
      data: data.map((point) => point.date),
      boundaryGap: false,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { ...tickText, hideOverlap: true, formatter: (value) => formatShortDateValue(value) },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { ...tickText, formatter: (value) => `${Number(value).toFixed(0)}%` },
      splitLine: { lineStyle: { color: chartColors.subtleGrid } },
    },
    series: lines.map((line) => ({
      name: line.name,
      type: 'line',
      data: data.map((point) => {
        const value = point[line.dataKey];
        return value === null || value === undefined ? null : value;
      }),
      smooth: true,
      showSymbol: false,
      symbolSize: 8,
      connectNulls: Boolean(line.connectNulls),
      lineStyle: { color: line.color, width: line.width },
      itemStyle: { color: line.color },
    })),
  };
}

export default function PerformanceLineEChart({ data = [], benchmarks = [], portfolioColor, height = '100%' }) {
  const { chartColors } = useTheme();
  const lines = useMemo(() => ([
    { dataKey: 'portfolio_return_pct', name: 'Portfolio', color: portfolioColor, width: 2.4, connectNulls: false },
    ...benchmarks
      .filter((benchmark) => benchmark.isVisible)
      .map((benchmark) => ({ dataKey: benchmark.dataKey, name: benchmark.name, color: benchmark.color, width: 1.8, connectNulls: true })),
  ]), [benchmarks, portfolioColor]);

  const option = useMemo(() => buildOption(data, lines, chartColors), [chartColors, data, lines]);
  return <EChart option={option} height={height} />;
}
