import React, { useMemo } from 'react';
import { getAppliedBreakTwentyTypography } from '../../theme/applyTheme';
import { formatPercent } from '../../utils/format';
import { escapeHtml } from '../../utils/html';
import { useTheme } from '../../appState';
import {
  HOLDINGS_CHART_TOOLTIP_STYLE,
  formatMoneyNarrow,
  formatShareCount,
} from './holdingsTooltipUtils';
import EChart from '../charts/EChart';

const TOOLTIP_WRAP_STYLE = [
  `background:${HOLDINGS_CHART_TOOLTIP_STYLE.background}`,
  `border:${HOLDINGS_CHART_TOOLTIP_STYLE.border}`,
  'border-radius:var(--tooltip-radius)',
  `box-shadow:${HOLDINGS_CHART_TOOLTIP_STYLE.boxShadow}`,
].join(';');

function accountRowsHtml(list) {
  return list.map((account) => (
    `<div class="holdings-pnl-tooltip-account-row"><span>${escapeHtml(account.label)}</span>`
    + `<span>${escapeHtml(formatShareCount(account.quantity))}</span></div>`
  )).join('');
}

// Replicates the HoldingsPieTooltip React component (incl. the "Other"-slice
// nested ticker/account breakdown and the tickers footer).
function buildTooltipHtml(entry, currency) {
  if (!entry) return '';
  const label = entry.tooltipLabel || entry.label || entry.symbol || '';
  const sharePct = Number(entry.sharePct);
  const accountBreakdown = Array.isArray(entry.accountBreakdown) ? entry.accountBreakdown : [];
  const isOtherSlice = label === 'Other' && Array.isArray(entry.tooltipBreakdown) && entry.tooltipBreakdown.length > 0;

  let html = `<div style="${TOOLTIP_WRAP_STYLE}"><div class="holdings-pnl-tooltip holdings-pie-tooltip">`;
  html += `<div class="holdings-pnl-tooltip-label holdings-pie-tooltip-label">${escapeHtml(label)}</div>`;
  if (entry.tooltipSubLabel) {
    html += `<div class="holdings-pnl-tooltip-subtitle holdings-pie-tooltip-meta">${escapeHtml(entry.tooltipSubLabel)}</div>`;
  }
  html += `<div class="holdings-pnl-tooltip-row"><span>Market Value</span><span>${escapeHtml(formatMoneyNarrow(entry.value, currency))}</span></div>`;
  if (Number.isFinite(sharePct)) {
    html += `<div class="holdings-pnl-tooltip-row"><span>Weight</span><span>${escapeHtml(formatPercent(sharePct))}</span></div>`;
  }
  if (!isOtherSlice && accountBreakdown.length > 0) {
    html += `<div class="holdings-pnl-tooltip-divider"></div><div class="holdings-pnl-tooltip-account-list">${accountRowsHtml(accountBreakdown)}</div>`;
  }
  if (isOtherSlice) {
    html += '<div class="holdings-pnl-tooltip-divider"></div><div class="holdings-pie-tooltip-breakdown">';
    html += `<div class="holdings-pie-tooltip-breakdown-label">${escapeHtml(entry.tooltipBreakdownLabel || 'Tickers')}</div>`;
    entry.tooltipBreakdown.forEach((item) => {
      html += '<div class="holdings-pie-tooltip-breakdown-item"><div class="holdings-pie-tooltip-breakdown-row">'
        + `<span class="holdings-pie-tooltip-breakdown-name">${escapeHtml(item.label)}</span>`
        + `<span class="holdings-pie-tooltip-breakdown-share">${escapeHtml(Number(item.sharePct).toFixed(1))}%</span></div>`;
      if (Array.isArray(item.accountBreakdown) && item.accountBreakdown.length > 0) {
        html += `<div class="holdings-pie-tooltip-nested-accounts">${accountRowsHtml(item.accountBreakdown)}</div>`;
      }
      html += '</div>';
    });
    html += '</div>';
  }
  if (Array.isArray(entry.tooltipTickers) && entry.tooltipTickers.length > 0) {
    html += '<div class="holdings-pnl-tooltip-divider"></div>';
    html += '<div class="holdings-pie-tooltip-tickers"><span class="holdings-pie-tooltip-tickers-label">Tickers:</span> '
      + `${escapeHtml(entry.tooltipTickers.join(', '))}</div>`;
  }
  html += '</div></div>';
  return html;
}

function buildOption(data, { labelKey, primaryCurrency, chartColors }) {
  const breaktwentyTypography = getAppliedBreakTwentyTypography();
  return {
    tooltip: {
      trigger: 'item',
      appendToBody: true,
      backgroundColor: 'transparent',
      borderWidth: 0,
      padding: 0,
      extraCssText: 'box-shadow:none;',
      formatter: (params) => buildTooltipHtml(params?.data?._item, primaryCurrency),
    },
    series: [{
      type: 'pie',
      radius: ['42%', '68%'],
      center: ['50%', '50%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: chartColors.segmentBorder, borderWidth: 1 },
      emphasis: { scale: false },
      labelLine: { show: true, lineStyle: { color: chartColors.labelLine } },
      label: {
        show: true,
        position: 'outside',
        color: chartColors.dataLabel,
        fontFamily: breaktwentyTypography.fontFamilies.sans,
        fontSize: breaktwentyTypography.chart.pieLabelFontSize,
        fontWeight: breaktwentyTypography.chart.denseTickFontWeight,
        formatter: (params) => {
          const text = params.data?._item?.[labelKey];
          if (!text) return '';
          return `${text}: ${Number(params.percent).toFixed(1)}%`;
        },
      },
      data: data.map((row) => ({
        name: row[labelKey] || row.symbol || row.label,
        value: row.value,
        itemStyle: { color: row.fill },
        _item: row,
      })),
    }],
  };
}

export default function HoldingsPieEChart({ data = [], labelKey, primaryCurrency, height = '100%' }) {
  const { chartColors } = useTheme();
  const option = useMemo(
    () => buildOption(data, { labelKey, primaryCurrency, chartColors }),
    [chartColors, data, labelKey, primaryCurrency],
  );
  return <EChart option={option} height={height} />;
}
