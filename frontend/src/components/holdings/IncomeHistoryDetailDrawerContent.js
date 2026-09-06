import React, { useEffect, useRef } from 'react';
import { getBalancesHidden } from '../../hooks/useBalancesHidden';
import {
  MASKED_BALANCE_TEXT,
  formatMoneyNarrow,
} from './holdingsTooltipUtils';
import FitMoney from '../FitMoney';
import TriangleIcon from '../TriangleIcon';
import InstitutionLogo from '../InstitutionLogo';
import { formatCompactMoney } from '../../utils/format';

function formatIncomeDetailValue(value, tone, primaryCurrency) {
  if (getBalancesHidden()) return MASKED_BALANCE_TEXT;
  const numericValue = Number(value) || 0;
  if (tone === 'negative' || tone === 'withholding') {
    return formatMoneyNarrow(-Math.abs(numericValue), primaryCurrency);
  }
  return formatMoneyNarrow(numericValue, primaryCurrency);
}

function normalizeWheelDelta(delta, deltaMode, viewportSize) {
  if (deltaMode === 1) return delta * 16;
  if (deltaMode === 2) return delta * viewportSize;
  return delta;
}

function ContainedWheelScroll({ className, children }) {
  const scrollRef = useRef(null);

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return undefined;

    const handleWheel = (event) => {
      if (event.ctrlKey || node.scrollHeight <= node.clientHeight + 1) return;

      const deltaY = normalizeWheelDelta(event.deltaY, event.deltaMode, node.clientHeight);
      if (deltaY === 0) return;

      event.preventDefault();
      event.stopPropagation();
      node.scrollTop += deltaY;
    };

    node.addEventListener('wheel', handleWheel, { passive: false });
    return () => node.removeEventListener('wheel', handleWheel);
  }, []);

  return (
    <div ref={scrollRef} className={className}>
      {children}
    </div>
  );
}

export default function IncomeHistoryDetailDrawerContent({
  detail,
  primaryCurrency,
  expandedGroupKeys,
  onToggleGroup,
}) {
  if (!detail) {
    return null;
  }

  const renderRows = (rows) => (
    <ContainedWheelScroll className="income-detail-list">
      {rows.map((row) => (
        <div key={row.key || `${row.label}-${row.meta || ''}`} className="income-detail-row">
          <div className="income-detail-row-copy">
            <span className="income-detail-row-label">
              {row.account ? (
                <span className="income-detail-row-account-label">
                  {row.institution ? (
                    <span className="income-detail-row-institution-logo">
                      <InstitutionLogo name={row.institution} size={16} boxed />
                    </span>
                  ) : null}
                  <span className="income-detail-row-label-account">{row.account}</span>
                </span>
              ) : null}
              {row.account && row.label ? ' · ' : null}
              {row.label}
              {row.meta ? <span className="income-detail-row-label-meta"> · {row.meta}</span> : null}
            </span>
            {row.metaSecondary ? (
              <span className="income-detail-row-label-meta is-secondary">{row.metaSecondary}</span>
            ) : null}
          </div>
          <span className={`income-detail-row-value ${row.tone ? `is-${row.tone}` : ''}`.trim()}>
            {formatIncomeDetailValue(row.value, row.tone, primaryCurrency)}
          </span>
        </div>
      ))}
    </ContainedWheelScroll>
  );

  const hasGroups = Array.isArray(detail.groups) && detail.groups.length > 0;
  const hasRows = Array.isArray(detail.rows) && detail.rows.length > 0;
  const summaryItems = Array.isArray(detail.summaryItems) ? detail.summaryItems : [];
  const orderedSummaryItems = detail.kind === 'interest'
    ? [
      summaryItems.find((item) => item.key === 'earned'),
      summaryItems.find((item) => item.key === 'paid'),
      summaryItems.find((item) => item.key === 'total'),
    ].filter(Boolean)
    : [
      summaryItems.find((item) => item.key === 'dividends'),
      summaryItems.find((item) => item.key === 'withholding'),
      summaryItems.find((item) => item.key === 'net'),
    ].filter(Boolean);
  const primarySummaryItems = orderedSummaryItems.slice(0, 2);
  const netSummaryItem = orderedSummaryItems[2] || null;

  return (
    <div className="income-detail-drawer-content">
      {orderedSummaryItems.length > 0 ? (
        <div className="income-detail-summary-stack">
          {primarySummaryItems.map((item) => (
            <div key={item.key || item.label} className="income-detail-summary-line">
              <span className="income-detail-summary-text">{item.label}:</span>
              <span className={`income-detail-summary-inline-value ${item.tone ? `is-${item.tone}` : ''}`.trim()}>
                {formatIncomeDetailValue(item.value, item.tone, primaryCurrency)}
              </span>
            </div>
          ))}
          {netSummaryItem ? (
            <>
              <div className="income-detail-summary-divider" />
              <div className="income-detail-summary-line">
                <span className="income-detail-summary-text">{netSummaryItem.label}:</span>
                <span className={`income-detail-summary-inline-value ${Number(netSummaryItem.value) >= 0 ? 'is-positive' : 'is-negative'}`.trim()}>
                  <FitMoney full={getBalancesHidden() ? MASKED_BALANCE_TEXT : formatMoneyNarrow(netSummaryItem.value || 0, primaryCurrency)} compact={getBalancesHidden() ? MASKED_BALANCE_TEXT : formatCompactMoney(netSummaryItem.value || 0, primaryCurrency)} className="income-detail-summary-money" />
                </span>
              </div>
            </>
          ) : null}
        </div>
      ) : null}

      <div className="income-detail-section">
        {hasGroups ? (
          <ContainedWheelScroll className="income-detail-group-list">
            {detail.groups.map((group) => {
              const isExpanded = Boolean(expandedGroupKeys[group.key]);

              return (
                <div key={group.key} className={`income-detail-group ${isExpanded ? 'is-open' : ''}`.trim()}>
                  <button
                    type="button"
                    className={`income-detail-group-toggle ${isExpanded ? 'is-open' : ''}`.trim()}
                    onClick={() => onToggleGroup(group.key)}
                  >
                    <span className="income-detail-group-copy">
                      <span className="income-detail-group-label">{group.label}</span>
                    </span>
                    <span className="income-detail-group-side">
                      <span className={`income-detail-group-total ${Number(group.total) >= 0 ? 'is-positive' : 'is-negative'}`.trim()}>
                        <FitMoney full={getBalancesHidden() ? MASKED_BALANCE_TEXT : formatMoneyNarrow(group.total || 0, primaryCurrency)} compact={getBalancesHidden() ? MASKED_BALANCE_TEXT : formatCompactMoney(group.total || 0, primaryCurrency)} className="income-detail-group-money" />
                      </span>
                      <span className="income-detail-group-chevron" aria-hidden="true">
                        <TriangleIcon direction={isExpanded ? 'up' : 'down'} />
                      </span>
                    </span>
                  </button>

                  {isExpanded ? (
                    <div className="income-detail-group-body">
                      {renderRows(group.rows)}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </ContainedWheelScroll>
        ) : hasRows ? (
          renderRows(detail.rows)
        ) : (
          <div className="holdings-analysis-empty income-detail-empty">
            <span>{detail.emptyMessage || 'No period detail available.'}</span>
          </div>
        )}
      </div>
    </div>
  );
}
