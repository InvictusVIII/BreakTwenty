import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { downloadCsv } from '../utils/exportCsv';
import { ExportToolbarIcon } from './ToolbarActionIcon';

// Per-page Export control: a compact download icon styled like the toolbar's
// balance-toggle eye, using the app's own tooltip (data-tooltip + aria-label).
// By default it self-portals into the shell's right-cluster `app-toolbar-export-slot`.
// Set `portal={false}` when a page owns the inline placement.
function CsvExportButton({ dataset, getParams, label = 'Export CSV', portal = true }) {
  const [busy, setBusy] = useState(false);

  const handleClick = async () => {
    setBusy(true);
    try {
      await downloadCsv(dataset, getParams());
    } catch (_) {
      // Best-effort download; failures (e.g. backend offline) are non-fatal here.
    } finally {
      setBusy(false);
    }
  };

  const tooltip = busy ? 'Exporting…' : label;
  const button = (
    <button
      type="button"
      className="top-toolbar-icon-btn csv-export-btn app-control-root"
      onClick={handleClick}
      disabled={busy}
      data-tooltip={tooltip}
      aria-label={tooltip}
    >
      <span className="app-control-icon" aria-hidden="true">
        <ExportToolbarIcon />
      </span>
    </button>
  );

  if (!portal) {
    return button;
  }

  const slot = typeof document === 'undefined' ? null : document.getElementById('app-toolbar-export-slot');
  return slot ? createPortal(button, slot) : null;
}

export default CsvExportButton;
