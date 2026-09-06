import React from 'react';
import { LuCalendarDays } from 'react-icons/lu';
import ControlChevron from './ControlChevron';

function joinClassNames(...values) {
  return values.filter(Boolean).join(' ');
}

function TimelineTrigger({
  isOpen = false,
  summary,
  onClick,
  className = '',
  controls,
  ariaLabel = 'Timeline',
  showSummaryTitle = true,
}) {
  return (
    <button
      type="button"
      className={joinClassNames('scope-selector-trigger', 'timeline-selector-trigger', 'app-control-root', isOpen ? 'is-open' : '', className)}
      aria-haspopup="dialog"
      aria-expanded={isOpen}
      aria-controls={controls}
      aria-label={`${ariaLabel}: ${summary}`}
      onClick={onClick}
    >
      <span className="scope-selector-icon timeline-selector-icon app-control-icon" aria-hidden="true">
        <LuCalendarDays />
      </span>
      <span className="scope-selector-summary timeline-selector-summary app-control-label" title={showSummaryTitle ? summary : undefined}>{summary}</span>
      <span className={`scope-selector-chevron timeline-selector-chevron app-control-chevron ${isOpen ? 'is-open' : ''}`.trim()} aria-hidden="true">
        <ControlChevron />
      </span>
    </button>
  );
}

export default TimelineTrigger;
