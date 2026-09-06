import React from 'react';

function InstitutionSyncProgress({
  title,
  subtitle,
  progress = null,
  progressLabel = '',
  wrapperClassName = 'modal-body modal-center',
}) {
  const className = `${wrapperClassName} institution-sync-progress`.trim();
  const percent = Number(progress?.percent);
  const hasPercent = progress?.percent !== null
    && progress?.percent !== undefined
    && Number.isFinite(percent);
  const boundedPercent = hasPercent ? Math.max(0, Math.min(100, percent)) : 0;

  return (
    <div className={className}>
      <div className="modal-spinner" />
      <p className="modal-2fa-text">{title}</p>
      {subtitle && <p className="modal-2fa-sub">{subtitle}</p>}
      {progress && (
        <>
          <div
            className={`institution-sync-progress-bar ${hasPercent ? '' : 'is-indeterminate'}`.trim()}
            role="progressbar"
            aria-label={progressLabel || title}
            aria-valuemin={hasPercent ? 0 : undefined}
            aria-valuemax={hasPercent ? 100 : undefined}
            aria-valuenow={hasPercent ? boundedPercent : undefined}
          >
            <span style={hasPercent ? { width: `${boundedPercent}%` } : undefined} />
          </div>
          {progressLabel && <p className="modal-2fa-sub">{progressLabel}</p>}
        </>
      )}
    </div>
  );
}

export default InstitutionSyncProgress;
