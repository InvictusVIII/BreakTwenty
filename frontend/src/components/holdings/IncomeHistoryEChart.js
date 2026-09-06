import React, { useMemo } from 'react';
import { formatCompactNumber } from '../../utils/format';
import { escapeHtml } from '../../utils/html';
import { getBalancesHidden } from '../../hooks/useBalancesHidden';
import { useTheme } from '../../appState';
import { formatMoneyNarrow } from './holdingsTooltipUtils';
import {
  INCOME_HISTORY_TOOLTIP_STYLE,
  getChartTickText,
} from './holdingsIncomeConstants';
import EChart from '../charts/EChart';

// ECharts replacement for the hand-rolled IncomeHistorySvgChart, keeping the
// exact look: green income bar (up) + red tax bar (down) side by side per
// bucket, the global data highlight on the selected bucket, the same
// breakdown tooltip, and click-to-open the detail tray.

const TOOLTIP_WRAP_STYLE = [
  `background:${INCOME_HISTORY_TOOLTIP_STYLE.background}`,
  `border:${INCOME_HISTORY_TOOLTIP_STYLE.border}`,
  'border-radius:var(--tooltip-radius)',
  `box-shadow:${INCOME_HISTORY_TOOLTIP_STYLE.boxShadow}`,
].join(';');

function highlightOutline(color, chartColors) {
  return {
    color,
    borderColor: chartColors.highlightStroke,
    borderWidth: chartColors.highlightStrokeWidth,
  };
}

function hoverOutline(color, chartColors) {
  return {
    color,
    borderColor: chartColors.hoverStroke,
    borderWidth: chartColors.hoverStrokeWidth,
  };
}

function swatchRow(color, name, valueText, toneClass) {
  return '<div class="income-chart-tooltip-row"><span class="income-chart-tooltip-name">'
    + `<span class="income-chart-tooltip-swatch" style="background-color:${color}"></span>`
    + `<span>${escapeHtml(name)}</span></span>`
    + `<span class="income-chart-tooltip-value ${toneClass}">${escapeHtml(valueText)}</span></div>`;
}

// Faithful port of IncomeHistoryTooltip (interest + combinedIncome branches).
function buildTooltipHtml(entry, currency, chartColors) {
  if (!entry) return '';
  const segments = Array.isArray(entry.segments)
    ? entry.segments.filter((segment) => Math.abs(Number(segment?.value) || 0) > 0.0001)
    : [];
  if (segments.length === 0) return '';

  const drawerDetail = entry._drawerDetail || {};
  const groups = Array.isArray(drawerDetail.groups) ? drawerDetail.groups : [];
  let body = '';

  if (drawerDetail.kind === 'interest') {
    if (groups.length > 0) {
      body += '<div class="income-chart-tooltip-months">';
      groups.forEach((group) => {
        let rows = '';
        if (Math.abs(Number(group.earned) || 0) > 0.0001) {
          rows += swatchRow(chartColors.interestReceived, 'Earned', formatMoneyNarrow(group.earned || 0, currency), 'is-positive');
        }
        if (Math.abs(Number(group.paid) || 0) > 0.0001) {
          rows += swatchRow(chartColors.interestPaid, 'Paid', formatMoneyNarrow(-Math.abs(Number(group.paid) || 0), currency), 'is-negative');
        }
        body += `<div class="income-chart-tooltip-month"><div class="income-chart-tooltip-month-label">${escapeHtml(group.label)}</div>${rows}</div>`;
      });
      body += '</div>';
    } else {
      if (Math.abs(Number(entry.Earned) || 0) > 0.0001) {
        body += swatchRow(chartColors.interestReceived, 'Earned', formatMoneyNarrow(entry.Earned || 0, currency), 'is-positive');
      }
      if (Math.abs(Number(entry.Paid) || 0) > 0.0001) {
        body += swatchRow(chartColors.interestPaid, 'Paid', formatMoneyNarrow(-Math.abs(Number(entry.Paid) || 0), currency), 'is-negative');
      }
    }
    const net = (Number(entry.Earned) || 0) - (Number(entry.Paid) || 0);
    body += `<div class="income-chart-tooltip-total"><span class="income-chart-tooltip-name"><span>Net Total</span></span>`
      + `<span class="income-chart-tooltip-value ${net >= 0 ? 'is-positive' : 'is-negative'}">${escapeHtml(formatMoneyNarrow(net, currency))}</span></div>`;
  } else {
    if (groups.length > 0) {
      body += '<div class="income-chart-tooltip-months">';
      groups.forEach((group) => {
        let rows = '';
        if (Math.abs(Number(group.dividends) || 0) > 0.0001) {
          rows += swatchRow(chartColors.dividendIncome, 'Dividends', formatMoneyNarrow(group.dividends || 0, currency), 'is-positive');
        }
        if (Math.abs(Number(group.withholding) || 0) > 0.0001) {
          rows += swatchRow(chartColors.withholdingTax, 'Withholding Tax', formatMoneyNarrow(group.withholding || 0, currency), 'is-negative');
        }
        body += `<div class="income-chart-tooltip-month"><div class="income-chart-tooltip-month-label">${escapeHtml(group.label)}</div>${rows}</div>`;
      });
      body += '</div>';
    } else {
      if (Math.abs(Number(entry.Dividends) || 0) > 0.0001) {
        body += swatchRow(chartColors.dividendIncome, 'Dividends', formatMoneyNarrow(entry.Dividends || 0, currency), 'is-positive');
      }
      if (Math.abs(Number(entry.Withholding) || 0) > 0.0001) {
        body += swatchRow(chartColors.withholdingTax, 'Withholding Tax', formatMoneyNarrow(entry.Withholding || 0, currency), 'is-negative');
      }
    }
    const net = Number(entry.Net) || 0;
    body += `<div class="income-chart-tooltip-total"><span class="income-chart-tooltip-name"><span>Net Total</span></span>`
      + `<span class="income-chart-tooltip-value ${net >= 0 ? 'is-positive' : 'is-negative'}">${escapeHtml(formatMoneyNarrow(net, currency))}</span></div>`;
  }

  return `<div style="${TOOLTIP_WRAP_STYLE}"><div class="income-chart-tooltip">`
    + `<div class="income-chart-tooltip-label">${escapeHtml(entry.month)}</div>${body}</div></div>`;
}

function buildOption(data, { selectedBucketKey, primaryCurrency, balancesHidden, positiveKey, negativeKey, positiveName, negativeName, positiveColor, negativeColor, chartColors }) {
  const selectedIndex = data.findIndex((bucket) => bucket._monthKey === selectedBucketKey);
  const chartTickText = getChartTickText();
  const axisText = {
    color: chartColors.tick,
    fontFamily: chartTickText.fontFamily,
    fontSize: chartTickText.fontSize,
    fontWeight: chartTickText.fontWeight,
  };
  const seriesItem = (key, name, color, sign) => ({
    name,
    type: 'bar',
    barMaxWidth: 22,
    itemStyle: { color },
    data: data.map((bucket, index) => {
      const value = sign * Math.abs(Number(bucket[key]) || 0);
      return index === selectedIndex && Math.abs(value) > 0.0001
        ? { value, itemStyle: highlightOutline(color, chartColors), emphasis: { itemStyle: highlightOutline(color, chartColors) } }
        : { value, emphasis: { itemStyle: hoverOutline(color, chartColors) } };
    }),
  });

  return {
    animation: false,
    grid: { top: 16, right: 18, bottom: 56, left: 56 },
    tooltip: {
      trigger: 'axis',
      appendToBody: true,
      // Always anchor the tooltip to the LEFT of the cursor (as the original SVG
      // chart did), instead of ECharts' default "right of cursor, flip near the
      // right edge". Clamped to stay on-screen.
      position: (point, params, dom, rect, size) => {
        const offset = 40;
        const [tooltipWidth, tooltipHeight] = size.contentSize;
        const box = document.querySelector('.income-history-echart')?.getBoundingClientRect();
        // Container-relative (ECharts adds the chart's page offset for appendToBody).
        // Horizontal: to the LEFT of the hovered bar — same relative offset for
        // every bucket (follows the bar, not a fixed spot).
        let left = point[0] - tooltipWidth - offset;
        // Vertical: FIXED high (above the plot) — same height for every bucket.
        let top = -160;
        if (box) {
          // Clamp so the final (page) position stays on-screen.
          left = Math.max(8 - box.left, Math.min(left, window.innerWidth - 8 - tooltipWidth - box.left));
          top = Math.max(8 - box.top, Math.min(top, window.innerHeight - 8 - tooltipHeight - box.top));
        }
        return [left, top];
      },
      backgroundColor: 'transparent',
      borderWidth: 0,
      padding: 0,
      extraCssText: 'box-shadow:none;',
      axisPointer: { type: 'shadow', shadowStyle: { color: chartColors.cursorFill } },
      formatter: (params) => {
        const index = Array.isArray(params) ? params[0]?.dataIndex : params?.dataIndex;
        return buildTooltipHtml(data[index], primaryCurrency, chartColors);
      },
    },
    xAxis: {
      type: 'category',
      data: data.map((bucket) => bucket.month),
      axisLabel: { ...axisText, rotate: 38, hideOverlap: true },
      axisLine: { lineStyle: { color: chartColors.subtleGrid } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { ...axisText, formatter: (value) => (balancesHidden ? '***' : formatCompactNumber(value)) },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: chartColors.subtleGrid } },
    },
    series: [
      seriesItem(positiveKey, positiveName, positiveColor, 1),
      seriesItem(negativeKey, negativeName, negativeColor, -1),
    ],
  };
}

export default function IncomeHistoryEChart({
  data = [],
  selectedBucketKey = null,
  onSelectBucket = null,
  primaryCurrency,
  height = 400,
  positiveKey = 'Dividends',
  negativeKey = 'Withholding',
  positiveName = 'Dividends',
  negativeName = 'Withholding Tax',
  positiveColor,
  negativeColor,
}) {
  const balancesHidden = getBalancesHidden();
  const { chartColors } = useTheme();
  const resolvedPositiveColor = positiveColor || chartColors.dividendIncome;
  const resolvedNegativeColor = negativeColor || chartColors.withholdingTax;
  const option = useMemo(
    () => buildOption(data, {
      selectedBucketKey, primaryCurrency, balancesHidden,
      positiveKey, negativeKey, positiveName, negativeName,
      positiveColor: resolvedPositiveColor,
      negativeColor: resolvedNegativeColor,
      chartColors,
    }),
    [
      balancesHidden,
      chartColors,
      data,
      negativeKey,
      negativeName,
      positiveKey,
      positiveName,
      primaryCurrency,
      resolvedNegativeColor,
      resolvedPositiveColor,
      selectedBucketKey,
    ],
  );
  // Open the tray for whichever bucket COLUMN is clicked — anywhere in the plot,
  // not just on the visible bar (the wrapper resolves the column's dataIndex).
  const handleGridClick = (dataIndex) => {
    const entry = data[dataIndex];
    if (entry && onSelectBucket) onSelectBucket(entry);
  };

  return <EChart option={option} height={height} onGridClick={handleGridClick} className="income-history-echart" />;
}
