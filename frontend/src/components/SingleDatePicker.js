import React, { useRef, useState } from 'react';
import { MdEvent } from 'react-icons/md';
import AnchoredPopover from './AnchoredPopover';
import TimelineCustomRangePicker from './TimelineCustomRangePicker';
import './SingleDatePicker.css';

// Match the toolbar calendar popover (`.timeline-range-picker-popover`,
// width: min(23rem, …)) so the single-date calendar reads identically.
const POPOVER_WIDTH = 368;
const POPOVER_CLAMP_HEIGHT = 560;

function formatDisplay(value) {
  if (!value) return '';
  const [year, month, day] = String(value).split('-').map(Number);
  if (!year || !month || !day) return value;
  return new Date(year, month - 1, day).toLocaleDateString('en-CA', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

// Standardized single-date field: a themed trigger that opens the app's calendar
// (`TimelineCustomRangePicker` in single mode) in an anchored popover. Replaces
// native `<input type="date">` everywhere so every date field matches the
// Dashboard / Accounts / Investments calendar. Value/onChange are `YYYY-MM-DD`.
function SingleDatePicker({
  value,
  onChange,
  placeholder = 'Select date',
  ariaLabel = 'Date',
  className = '',
  size = 'default',
  popoverPlacement = 'top',
  popoverAlign = 'left',
  popoverCrossAlign = 'start',
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [anchorRect, setAnchorRect] = useState(null);
  const triggerRef = useRef(null);

  const open = () => {
    if (triggerRef.current) setAnchorRect(triggerRef.current.getBoundingClientRect());
    setIsOpen(true);
  };
  const close = () => setIsOpen(false);

  const handleApply = (nextValue) => {
    onChange(nextValue);
    close();
  };

  const display = formatDisplay(value);

  return (
    <div className={`single-date-picker ${size === 'compact' ? 'single-date-picker--compact' : ''} ${className}`.trim()}>
      <button
        type="button"
        ref={triggerRef}
        className={`single-date-trigger app-control-root ${isOpen ? 'is-open' : ''}`.trim()}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        aria-label={ariaLabel}
        onClick={() => (isOpen ? close() : open())}
      >
        <span className={`single-date-value app-control-label ${display ? '' : 'is-placeholder'}`.trim()}>
          {display || placeholder}
        </span>
        <span className="single-date-icon app-control-icon" aria-hidden="true">
          <MdEvent />
        </span>
      </button>
      <AnchoredPopover
        isOpen={isOpen}
        anchorRect={anchorRect}
        width={POPOVER_WIDTH}
        clampHeight={POPOVER_CLAMP_HEIGHT}
        placement={popoverPlacement}
        align={popoverAlign}
        crossAlign={popoverCrossAlign}
        onDismiss={close}
        className="single-date-popover app-surface-button-scope"
        role="dialog"
        ariaLabel={ariaLabel}
      >
        <TimelineCustomRangePicker
          mode="single"
          startDate={value || ''}
          endDate={value || ''}
          onApply={handleApply}
          onCancel={close}
        />
      </AnchoredPopover>
    </div>
  );
}

export default SingleDatePicker;
