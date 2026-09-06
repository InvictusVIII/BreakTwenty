import React from 'react';
import { MdCheck, MdSync } from 'react-icons/md';
import { breaktwentyTypography } from '../theme/typography';

function SyncAttentionIcon({ size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 2L1 21h22L12 2z" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
      <text
        x="12"
        y="18"
        textAnchor="middle"
        fill="currentColor"
        fontFamily={breaktwentyTypography.fontFamilies.sans}
        fontSize={breaktwentyTypography.chart.iconFontSize}
        fontWeight={breaktwentyTypography.fontWeights.bold}
      >
        !
      </text>
    </svg>
  );
}

function SyncStatusIcon({ model, size }) {
  if (model.iconState === 'attention') return <SyncAttentionIcon size={size} />;
  if (model.iconState === 'check') return <MdCheck size={size} aria-hidden="true" />;
  return <MdSync size={size} className={model.spin ? 'spin-icon' : ''} aria-hidden="true" />;
}

function getActionLabel(model, institutionName) {
  const name = institutionName || 'institution';
  if (model.actionTarget === 'auth') return `Sign in to ${name}`;
  if (model.tone === 'failed' || model.tone === 'partial') return `Retry ${name} sync`;
  return `Sync ${name}`;
}

export default function ProviderSyncStatus({
  model,
  institutionName,
  onAction = null,
  showIcon = true,
  showTooltip = true,
  displayLabel = null,
  iconClassName = '',
  textClassName = '',
  iconSize = 18,
}) {
  const canAct = Boolean(model.actionTarget && onAction);
  const visibleLabel = displayLabel || model.label;
  const tooltipRows = Array.isArray(model.tooltipRows) ? model.tooltipRows : [];
  const accessibleRows = showTooltip ? tooltipRows.filter((row) => row !== visibleLabel) : [];
  const accessibleStatus = visibleLabel === model.label
    ? visibleLabel
    : `${model.label}. ${visibleLabel}`;
  const accessibleTooltip = accessibleRows.length ? `. ${accessibleRows.join('. ')}` : '';
  const tooltip = showTooltip ? model.tooltip : '';
  return (
    <>
      {showIcon ? (
        <button
          type="button"
          className={`provider-sync-status-action ${iconClassName} ${model.colorClass}`.trim()}
          onClick={canAct ? onAction : undefined}
          disabled={!canAct}
          aria-label={getActionLabel(model, institutionName)}
        >
          <SyncStatusIcon model={model} size={iconSize} />
        </button>
      ) : null}
      <span
        className={`${textClassName} ${model.colorClass}`.trim()}
        data-tooltip={tooltip || undefined}
        aria-label={`${accessibleStatus}${accessibleTooltip}`}
        tabIndex={tooltip ? 0 : undefined}
      >
        {visibleLabel}
      </span>
    </>
  );
}
