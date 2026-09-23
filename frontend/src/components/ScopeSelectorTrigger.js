import React from 'react';
import { LuLandmark } from 'react-icons/lu';
import ControlChevron from './ControlChevron';

function joinClassNames(...values) {
  return values.filter(Boolean).join(' ');
}

export default function ScopeSelectorTrigger({
  isOpen = false,
  summary,
  onClick,
  className = '',
  controls,
  ariaLabel = 'Scope',
}) {
  return (
    <button
      type="button"
      className={joinClassNames('scope-selector-trigger', 'app-control-root', isOpen ? 'is-open' : '', className)}
      aria-haspopup="dialog"
      aria-expanded={isOpen}
      aria-controls={controls}
      aria-label={ariaLabel}
      onClick={onClick}
    >
      <span className="scope-selector-icon app-control-icon" aria-hidden="true">
        <LuLandmark />
      </span>
      <span className="scope-selector-summary app-control-label">{summary}</span>
      <span className={`scope-selector-chevron app-control-chevron ${isOpen ? 'is-open' : ''}`.trim()} aria-hidden="true">
        <ControlChevron />
      </span>
    </button>
  );
}
