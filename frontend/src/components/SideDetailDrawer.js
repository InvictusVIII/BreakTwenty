import React from 'react';
import { MdClose } from 'react-icons/md';

function joinClassNames(...values) {
  return values.filter(Boolean).join(' ');
}

export function SideDetailDrawerPanel({
  meta = '',
  title,
  subtitle = '',
  leadingAction = null,
  onClose,
  className = '',
  children,
}) {
  return (
    <div className={joinClassNames('app-detail-drawer-panel', className)}>
      <div className="app-detail-drawer-header">
        {leadingAction ? (
          <div className="app-detail-drawer-title-row has-leading-action">
            <div className="app-detail-drawer-leading">{leadingAction}</div>
            <div className="app-detail-drawer-heading">
              {meta ? <span className="app-detail-drawer-meta">{meta}</span> : null}
              <h3 className="app-detail-drawer-title">{title}</h3>
              {subtitle ? <p className="app-detail-drawer-subtitle">{subtitle}</p> : null}
            </div>
          </div>
        ) : (
          <div className="app-detail-drawer-heading">
            {meta ? <span className="app-detail-drawer-meta">{meta}</span> : null}
            <h3 className="app-detail-drawer-title">{title}</h3>
            {subtitle ? <p className="app-detail-drawer-subtitle">{subtitle}</p> : null}
          </div>
        )}
        <button
          type="button"
          className="app-detail-drawer-close"
          aria-label="Close details"
          onClick={onClose}
        >
          <MdClose size={18} />
        </button>
      </div>
      <div className="app-detail-drawer-body">
        {children}
      </div>
    </div>
  );
}
