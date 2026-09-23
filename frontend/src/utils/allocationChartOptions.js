import { formatMoney } from './format';
import { escapeHtml } from './html';

export const ALLOCATION_INSTITUTION_LEGEND_ROW_LIMIT = 8;
export const OTHER_INSTITUTIONS_COLOR_KEY = 'other-institutions';
export const OTHER_INSTITUTIONS_LABEL = 'Other Institutions';

export function formatAllocationPercent(value, total) {
  if (!total) return '0.0%';
  const pct = (Number(value || 0) / total) * 100;
  if (pct > 0 && pct < 0.1) return '<0.1%';
  return `${pct.toFixed(1)}%`;
}

function allocationValueHtml(value, total, balancesHidden, currency = 'CAD') {
  if (balancesHidden) return '******';
  return `<span class="tooltip-money">${escapeHtml(formatMoney(value, currency))}</span>`
    + `<span class="tooltip-share">(${escapeHtml(formatAllocationPercent(value, total))})</span>`;
}

function allocationRowHtml(item, total, balancesHidden, mainClass, currency = 'CAD') {
  const rowClass = mainClass ? `tooltip-row ${mainClass}` : 'tooltip-row';
  return `<div class="${rowClass}">`
    + `<span class="tooltip-dot" style="background:${item.legendColor}"></span>`
    + `<span class="tooltip-name">${escapeHtml(item.legendLabel)}</span>`
    + `<span class="tooltip-value">${allocationValueHtml(item.value, total, balancesHidden, currency)}</span></div>`;
}

// HTML for the ECharts pie tooltip, including grouped Other breakdowns.
function buildAllocationTooltipHtml(item, total, balancesHidden, currency = 'CAD') {
  if (!item) return '';
  let html = '<div class="tooltip-card">';
  html += allocationRowHtml(item, total, balancesHidden, 'tooltip-row-main', currency);
  if (Array.isArray(item.otherItems)) {
    html += '<div class="tooltip-breakdown">';
    item.otherItems.forEach((sub) => { html += allocationRowHtml(sub, total, balancesHidden, '', currency); });
    html += '</div>';
  }
  html += '</div>';
  return html;
}

function createAllocationTooltipFormatter(total, balancesHidden, currency = 'CAD') {
  let lastKey = null;
  let lastHtml = '';
  return (params) => {
    const key = params?.dataIndex ?? params?.name ?? null;
    if (key !== null && key === lastKey) return lastHtml;
    lastKey = key;
    lastHtml = buildAllocationTooltipHtml(params?.data?._item, total, balancesHidden, currency);
    return lastHtml;
  };
}

export function buildAllocationPieOption(displayData, total, balancesHidden, currency = 'CAD', chartColors) {
  return {
    tooltip: {
      trigger: 'item',
      triggerOn: 'none',
      // Attach to <body> so the panel's overflow/stacking context cannot clip the tooltip.
      appendToBody: true,
      backgroundColor: 'transparent',
      borderWidth: 0,
      padding: 0,
      extraCssText: 'box-shadow:none;',
      transitionDuration: 0,
      hideDelay: 80,
      formatter: createAllocationTooltipFormatter(total, balancesHidden, currency),
    },
    series: [{
      type: 'pie',
      radius: ['68%', '90%'],
      center: ['50%', '50%'],
      avoidLabelOverlap: false,
      label: { show: false },
      labelLine: { show: false },
      itemStyle: { borderColor: chartColors.segmentBorder, borderWidth: 1 },
      emphasis: { scale: false },
      data: displayData.map((item) => ({
        name: item.legendLabel,
        value: item.value,
        itemStyle: { color: item.legendColor },
        _item: item,
      })),
    }],
  };
}
