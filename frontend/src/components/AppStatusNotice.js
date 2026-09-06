import React from 'react';
import { MdClose, MdWarningAmber } from 'react-icons/md';
import './AppStatusNotice.css';

function AppStatusNotice({
  title,
  message,
  onDismiss,
  actionLabel,
  onAction,
  actionDisabled = false,
}) {
  if (!message) return null;

  return (
    <div className="app-status-notice" role="alert">
      <MdWarningAmber className="app-status-notice-icon" aria-hidden="true" />
      <div className="app-status-notice-copy">
        <p className="app-status-notice-title">{title}</p>
        <p className="app-status-notice-message">{message}</p>
      </div>
      {(actionLabel && onAction) || onDismiss ? (
        <div className="app-status-notice-controls">
          {actionLabel && onAction ? (
            <button
              type="button"
              className="btn-secondary app-control-root app-status-notice-action"
              onClick={onAction}
              disabled={actionDisabled}
            >
              <span className="app-control-label">{actionLabel}</span>
            </button>
          ) : null}
          {onDismiss ? (
            <button
              type="button"
              className="modal-close app-status-notice-dismiss"
              onClick={onDismiss}
              aria-label="Dismiss notice"
            >
              <MdClose aria-hidden="true" />
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default AppStatusNotice;
