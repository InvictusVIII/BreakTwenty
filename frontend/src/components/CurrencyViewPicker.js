import React, { useCallback, useRef, useState } from 'react';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import ControlChevron from './ControlChevron';
import './CurrencyViewPicker.css';

// App-wide currency picker (rendered once in the toolbar). `value` is the active
// currency code, `options` the selectable codes, and `onChange(code)` fires on
// select. While `busy` the trigger is disabled and shows a spinner — used while
// a currency switch persists to Settings and re-fetches FX rates.
export default function CurrencyViewPicker({
  value,
  options,
  onChange,
  panelId = 'currency-view-panel',
  ariaLabel = 'Display currency',
  busy = false,
}) {
  const [isOpen, setIsOpen] = useState(false);
  const ref = useRef(null);
  const close = useCallback(() => setIsOpen(false), []);
  useDismissibleLayer({
    open: isOpen,
    ref,
    onDismiss: close,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

  return (
    <div className="investments-filter-popover investments-currency-popover" ref={ref}>
      <button
        type="button"
        className={`investments-filter-trigger investments-currency-trigger app-control-root ${isOpen ? 'is-open' : ''} ${busy ? 'is-busy' : ''}`.trim()}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        aria-controls={panelId}
        aria-label={`${ariaLabel}: ${value}`}
        aria-busy={busy}
        disabled={busy}
        onClick={() => setIsOpen((previous) => !previous)}
      >
        <span className="investments-currency-trigger-value currency-picker-value app-control-label">{value}</span>
        <span className={`investments-filter-trigger-chevron app-control-chevron ${isOpen ? 'is-open' : ''}`.trim()} aria-hidden="true">
          {busy
            ? <span className="currency-picker-spinner" />
            : <ControlChevron />}
        </span>
      </button>
      <div
        id={panelId}
        role="menu"
        aria-label={ariaLabel}
        className={`investments-filter-panel investments-currency-panel ${isOpen ? 'is-open' : ''}`.trim()}
        aria-hidden={!isOpen}
      >
        <div className="income-timeline-menu-list" role="none">
          {options.map((option) => {
            const isSelected = value === option;
            return (
              <button
                key={option}
                type="button"
                role="menuitemradio"
                aria-checked={isSelected}
                className={`income-timeline-menu-item ${isSelected ? 'is-selected' : ''}`.trim()}
                onClick={() => { onChange(option); setIsOpen(false); }}
              >
                <span className="income-timeline-menu-item-copy">
                  <span className="income-timeline-menu-item-label">{option}</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
