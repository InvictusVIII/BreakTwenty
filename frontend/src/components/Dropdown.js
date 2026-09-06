import React, { useMemo, useRef, useState } from 'react';
import AnchoredPopover from './AnchoredPopover';
import ControlChevron from './ControlChevron';
import './Dropdown.css';

const CURRENCY_OPTION_HEIGHT = 38;
const CURRENCY_OPTION_GAP = 6;
const CURRENCY_POPOVER_WIDTH = 96;
const DROPDOWN_LIST_VERTICAL_PADDING = 16;

// Reusable themed select that matches the app's dark surfaces — unlike a native
// browser select, its open menu is fully styleable. Options are `{ value, label }`.
function Dropdown({
  value,
  options = [],
  onChange,
  placeholder = 'Select…',
  ariaLabel = 'Select',
  className = '',
  popoverClassName = '',
  fitToOptions = false,
  align = 'left',
  maxHeight = 320,
  onOpenChange,
  disabled = false,
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [anchorRect, setAnchorRect] = useState(null);
  const triggerRef = useRef(null);

  const selected = useMemo(
    () => options.find((o) => String(o.value) === String(value)) || null,
    [options, value],
  );
  const isCurrencyDropdown = className.split(/\s+/).includes('currency-dropdown');
  const effectiveMaxHeight = useMemo(() => {
    if (!isCurrencyDropdown) return maxHeight;
    const optionCount = Math.max(options.length, 1);
    const noScrollHeight = (
      optionCount * CURRENCY_OPTION_HEIGHT
      + Math.max(optionCount - 1, 0) * CURRENCY_OPTION_GAP
      + DROPDOWN_LIST_VERTICAL_PADDING
    );
    return Math.max(maxHeight, noScrollHeight);
  }, [isCurrencyDropdown, maxHeight, options.length]);
  const fitLabelWidth = useMemo(() => {
    if (!fitToOptions) return null;
    const labels = [...options.map((o) => o.label), placeholder].map((label) => String(label || ''));
    return `${Math.max(...labels.map((label) => label.length), 1)}ch`;
  }, [fitToOptions, options, placeholder]);
  const dropdownClassName = [
    'app-dropdown',
    className,
    fitToOptions ? 'is-fit-to-options' : '',
  ].filter(Boolean).join(' ');
  const popoverWidth = anchorRect
    ? (isCurrencyDropdown ? Math.max(anchorRect.width, CURRENCY_POPOVER_WIDTH) : anchorRect.width)
    : undefined;
  const popoverAlign = isCurrencyDropdown ? 'center' : align;

  const open = () => {
    if (disabled) return;
    if (triggerRef.current) setAnchorRect(triggerRef.current.getBoundingClientRect());
    setIsOpen(true);
    if (onOpenChange) onOpenChange(true);
  };
  const close = () => {
    setIsOpen(false);
    if (onOpenChange) onOpenChange(false);
  };

  return (
    <div
      className={dropdownClassName}
      style={fitLabelWidth ? { '--dropdown-fit-label-width': fitLabelWidth } : undefined}
    >
      <button
        type="button"
        ref={triggerRef}
        className={`app-dropdown-trigger app-control-root ${isOpen ? 'is-open' : ''}`.trim()}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => (isOpen ? close() : open())}
      >
        <span className={`app-dropdown-value app-control-label ${selected ? '' : 'is-placeholder'}`.trim()}>
          {selected ? selected.label : placeholder}
        </span>
        <span className="app-dropdown-chevron app-control-chevron" aria-hidden="true">
          <ControlChevron />
        </span>
      </button>
      <AnchoredPopover
        isOpen={isOpen}
        anchorRect={anchorRect}
        width={popoverWidth}
        clampHeight={effectiveMaxHeight}
        align={popoverAlign}
        onDismiss={close}
        className={`app-dropdown-popover app-surface-button-scope ${isCurrencyDropdown ? 'currency-dropdown-popover' : ''} ${popoverClassName}`.trim()}
        role="listbox"
        ariaLabel={ariaLabel}
      >
        <div className="app-dropdown-list" style={{ maxHeight: effectiveMaxHeight }}>
          {options.map((o) => (
            <button
              key={String(o.value)}
              type="button"
              role="option"
              aria-selected={String(o.value) === String(value)}
              className={`app-dropdown-option app-control-root ${String(o.value) === String(value) ? 'is-selected' : ''} ${isCurrencyDropdown ? 'is-currency-option' : ''}`.trim()}
              onClick={() => { onChange(o.value); close(); }}
            >
              <span className="app-control-label">{o.label}</span>
            </button>
          ))}
        </div>
      </AnchoredPopover>
    </div>
  );
}

export default Dropdown;
