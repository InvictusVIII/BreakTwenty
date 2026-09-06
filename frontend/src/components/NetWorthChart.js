import React, { useCallback, useMemo, useState } from 'react';
import { getAppliedBreakTwentyChartText } from '../theme/applyTheme';
import { formatCompactMoney, formatMoney } from '../utils/format';
import { useTheme } from '../appState';
import { getAppNow } from '../utils/appClock';
import EChart from './charts/EChart';

const NET_WORTH_AXIS_TICK_TARGET = 7;

// The series keys are calendar-date strings ("2024-02-01"). `new Date(str)` reads them as UTC
// midnight, which toLocaleDateString then renders a day earlier in any west-of-UTC zone
// ("Jan 31"). Parse the components into a LOCAL date so every label matches its key exactly.
function parseLocalDate(dateStr) {
  const [y, m, d] = String(dateStr).slice(0, 10).split('-').map(Number);
  if (Number.isFinite(y) && Number.isFinite(m) && Number.isFinite(d)) return new Date(y, m - 1, d);
  return new Date(dateStr);
}

function dateFromAxisValue(value) {
  if (typeof value === 'number') return new Date(value);
  return parseLocalDate(value);
}

function formatDate(value) {
  return dateFromAxisValue(value).toLocaleDateString('en-CA', { month: 'short', day: 'numeric' });
}

// Month + year for multi-year axis ticks — a month/day-only label can't tell 1990 from 2026.
function formatMonthYear(value) {
  return dateFromAxisValue(value).toLocaleDateString('en-CA', { month: 'short', year: 'numeric' });
}

// Full date (with year) for the tooltip — always unambiguous.
function formatDateFull(dateStr) {
  return parseLocalDate(dateStr).toLocaleDateString('en-CA', { year: 'numeric', month: 'short', day: 'numeric' });
}

function historyPointTime(point) {
  return parseLocalDate(point?.date).getTime();
}

function findNearestHistoryIndex(history, timeMs) {
  if (!history.length || !Number.isFinite(timeMs)) return 0;
  let lo = 0;
  let hi = history.length - 1;
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (historyPointTime(history[mid]) < timeMs) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  if (lo <= 0) return 0;
  const previous = lo - 1;
  const previousDelta = Math.abs(historyPointTime(history[previous]) - timeMs);
  const currentDelta = Math.abs(historyPointTime(history[lo]) - timeMs);
  return previousDelta <= currentDelta ? previous : lo;
}

export function buildUniformTickIndexes(length, target = NET_WORTH_AXIS_TICK_TARGET) {
  if (length <= 0) return new Set();
  if (length === 1) return new Set([0]);
  const count = Math.max(2, Math.min(target, length));
  return new Set(Array.from({ length: count }, (_value, index) => (
    Math.round((index * (length - 1)) / (count - 1))
  )));
}

function buildEmptyHistoryFrame() {
  const end = getAppNow();
  end.setDate(1);
  return Array.from({ length: 6 }, (_value, index) => {
    const date = new Date(end);
    date.setMonth(end.getMonth() - (5 - index));
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    return { date: `${year}-${month}-01`, net_worth: null };
  });
}

// Hex -> rgba so the area gradient can fade the line colour to transparent
// (ECharts gradient stops need real colour strings; opacity alone won't fade).
function withAlpha(color, alpha) {
  if (typeof color === 'string' && color.startsWith('#')) {
    let hex = color.slice(1);
    if (hex.length === 3) hex = hex.split('').map((c) => c + c).join('');
    if (hex.length === 6) {
      const num = parseInt(hex, 16);
      if (!Number.isNaN(num)) {
        return `rgba(${(num >> 16) & 255}, ${(num >> 8) & 255}, ${num & 255}, ${alpha})`;
      }
    }
  }
  return color;
}

function buildNetWorthTooltipHtml(entry, color, balancesHidden, currency) {
  if (!entry || entry.net_worth == null) return '';
  const value = balancesHidden ? '******' : formatMoney(entry.net_worth, currency);
  return '<div class="tooltip-card networth-chart-tooltip">'
    + `<p class="tooltip-date">${formatDateFull(entry.date)}</p>`
    + '<p class="networth-tooltip-figure">'
    + `<span class="networth-tooltip-dot" style="background-color:${color}"></span>`
    + `<span>${value}</span></p></div>`;
}

function createNetWorthTooltipFormatter(history, color, balancesHidden, currency) {
  let lastIndex = null;
  let lastHtml = '';
  return (params) => {
    const param = Array.isArray(params) ? params[0] : params;
    const axisTime = typeof param?.axisValue === 'number' ? param.axisValue : Number(param?.axisValue);
    const index = Number.isInteger(param?.dataIndex)
      ? param.dataIndex
      : Number.isFinite(axisTime)
        ? findNearestHistoryIndex(history, axisTime)
        : history.findIndex((point) => point.date === param?.axisValue);
    const clampedIndex = Math.max(0, Math.min(history.length - 1, index));
    if (clampedIndex === lastIndex) return lastHtml;
    lastIndex = clampedIndex;
    lastHtml = buildNetWorthTooltipHtml(history[clampedIndex], color, balancesHidden, currency);
    return lastHtml;
  };
}

export function buildNetWorthOption(history, { balancesHidden, color, currency, multiYear, tooltipPosition, emptyFrame, chartColors }) {
  const chartText = getAppliedBreakTwentyChartText();
  const chartTickText = chartText.tick;
  const axisText = {
    color: chartColors.tick,
    fontFamily: chartTickText.fontFamily,
    fontSize: chartTickText.fontSize,
    fontWeight: chartTickText.fontWeight,
  };
  const tickIndexes = buildUniformTickIndexes(history.length);
  const showUniformTick = (index) => tickIndexes.has(Number(index));
  return {
    grid: { top: 12, right: 30, bottom: 24, left: 72 },
    tooltip: {
      show: !emptyFrame,
      trigger: 'axis',
      appendToBody: true,
      confine: false,
      backgroundColor: 'transparent',
      borderWidth: 0,
      padding: 0,
      extraCssText: 'box-shadow:none;',
      axisPointer: { type: 'line', lineStyle: { color: chartColors.grid, type: 'dashed' } },
      // Return the actual data point's pixel (the white symbol), not the cursor: resolve the
      // snapped index from the cursor, then convert it back to a pixel. The +gap drops the
      // card just below the point.
      position: tooltipPosition,
      formatter: createNetWorthTooltipFormatter(history, color, balancesHidden, currency),
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: history.map((point) => point.date),
      axisLabel: {
        ...axisText,
        interval: showUniformTick,
        hideOverlap: true,
        margin: 10,
        showMinLabel: true,
        showMaxLabel: true,
        formatter: (value) => (multiYear ? formatMonthYear(value) : formatDate(value)),
      },
      axisLine: { show: false },
      axisTick: { show: false, interval: showUniformTick },
      axisPointer: { type: 'line', label: { show: false } },
      splitLine: { show: true, interval: showUniformTick, lineStyle: { color: chartColors.grid } },
    },
    yAxis: {
      type: 'value',
      min: emptyFrame ? 0 : undefined,
      max: emptyFrame ? 1 : undefined,
      splitNumber: emptyFrame ? 4 : undefined,
      axisLabel: {
        ...axisText,
        formatter: (value) => {
          if (balancesHidden) return '***';
          if (emptyFrame && Number(value) !== 0) return '';
          return formatCompactMoney(value, currency);
        },
      },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { show: true, lineStyle: { color: chartColors.grid } },
    },
    series: [{
      type: 'line',
      data: history.map((point) => point.net_worth),
      // No spline smoothing: straight segments keep the real price action crisp (and avoid
      // ECharts' smooth overshoot inventing values between points).
      smooth: false,
      showSymbol: false,
      symbolSize: 10,
      lineStyle: { color, width: 2.5 },
      itemStyle: { color, borderColor: chartColors.highlightStroke, borderWidth: chartColors.highlightStrokeWidth },
      areaStyle: {
        color: {
          type: 'linear',
          x: 0,
          y: 0,
          x2: 0,
          y2: 1,
          colorStops: [
            { offset: 0, color: withAlpha(color, 0.35) },
            { offset: 0.7, color: withAlpha(color, 0.12) },
            { offset: 1, color: withAlpha(color, 0) },
          ],
        },
      },
    }],
  };
}

function NetWorthChart({ history, balancesHidden, height = 280, color, currency = 'CAD', chartColors = null }) {
  const { chartColors: activeChartColors } = useTheme();
  const resolvedChartColors = chartColors || activeChartColors;
  const displayHistory = useMemo(
    () => (history.length > 0 ? history : buildEmptyHistoryFrame()),
    [history],
  );
  const emptyFrame = history.length === 0;

  const [chart, setChart] = useState(null);
  const multiYear = useMemo(() => {
    if (displayHistory.length < 2) return false;
    return parseLocalDate(displayHistory[displayHistory.length - 1].date).getFullYear() > parseLocalDate(displayHistory[0].date).getFullYear();
  }, [displayHistory]);
  const tooltipPosition = useCallback((pt, params, _dom, _rect, size) => {
    let x = pt[0];
    let y = pt[1];
    if (chart) {
      const param = Array.isArray(params) ? params[0] : params;
      let di = Number.isInteger(param?.dataIndex) ? param.dataIndex : null;
      if (di === null) {
        const data = chart.convertFromPixel({ xAxisIndex: 0 }, pt);
        const categoryIndex = Array.isArray(data) ? data[0] : data;
        di = Number.isFinite(categoryIndex) ? Math.round(categoryIndex) : null;
      }
      if (Number.isInteger(di)) {
        di = Math.max(0, Math.min(displayHistory.length - 1, di));
        if (displayHistory[di]) {
          const sx = chart.convertToPixel({ xAxisIndex: 0 }, di);
          const sy = chart.convertToPixel({ yAxisIndex: 0 }, displayHistory[di].net_worth);
          if (Number.isFinite(sx)) x = sx;
          if (Number.isFinite(sy)) y = sy;
        }
      }
    }
    const contentWidth = Number(size?.contentSize?.[0]) || 0;
    return [x - (contentWidth / 2), y + 24];
  }, [chart, displayHistory]);
  const option = useMemo(
    () => buildNetWorthOption(displayHistory, {
      balancesHidden,
      color: color || resolvedChartColors.netWorth,
      currency,
      multiYear,
      tooltipPosition,
      emptyFrame,
      chartColors: resolvedChartColors,
    }),
    [displayHistory, balancesHidden, color, currency, multiYear, tooltipPosition, emptyFrame, resolvedChartColors],
  );
  const clearChartHover = useCallback(() => {
    chart?.dispatchAction?.({ type: 'hideTip' });
    chart?.dispatchAction?.({ type: 'downplay', seriesIndex: 0 });
  }, [chart]);
  const chartEvents = useMemo(() => ({
    mouseout: clearChartHover,
    globalout: clearChartHover,
  }), [clearChartHover]);

  return (
    <div className="networth-chart">
      <EChart
        option={option}
        height={height}
        onChartReady={setChart}
        onEvents={chartEvents}
      />
    </div>
  );
}

export default NetWorthChart;
