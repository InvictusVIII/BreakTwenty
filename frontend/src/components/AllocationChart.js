import React, { useState, useMemo } from 'react';
import { ACCOUNT_TYPE_LABELS } from '../constants/providers';
import { formatCompactMoney, formatMoney } from '../utils/format';
import { escapeHtml } from '../utils/html';
import { useTheme } from '../appState';
import FitMoney from './FitMoney';
import StablePieTooltipEChart from './charts/StablePieTooltipEChart';
import TriangleIcon from './TriangleIcon';

export const ALLOCATION_INSTITUTION_LEGEND_ROW_LIMIT = 8;
export const OTHER_INSTITUTIONS_COLOR_KEY = 'other-institutions';
export const OTHER_INSTITUTIONS_LABEL = 'Other Institutions';

function getOtherAllocationLabel(nameKey) {
  if (nameKey === 'institution') return OTHER_INSTITUTIONS_LABEL;
  if (nameKey === 'type') return 'Other Types';
  return 'Other';
}

function getOtherAllocationColorKey(nameKey) {
  if (nameKey === 'institution') return OTHER_INSTITUTIONS_COLOR_KEY;
  return `other-${nameKey || 'allocation'}`;
}

function formatSignedMoney(value, sign = 'none', currency = 'CAD', { compact = false } = {}) {
  const absoluteValue = Math.abs(Number(value || 0));
  const fmt = compact ? formatCompactMoney : formatMoney;
  if (sign === 'positive') return `+${fmt(absoluteValue, currency)}`;
  if (sign === 'negative' && absoluteValue > 0) return `-${fmt(absoluteValue, currency)}`;
  return fmt(absoluteValue, currency);
}

function formatPercent(value, total) {
  if (!total) return '0.0%';
  const pct = (Number(value || 0) / total) * 100;
  if (pct > 0 && pct < 0.1) return '<0.1%';
  return `${pct.toFixed(1)}%`;
}

function getItemColorKey(item, nameKey) {
  const key = item.colorKey ?? item.institutionId ?? item[nameKey];
  return key !== undefined && key !== null ? String(key) : null;
}

function getSliceColor(
  item,
  index,
  palette,
  customColors = {},
  defaultColors = {},
  nameKey,
  otherColor,
) {
  if (item.isOtherAllocation) {
    const colorKey = item.colorKey || OTHER_INSTITUTIONS_COLOR_KEY;
    return customColors[colorKey]
      || defaultColors[colorKey]
      || otherColor;
  }

  const colorKey = getItemColorKey(item, nameKey);
  const customColor = colorKey ? customColors[colorKey] : null;
  const defaultColor = colorKey ? defaultColors[colorKey] : null;

  return customColor || defaultColor || palette[index % palette.length];
}

function getDisplayLabel(item, nameKey) {
  return item[nameKey] || item.name || item.institution || item.type || 'Unknown';
}

function buildDisplayData(
  data,
  nameKey,
  palette,
  customColors,
  defaultColors,
  legendRowLimit = ALLOCATION_INSTITUTION_LEGEND_ROW_LIMIT,
  otherColor,
) {
  if (data.length <= legendRowLimit) {
    return data.map((item, index) => ({
      ...item,
      legendColor: getSliceColor(item, index, palette, customColors, defaultColors, nameKey, otherColor),
      legendLabel: getDisplayLabel(item, nameKey),
    }));
  }

  const directLimit = Math.max(1, legendRowLimit - 1);
  const visibleItems = data.slice(0, directLimit).map((item, index) => ({
    ...item,
    legendColor: getSliceColor(item, index, palette, customColors, defaultColors, nameKey, otherColor),
    legendLabel: getDisplayLabel(item, nameKey),
  }));
  const otherItems = data.slice(directLimit).map((item, index) => {
    const sourceIndex = index + directLimit;

    return {
      ...item,
      legendColor: getSliceColor(item, sourceIndex, palette, customColors, defaultColors, nameKey, otherColor),
      legendLabel: getDisplayLabel(item, nameKey),
    };
  });
  const otherValue = otherItems.reduce((sum, item) => sum + Number(item.value || 0), 0);
  const otherLabel = getOtherAllocationLabel(nameKey);
  const otherRow = {
    [nameKey]: otherLabel,
    legendLabel: otherLabel,
    colorKey: getOtherAllocationColorKey(nameKey),
    value: otherValue,
    isOtherAllocation: true,
    otherItems,
  };

  return [
    ...visibleItems,
    {
      ...otherRow,
      legendColor: getSliceColor(otherRow, visibleItems.length, palette, customColors, defaultColors, nameKey, otherColor),
    },
  ];
}

function AllocationBreakdownRows({ items, total, balancesHidden, currency = 'CAD' }) {
  return (
    <div className="tooltip-breakdown">
      {items.map((item, index) => (
        <div key={`${item.colorKey || item.legendLabel}-${index}`} className="tooltip-row">
          <span
            className="tooltip-dot"
            style={{ background: item.legendColor }}
          />
          <span className="tooltip-name">{item.legendLabel}</span>
          <span className="tooltip-value">
            {balancesHidden ? '******' : (
              <>
                <span className="tooltip-money">{formatMoney(item.value, currency)}</span>
                <span className="tooltip-share">({formatPercent(item.value, total)})</span>
              </>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

function allocationValueHtml(value, total, balancesHidden, currency = 'CAD') {
  if (balancesHidden) return '******';
  return `<span class="tooltip-money">${escapeHtml(formatMoney(value, currency))}</span>`
    + `<span class="tooltip-share">(${escapeHtml(formatPercent(value, total))})</span>`;
}

function allocationRowHtml(item, total, balancesHidden, mainClass, currency = 'CAD') {
  const rowClass = mainClass ? `tooltip-row ${mainClass}` : 'tooltip-row';
  return `<div class="${rowClass}">`
    + `<span class="tooltip-dot" style="background:${item.legendColor}"></span>`
    + `<span class="tooltip-name">${escapeHtml(item.legendLabel)}</span>`
    + `<span class="tooltip-value">${allocationValueHtml(item.value, total, balancesHidden, currency)}</span></div>`;
}

// HTML for the ECharts pie tooltip — same markup/classes as the old React
// AllocationTooltip, including grouped Other breakdowns.
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
      // Attach to <body> so the panel's overflow/stacking context can't clip it,
      // and lift it above the app's chrome. Standard for every migrated chart.
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

function DonutChart({
  data,
  nameKey,
  balancesHidden,
  centerValue,
  centerValueCompact = null,
  centerTone = 'asset',
  customColors = {},
  defaultColors = {},
  currency = 'CAD',
  legendVariant = 'default',
  legendRowLimit = ALLOCATION_INSTITUTION_LEGEND_ROW_LIMIT,
  footer = null,
  chartColors = null,
  donutHeight = 280,
}) {
  const { chartColors: activeChartColors } = useTheme();
  const resolvedChartColors = chartColors || activeChartColors;
  const sourceData = useMemo(() => (Array.isArray(data) ? data : []), [data]);
  const total = sourceData.reduce((sum, item) => sum + Number(item.value || 0), 0);
  const colors = centerTone === 'liability' ? resolvedChartColors.liabilities : resolvedChartColors.assets;
  const displayData = useMemo(
    () => buildDisplayData(sourceData, nameKey, colors, customColors, defaultColors, legendRowLimit, resolvedChartColors.other),
    [sourceData, nameKey, colors, customColors, defaultColors, legendRowLimit, resolvedChartColors],
  );
  const pieOption = useMemo(
    () => buildAllocationPieOption(displayData, total, balancesHidden, currency, resolvedChartColors),
    [displayData, total, balancesHidden, currency, resolvedChartColors],
  );

  if (!sourceData.length) return <p className="no-data">No data</p>;

  return (
    <div className="allocation-wrapper">
      <div className="allocation-donut-column">
        <div className="allocation-donut-stage">
          <StablePieTooltipEChart
            option={pieOption}
            height={donutHeight}
            className="allocation-donut-echart"
          />
          <div className={`allocation-donut-center is-${centerTone}`}>
            {balancesHidden ? '******' : (centerValueCompact
              ? <FitMoney full={centerValue} compact={centerValueCompact} className="allocation-donut-center-fit" />
              : centerValue)}
          </div>
        </div>
        {footer}
      </div>
      <div className={`allocation-legend ${legendVariant === 'bars' ? 'allocation-legend--bars' : ''}`.trim()}>
        {displayData.map((item, i) => (
          <div
            key={`${item.legendLabel}-${i}`}
            className={[
              'legend-item',
              legendVariant === 'bars' && 'allocation-bar-legend-row',
              Array.isArray(item.otherItems) && 'legend-item-other',
            ].filter(Boolean).join(' ')}
          >
            {legendVariant === 'bars' ? (
              <>
                <span className="allocation-bar-legend-label">
                  <span
                    className="legend-dot"
                    style={{ background: item.legendColor }}
                  />
                  <span className="legend-name">{item.legendLabel}</span>
                </span>
                <span className="allocation-bar-legend-track" aria-hidden="true">
                  <span
                    className="allocation-bar-legend-fill"
                    style={{
                      width: `${Math.min(100, Math.max(0, total ? (Number(item.value || 0) / total) * 100 : 0))}%`,
                      background: item.legendColor,
                    }}
                  />
                </span>
                <span className="legend-value">
                  {balancesHidden ? '******' : (
                    <>
                      <span className="legend-money">{formatMoney(item.value, currency)}</span>
                      <span className="legend-share">{formatPercent(item.value, total)}</span>
                    </>
                  )}
                </span>
              </>
            ) : (
              <>
                <span
                  className="legend-dot"
                  style={{ background: item.legendColor }}
                />
                <span className="legend-name">{item.legendLabel}</span>
                <span className="legend-value">
                  {balancesHidden ? '******' : (
                    <>
                      <span className="legend-money">{formatMoney(item.value, currency)}</span>
                      <span className="legend-share">({formatPercent(item.value, total)})</span>
                    </>
                  )}
                </span>
              </>
            )}
            {Array.isArray(item.otherItems) ? (
              <div className="legend-other-popover" role="tooltip">
                <div className="tooltip-heading">{item.legendLabel}</div>
                <AllocationBreakdownRows
                  items={item.otherItems}
                  total={total}
                  balancesHidden={balancesHidden}
                  currency={currency}
                />
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function AllocationChart({
  accounts,
  balancesHidden,
  assetTotal,
  liabilityTotal,
  tab: controlledTab,
  liabView: controlledLiabView,
  onLiabViewChange,
  assetInstitutionColors = {},
  liabilityInstitutionColors = {},
  liabilityTypeColors = {},
  assetInstitutionDefaultColors = {},
  liabilityInstitutionDefaultColors = {},
  liabilityTypeDefaultColors = {},
  currency = 'CAD',
  legendVariant = 'default',
  legendRowLimit = ALLOCATION_INSTITUTION_LEGEND_ROW_LIMIT,
  liabilityViewStepper = false,
  chartColors = null,
  donutHeight = 280,
}) {
  const [localLiabView, setLocalLiabView] = useState('institution');
  const tab = controlledTab || 'assets';
  const liabView = controlledLiabView || localLiabView;
  const setLiabView = onLiabViewChange || setLocalLiabView;

  const assetData = useMemo(() => {
    if (!accounts?.length) return [];
    const map = new Map();

    for (const account of accounts) {
      const balance = Number(account.balance || 0);
      if (account.is_liability || balance <= 0) continue;

      const key = account.institution_id !== undefined && account.institution_id !== null
        ? String(account.institution_id)
        : account.institution;
      if (!key) continue;

      const existing = map.get(key) || {
        institution: account.institution || 'Unknown',
        institutionId: account.institution_id,
        colorKey: key,
        value: 0,
      };
      existing.value += balance;
      map.set(key, existing);
    }

    return Array.from(map.values())
      .filter(d => d.value > 0)
      .sort((a, b) => b.value - a.value);
  }, [accounts]);

  const liabByInstitution = useMemo(() => {
    if (!accounts?.length) return [];
    const map = {};
    for (const a of accounts) {
      if (!a.is_liability || !a.balance) continue;
      const key = a.institution_id !== undefined && a.institution_id !== null
        ? String(a.institution_id)
        : a.institution;
      if (!key) continue;
      const existing = map[key] || {
        institution: a.institution || 'Unknown',
        institutionId: a.institution_id,
        colorKey: key,
        value: 0,
      };
      existing.value += Math.abs(a.balance);
      map[key] = existing;
    }
    return Object.values(map)
      .filter(d => d.value > 0)
      .sort((a, b) => b.value - a.value);
  }, [accounts]);

  const liabByType = useMemo(() => {
    if (!accounts?.length) return [];
    const map = {};
    for (const a of accounts) {
      if (!a.is_liability || !a.balance) continue;
      const key = a.account_type || 'other';
      map[key] = (map[key] || 0) + Math.abs(a.balance);
    }
    return Object.entries(map)
      .map(([type, value]) => ({
        type: ACCOUNT_TYPE_LABELS[type] || type.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
        colorKey: type,
        value,
      }))
      .filter(d => d.value > 0)
      .sort((a, b) => b.value - a.value);
  }, [accounts]);

  const liabData = liabView === 'institution' ? liabByInstitution : liabByType;
  const liabNameKey = liabView === 'institution' ? 'institution' : 'type';
  const liabilityCustomColors = liabView === 'institution' ? liabilityInstitutionColors : liabilityTypeColors;
  const liabilityDefaultColors = liabView === 'institution' ? liabilityInstitutionDefaultColors : liabilityTypeDefaultColors;
  const assetCenterTotal = assetTotal ?? assetData.reduce((sum, item) => sum + Number(item.value || 0), 0);
  const liabilityCenterTotal = liabilityTotal ?? liabData.reduce((sum, item) => sum + Number(item.value || 0), 0);
  // Assets are normally positive, but a net-negative position (e.g. a huge negative
  // manual balance) must read as negative, not a forced "+".
  const assetSign = Number(assetCenterTotal) < 0 ? 'negative' : 'positive';
  const assetCenterValue = formatSignedMoney(assetCenterTotal, assetSign, currency);
  const liabilityCenterValue = formatSignedMoney(liabilityCenterTotal, 'negative', currency);
  const assetCenterValueCompact = formatSignedMoney(assetCenterTotal, assetSign, currency, { compact: true });
  const liabilityCenterValueCompact = formatSignedMoney(liabilityCenterTotal, 'negative', currency, { compact: true });
  const liabilityViewLabel = liabView === 'institution' ? 'By Institution' : 'By Type';
  const liabilityNextView = liabView === 'institution' ? 'type' : 'institution';
  const liabilityNextLabel = liabilityNextView === 'institution' ? 'By Institution' : 'By Type';
  const liabilityStepper = liabilityViewStepper ? (
    tab === 'liabilities' ? (
      <div className="chart-view-stepper dashboard-allocation-mode-stepper" role="group" aria-label="Liability breakdown">
        <button
          type="button"
          className="chart-view-stepper-btn chart-view-stepper-prev-btn app-control-root"
          onClick={() => setLiabView(liabilityNextView)}
          aria-label={`Show ${liabilityNextLabel}`}
          data-tooltip={liabilityNextLabel}
        >
          <span className="app-control-icon" aria-hidden="true">
            <TriangleIcon direction="left" />
          </span>
        </button>
        <span className="chart-view-stepper-label">{liabilityViewLabel}</span>
        <button
          type="button"
          className="chart-view-stepper-btn chart-view-stepper-next-btn app-control-root"
          onClick={() => setLiabView(liabilityNextView)}
          aria-label={`Show ${liabilityNextLabel}`}
          data-tooltip={liabilityNextLabel}
        >
          <span className="app-control-icon" aria-hidden="true">
            <TriangleIcon direction="right" />
          </span>
        </button>
      </div>
    ) : (
      <div className="chart-view-stepper dashboard-allocation-mode-stepper is-placeholder" aria-hidden="true" />
    )
  ) : null;

  return (
    <>
      {tab === 'assets' ? (
        <DonutChart
          data={assetData}
          nameKey="institution"
          balancesHidden={balancesHidden}
          centerValue={assetCenterValue}
          centerValueCompact={assetCenterValueCompact}
          centerTone="asset"
          customColors={assetInstitutionColors}
          defaultColors={assetInstitutionDefaultColors}
          currency={currency}
          legendVariant={legendVariant}
          legendRowLimit={legendRowLimit}
          footer={liabilityStepper}
          chartColors={chartColors}
          donutHeight={donutHeight}
        />
      ) : (
        <DonutChart
          data={liabData}
          nameKey={liabNameKey}
          balancesHidden={balancesHidden}
          centerValue={liabilityCenterValue}
          centerValueCompact={liabilityCenterValueCompact}
          centerTone="liability"
          customColors={liabilityCustomColors}
          defaultColors={liabilityDefaultColors}
          currency={currency}
          legendVariant={legendVariant}
          legendRowLimit={legendRowLimit}
          footer={liabilityStepper}
          chartColors={chartColors}
          donutHeight={donutHeight}
        />
      )}
    </>
  );
}

export default AllocationChart;
