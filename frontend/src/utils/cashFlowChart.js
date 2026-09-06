// Shared Cash Flow chart + money helpers.
//
// Extracted from pages/CashFlow.js so the Cash Flow page and the dashboard
// cash-flow panel build identical donuts/tooltips from a single source instead
// of diverging copies. Pure (no React) — safe to import anywhere.
import { getAppliedBreakTwentyChartColors } from '../theme/applyTheme';
import { currencySymbolFor } from './format';
import { escapeHtml } from './html';

export function formatMoney(amount, currency = 'CAD', hidden = false) {
  if (hidden) return '••••';
  const sign = amount < 0 ? '-' : '';
  const abs = Math.abs(amount).toLocaleString('en-CA', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const symbol = currencySymbolFor(currency);
  // \p{L} (not [A-Za-z]) so the accented "č" in "Kč" counts as a letter and
  // gets the separating NBSP, matching ICU narrowSymbol ("Kč 2,345.00").
  const spacer = /\p{L}$/u.test(symbol) ? ' ' : '';
  return `${sign}${symbol}${spacer}${abs}`;
}

// Collapse leaf rows up to their parent category. Leaves without a parent
// stay as-is. Uses parent color/icon fields from the
// backend so the rolled-up pill carries the parent's own visual identity,
// not a borrowed-leaf color.
export function rollupByParent(rows) {
  const map = new Map();
  let totalAmount = 0;
  rows.forEach((row) => {
    totalAmount += row.amount;
    const hasParent = row.parent_id != null;
    const key = hasParent ? `p-${row.parent_id}` : `l-${row.category_id}`;
    if (!map.has(key)) {
      map.set(key, {
        category_id: hasParent ? row.parent_id : row.category_id,
        name: hasParent ? row.parent_name : row.name,
        icon: hasParent ? row.parent_icon : row.icon,
        icon_set: hasParent ? row.parent_icon_set : row.icon_set,
        color_dark: hasParent ? row.parent_color_dark : row.color_dark,
        color_light: hasParent ? row.parent_color_light : row.color_light,
        seed_key: hasParent ? row.parent_seed_key : row.seed_key,
        parent_id: null,
        parent_name: null,
        amount: 0,
        transaction_count: 0,
      });
    }
    const entry = map.get(key);
    entry.amount += row.amount;
    entry.transaction_count += row.transaction_count || 0;
  });
  const out = Array.from(map.values());
  out.forEach((r) => {
    r.percent_of_total = totalAmount > 0 ? (r.amount / totalAmount) * 100 : 0;
  });
  out.sort((a, b) => b.amount - a.amount);
  return out;
}

// Shared HTML tooltips + ECharts option builders for the Cash Flow charts.
// Reuses the global `.tooltip*` classes so they read in-theme, and
// appendToBody so the panel's overflow can't clip them.
export const CASH_FLOW_DONUT_HEIGHT = 285;

function cfDonutTooltipHtml(name, value, color, total, currency, balancesHidden) {
  const pct = total > 0 ? `<span class="tooltip-share">(${((value / total) * 100).toFixed(1)}%)</span>` : '';
  return '<div class="tooltip-card"><div class="tooltip-row tooltip-row-main">'
    + `<span class="tooltip-dot" style="background:${color}"></span>`
    + `<span class="tooltip-name">${escapeHtml(name)}</span>`
    + '<span class="tooltip-value">'
    + `<span class="tooltip-money">${escapeHtml(formatMoney(value, currency, balancesHidden))}</span>${pct}</span></div></div>`;
}

export function buildCfDonutOption(
  segments,
  {
    total,
    currency,
    balancesHidden,
    startAngle = 90,
    chartColors = getAppliedBreakTwentyChartColors(),
    tooltipDisabled = false,
  },
) {
  return {
    tooltip: {
      show: !tooltipDisabled,
      trigger: 'item',
      triggerOn: 'none',
      appendToBody: true,
      backgroundColor: 'transparent',
      borderWidth: 0,
      padding: 0,
      extraCssText: 'box-shadow:none;',
      transitionDuration: 0,
      hideDelay: 80,
      formatter: (params) => cfDonutTooltipHtml(
        params?.name,
        Number(params?.value || 0),
        params?.data?.itemStyle?.color,
        total,
        currency,
        balancesHidden,
      ),
    },
    series: [{
      type: 'pie',
      radius: ['55%', '88%'],
      center: ['50%', '50%'],
      startAngle,
      avoidLabelOverlap: false,
      label: { show: false },
      labelLine: { show: false },
      itemStyle: { borderColor: chartColors.segmentBorder, borderWidth: 1 },
      emphasis: { scale: false },
      data: segments.map((segment) => ({
        name: segment.name,
        value: segment.value,
        itemStyle: { color: segment.color },
      })),
    }],
  };
}
