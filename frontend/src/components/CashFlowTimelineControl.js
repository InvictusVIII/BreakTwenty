import React, { useCallback, useLayoutEffect, useState, useRef } from 'react';
import { MdCheck } from 'react-icons/md';
import AnchoredPopover from './AnchoredPopover';
import CurrentPeriodIcon from './CurrentPeriodIcon';
import TimelineTrigger from './TimelineTrigger';
import TimelineCustomRangePicker from './TimelineCustomRangePicker';
import TriangleIcon from './TriangleIcon';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import useTimelineCustomRangeDraft from '../hooks/useTimelineCustomRangeDraft';
import { getAppNow } from '../utils/appClock';

/**
 * Cash-flow timeline control — a "window SIZE + navigation" model (shared by the
 * Cash Flow page and the Dashboard cash-flow panel).
 *
 * The dropdown picks the window SIZE only — Month / Quarter / Year / Year to
 * Date / All Time / Custom Range. The previous/next buttons navigate one window of that
 * size (calendar-aligned), and the ⊙ Today button jumps to the window
 * containing today. The trigger always shows the CONCRETE period ("June 2026",
 * "Q2 2026", "2026") — never a relative word — so stepping back can never
 * mislabel the view (the old flat preset list kept "This Month" checked even on
 * a January you'd stepped to).
 *
 * Month / Quarter / Year are navigable and calendar-aligned (a calendar month,
 * a calendar quarter Q1–Q4, a calendar year Jan–Dec). Year to Date / All Time /
 * Custom Range are fixed — their arrows + Today disable.
 *
 * State shape (owned by the parent): { periodKey, anchor: Date, customRange }
 * where `periodKey` is the size key. Exports `computeRange`, `computeLabel`,
 * `canStep`, `canStepForward`, `stepAnchor`, `isAtCurrent`,
 * `getInitialAnchorForPreset` so callers can derive API dates + control state.
 */

const TIMELINE_SIZES = [
  { key: 'month',   label: 'Month' },
  { key: 'quarter', label: 'Quarter' },
  { key: 'year',    label: 'Year' },
  { key: 'ytd',     label: 'Year to Date' },
  { key: 'all',     label: 'All Time' },
  { key: 'custom',  label: 'Custom Range' },
];
const NAVIGABLE_SIZES = new Set(['month', 'quarter', 'year']);
const CUSTOM_RANGE_POPOVER_WIDTH = 368;
const CUSTOM_RANGE_POPOVER_CLAMP_HEIGHT = 560;

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

function toLocalDateValue(d) {
  if (!d) return null;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function startOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
function endOfMonth(d) { return new Date(d.getFullYear(), d.getMonth() + 1, 0); }
function addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, 1); }
function quarterIndex(d) { return Math.floor(d.getMonth() / 3); }
function startOfQuarter(d) { return new Date(d.getFullYear(), quarterIndex(d) * 3, 1); }
function endOfQuarter(d) { return new Date(d.getFullYear(), quarterIndex(d) * 3 + 3, 0); }
function startOfYear(d) { return new Date(d.getFullYear(), 0, 1); }
function endOfYear(d) { return new Date(d.getFullYear(), 11, 31); }

// Every size's CURRENT window contains today, so "now" is the right anchor for
// all of them; `periodKey` is accepted for call-site symmetry.
export function getInitialAnchorForPreset(periodKey) {
  return getAppNow();
}

export function computeRange(periodKey, anchor, customRange) {
  const today = getAppNow();
  switch (periodKey) {
    case 'month':
      return { start: toLocalDateValue(startOfMonth(anchor)), end: toLocalDateValue(endOfMonth(anchor)) };
    case 'quarter':
      return { start: toLocalDateValue(startOfQuarter(anchor)), end: toLocalDateValue(endOfQuarter(anchor)) };
    case 'year':
      return { start: toLocalDateValue(startOfYear(anchor)), end: toLocalDateValue(endOfYear(anchor)) };
    case 'ytd':
      return { start: toLocalDateValue(startOfYear(today)), end: toLocalDateValue(today) };
    case 'all':
      // Send only `end` so the backend's "both missing → default to current
      // month" safety branch doesn't fire; missing `start` = truly unbounded.
      return { start: null, end: toLocalDateValue(today) };
    case 'custom':
      return { start: customRange?.start || null, end: customRange?.end || null };
    default:
      return { start: null, end: null };
  }
}

export function computeLabel(periodKey, anchor, customRange) {
  switch (periodKey) {
    case 'month':
      return `${MONTH_NAMES[anchor.getMonth()]} ${anchor.getFullYear()}`;
    case 'quarter':
      return `Q${quarterIndex(anchor) + 1} ${anchor.getFullYear()}`;
    case 'year':
      return `${anchor.getFullYear()}`;
    case 'ytd':
      return `${anchor.getFullYear()} YTD`;
    case 'all':
      return 'All Time';
    case 'custom':
      if (customRange?.start && customRange?.end) return `${customRange.start} – ${customRange.end}`;
      return 'Custom Range';
    default:
      return 'Period';
  }
}

export function canStep(periodKey) {
  return NAVIGABLE_SIZES.has(periodKey);
}

export function stepAnchor(periodKey, anchor, direction) {
  switch (periodKey) {
    case 'month': return addMonths(anchor, direction);
    case 'quarter': return addMonths(anchor, direction * 3);
    case 'year': return new Date(anchor.getFullYear() + direction, 0, 1);
    default: return anchor;
  }
}

function canStepForward(periodKey, anchor) {
  if (!canStep(periodKey)) return false;
  const now = getAppNow();
  const next = stepAnchor(periodKey, anchor, 1);
  switch (periodKey) {
    case 'month': return startOfMonth(next) <= startOfMonth(now);
    case 'quarter': return startOfQuarter(next) <= startOfQuarter(now);
    case 'year': return next.getFullYear() <= now.getFullYear();
    default: return false;
  }
}

// Whether the anchor is already on today's window — drives the Today button's
// disabled state. Non-navigable sizes are always treated as "current".
export function isAtCurrent(periodKey, anchor) {
  if (!canStep(periodKey)) return true;
  const now = getAppNow();
  switch (periodKey) {
    case 'month': return startOfMonth(anchor).getTime() === startOfMonth(now).getTime();
    case 'quarter': return startOfQuarter(anchor).getTime() === startOfQuarter(now).getTime();
    case 'year': return anchor.getFullYear() === now.getFullYear();
    default: return true;
  }
}

function CashFlowTimelineControl({ periodKey, anchor, customRange, onChange }) {
  const [open, setOpen] = useState(false);
  const [customRangeAnchorRect, setCustomRangeAnchorRect] = useState(null);
  const containerRef = useRef(null);
  const panelRef = useRef(null);

  const label = computeLabel(periodKey, anchor, customRange);
  const {
    isCustomCommitted,
    isCustomSelected,
    openCustomRangeDraft,
    clearCustomRangeDraft,
  } = useTimelineCustomRangeDraft({
    isOpen: open,
    committedKey: periodKey,
    customKey: 'custom',
  });
  const closeMenu = useCallback(() => {
    clearCustomRangeDraft();
    setCustomRangeAnchorRect(null);
    setOpen(false);
  }, [clearCustomRangeDraft]);

  useDismissibleLayer({
    open,
    onDismiss: closeMenu,
    ref: containerRef,
    ignoreSelector: '.timeline-range-picker-floating-popover',
  });
  useLayoutEffect(() => {
    if (open && isCustomSelected && panelRef.current) {
      setCustomRangeAnchorRect(panelRef.current.getBoundingClientRect());
    }
  }, [isCustomSelected, open]);
  const stepEnabled = canStep(periodKey);
  const forwardEnabled = canStepForward(periodKey, anchor);
  const todayEnabled = stepEnabled && !isAtCurrent(periodKey, anchor);

  const handleStep = (direction) => {
    if (!stepEnabled) return;
    if (direction > 0 && !forwardEnabled) return;  // no stepping into the future
    onChange({ periodKey, anchor: stepAnchor(periodKey, anchor, direction), customRange });
  };

  const handleToday = () => {
    if (!stepEnabled) return;
    onChange({ periodKey, anchor: getInitialAnchorForPreset(periodKey), customRange });
  };

  const handleSelectSize = (size) => {
    if (size.key === 'custom') {
      // Reveal the portaled calendar and keep the panel open — matches the other
      // pages' custom-range behaviour. The period key commits only on Apply.
      openCustomRangeDraft();
      return;
    }
    setOpen(false);
    clearCustomRangeDraft();
    setCustomRangeAnchorRect(null);
    // Selecting a size jumps to its CURRENT window (then the arrows navigate).
    onChange({ periodKey: size.key, anchor: getInitialAnchorForPreset(size.key), customRange });
  };

  const handleApplyCustomRange = (range) => {
    onChange({ periodKey: 'custom', anchor, customRange: { start: range.start || '', end: range.end || '' } });
    clearCustomRangeDraft();
    setCustomRangeAnchorRect(null);
    setOpen(false);
  };

  const handleCancelCustomRange = () => {
    clearCustomRangeDraft();
    setCustomRangeAnchorRect(null);
    setOpen(false);
  };

  return (
    <div
      className="investments-filter-popover dashboard-timeline-popover cf-timeline-popover"
      ref={containerRef}
    >
      <button
        type="button"
        className="cf-timeline-step-btn cf-timeline-prev-btn app-control-root"
        disabled={!stepEnabled}
        onClick={() => handleStep(-1)}
        aria-label="Previous period"
        data-tooltip="Previous period"
      >
        <span className="app-control-icon" aria-hidden="true">
          <TriangleIcon direction="left" />
        </span>
      </button>

      <TimelineTrigger
        isOpen={open}
        summary={label}
        controls="cash-flow-timeline-panel"
        className="dashboard-timeline-trigger cf-timeline-trigger"
        ariaLabel="Cash Flow period"
        showSummaryTitle={false}
        onClick={() => setOpen((p) => !p)}
      />

      <button
        type="button"
        className="cf-timeline-step-btn cf-timeline-next-btn app-control-root"
        disabled={!stepEnabled || !forwardEnabled}
        onClick={() => handleStep(1)}
        aria-label="Next period"
        data-tooltip="Next period"
      >
        <span className="app-control-icon" aria-hidden="true">
          <TriangleIcon direction="right" />
        </span>
      </button>

      <button
        type="button"
        className="cf-timeline-step-btn cf-timeline-today-btn app-control-root"
        disabled={!todayEnabled}
        onClick={handleToday}
        aria-label="Jump to current period"
        data-tooltip="Jump to current period"
      >
        <span className="app-control-icon" aria-hidden="true">
          <CurrentPeriodIcon />
        </span>
      </button>

      <div
        id="cash-flow-timeline-panel"
        role="dialog"
        aria-label="Cash Flow period"
        className={`investments-filter-panel dashboard-timeline-panel timeline-range-panel ${open ? 'is-open' : ''}`.trim()}
        aria-hidden={!open}
        ref={panelRef}
      >
        <div className="income-timeline-menu-list" role="menu" aria-label="Cash Flow window size">
          {TIMELINE_SIZES.map((size) => {
            const isSelected = size.key === 'custom' ? isCustomSelected : size.key === periodKey;
            const isChecked = size.key === 'custom' ? isCustomCommitted : isSelected;
            return (
              <button
                key={size.key}
                type="button"
                role="menuitemradio"
                aria-checked={isChecked}
                className={`income-timeline-menu-item ${isSelected ? 'is-selected' : ''}`.trim()}
                onClick={() => handleSelectSize(size)}
              >
                <span className="income-timeline-menu-item-copy">
                  <span className="income-timeline-menu-item-label">{size.label}</span>
                </span>
                <span
                  className={`income-timeline-checkbox ${isChecked ? 'is-selected' : ''}`.trim()}
                  aria-hidden="true"
                >
                  {isChecked ? <MdCheck size={14} /> : null}
                </span>
              </button>
            );
          })}
        </div>

      </div>
      <AnchoredPopover
        isOpen={open && isCustomSelected}
        anchorRect={customRangeAnchorRect}
        width={CUSTOM_RANGE_POPOVER_WIDTH}
        clampHeight={CUSTOM_RANGE_POPOVER_CLAMP_HEIGHT}
        placement="left"
        crossAlign="start"
        onDismiss={closeMenu}
        className="timeline-range-picker-floating-popover app-surface-button-scope"
        role="dialog"
        ariaLabel="Custom cash flow range"
      >
        <TimelineCustomRangePicker
          startDate={customRange?.start || ''}
          endDate={customRange?.end || ''}
          onApply={handleApplyCustomRange}
          onCancel={handleCancelCustomRange}
        />
      </AnchoredPopover>
    </div>
  );
}

export default CashFlowTimelineControl;
