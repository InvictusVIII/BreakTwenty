import React, { useMemo } from 'react';
import { formatCompactNumber } from '../../utils/format';
import { escapeHtml } from '../../utils/html';
import { useTheme } from '../../appState';
import { formatMoneyNarrow, HOLDINGS_CHART_TOOLTIP_STYLE } from './holdingsTooltipUtils';
import { getChartAxisLabelText, getChartTickText } from './holdingsIncomeConstants';
import EChart from '../charts/EChart';

const TOOLTIP_WRAP_STYLE = [
  `background:${HOLDINGS_CHART_TOOLTIP_STYLE.background}`,
  `border:${HOLDINGS_CHART_TOOLTIP_STYLE.border}`,
  'border-radius:var(--tooltip-radius)',
  `box-shadow:${HOLDINGS_CHART_TOOLTIP_STYLE.boxShadow}`,
].join(';');

function tooltipRow(color, name, valueText) {
  return '<div class="income-chart-tooltip-row"><span class="income-chart-tooltip-name">'
    + `<span class="income-chart-tooltip-swatch" style="background-color:${color}"></span>`
    + `<span>${escapeHtml(name)}</span></span>`
    + `<span class="income-chart-tooltip-value">${escapeHtml(valueText)}</span></div>`;
}

// Same content as the DividendPositionTooltip React component.
function buildTooltipHtml(entry, currency, chartColors) {
  if (!entry) return '';
  let body = `<div class="income-chart-tooltip-label">${escapeHtml(entry.symbol)}</div>`;
  body += tooltipRow(chartColors.dividendIncome, 'Dividends', formatMoneyNarrow(entry.grossDividends || 0, currency));
  if (Math.abs(Number(entry.withholdingTaxTotal) || 0) > 0.0001) {
    body += tooltipRow(chartColors.withholdingTax, 'Withholding Tax', formatMoneyNarrow(entry.withholdingTaxTotal || 0, currency));
  }
  body += '<div class="income-chart-tooltip-total"><span class="income-chart-tooltip-name"><span>Net Total</span></span>'
    + `<span class="income-chart-tooltip-value">${escapeHtml(formatMoneyNarrow(entry.netTotal || 0, currency))}</span></div>`;
  return `<div style="${TOOLTIP_WRAP_STYLE}"><div class="income-chart-tooltip">${body}</div></div>`;
}

function buildOption(data, { selectedSymbol, primaryCurrency, balancesHidden, chartColors }) {
  const chartTickText = getChartTickText();
  const chartAxisLabelText = getChartAxisLabelText();
  return {
    grid: { top: 8, right: 60, bottom: 8, left: 8, containLabel: true },
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
        return buildTooltipHtml(data[index], primaryCurrency, chartColors);
      },
    },
    xAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: chartColors.tick,
        fontFamily: chartTickText.fontFamily,
        fontSize: chartTickText.fontSize,
        fontWeight: chartTickText.fontWeight,
        formatter: (value) => (balancesHidden ? '***' : formatCompactNumber(value)),
      },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'category',
      data: data.map((row) => row.symbol),
      inverse: true,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: chartColors.label,
        fontFamily: chartAxisLabelText.fontFamily,
        fontSize: chartAxisLabelText.fontSize,
        fontWeight: chartAxisLabelText.fontWeight,
      },
      splitLine: { show: false },
    },
    series: [{
      type: 'bar',
      barMaxWidth: 26,
      data: data.map((row, index) => ({
        value: row.total,
        itemStyle: {
          color: chartColors.holdingsBars[index % chartColors.holdingsBars.length],
          opacity: selectedSymbol && selectedSymbol !== row.symbol ? 0.58 : 1,
          borderColor: selectedSymbol === row.symbol ? chartColors.highlightStroke : 'transparent',
          borderWidth: selectedSymbol === row.symbol ? chartColors.highlightStrokeWidth : 0,
        },
        emphasis: {
          itemStyle: {
            color: chartColors.holdingsBars[index % chartColors.holdingsBars.length],
            opacity: selectedSymbol && selectedSymbol !== row.symbol ? 0.58 : 1,
            borderColor: selectedSymbol === row.symbol ? chartColors.highlightStroke : chartColors.hoverStroke,
            borderWidth: selectedSymbol === row.symbol ? chartColors.highlightStrokeWidth : chartColors.hoverStrokeWidth,
          },
        },
      })),
      label: {
        show: true,
        position: 'right',
        color: chartColors.tick,
        fontFamily: chartTickText.fontFamily,
        fontSize: chartTickText.fontSize,
        fontWeight: chartTickText.fontWeight,
        formatter: (params) => formatMoneyNarrow(data[params.dataIndex]?.total || 0, primaryCurrency),
      },
    }],
  };
}

export default function DividendsByPositionEChart({
  data = [],
  selectedSymbol = null,
  onSelect = null,
  primaryCurrency,
  balancesHidden = false,
}) {
  const { chartColors } = useTheme();
  const option = useMemo(
    () => buildOption(data, { selectedSymbol, primaryCurrency, balancesHidden, chartColors }),
    [balancesHidden, chartColors, data, primaryCurrency, selectedSymbol],
  );
  const onEvents = useMemo(() => ({
    click: (params) => {
      const entry = data[params?.dataIndex];
      if (entry && onSelect) onSelect(entry);
    },
  }), [data, onSelect]);

  const height = Math.max(200, data.length * 38 + 40);
  return <EChart option={option} height={height} onEvents={onEvents} />;
}
