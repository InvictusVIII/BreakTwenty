import React, { useMemo, useState } from 'react';
import TriangleIcon from './TriangleIcon';
import { getAppNow } from '../utils/appClock';

const WEEKDAY_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function parseDateValue(value) {
  if (!value) return null;
  const [year, month, day] = value.split('-').map(Number);
  if (!year || !month || !day) return null;
  return new Date(year, month - 1, day);
}

function formatDateValue(date) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
    return '';
  }

  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function startOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function addMonths(date, months) {
  return new Date(date.getFullYear(), date.getMonth() + months, 1);
}

function addDays(date, days) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
}

function compareDateValues(left, right) {
  if (!left && !right) return 0;
  if (!left) return -1;
  if (!right) return 1;
  return left.localeCompare(right);
}

function isSameDate(left, right) {
  return Boolean(left) && Boolean(right) && left === right;
}

function formatSummaryDate(value) {
  const parsed = parseDateValue(value);
  if (!parsed) return 'Select date';
  return parsed.toLocaleDateString('en-CA', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function formatMonthHeading(date) {
  return date.toLocaleDateString('en-CA', {
    month: 'long',
    year: 'numeric',
  });
}

function getCalendarDays(monthDate) {
  const monthStart = startOfMonth(monthDate);
  const gridStart = addDays(monthStart, -monthStart.getDay());

  return Array.from({ length: 42 }, (_, index) => {
    const date = addDays(gridStart, index);
    return {
      date,
      value: formatDateValue(date),
      dayNumber: date.getDate(),
      isCurrentMonth: date.getMonth() === monthDate.getMonth(),
    };
  });
}

function getInclusiveDayCount(startValue, endValue) {
  const start = parseDateValue(startValue);
  const end = parseDateValue(endValue);
  if (!start || !end) return null;
  return Math.round((end.getTime() - start.getTime()) / 86400000) + 1;
}

function getYearPageStart(date) {
  const year = date.getFullYear();
  return year - (year % 12);
}

function clampYearPageStart(yearPageStart, currentYear) {
  return Math.min(yearPageStart, getYearPageStart(new Date(currentYear, 0, 1)));
}

function TimelineCustomRangePicker({
  startDate,
  endDate,
  onApply,
  onCancel,
  mode = 'range',
}) {
  const isSingle = mode === 'single';
  const today = useMemo(() => {
    const current = getAppNow();
    return new Date(current.getFullYear(), current.getMonth(), current.getDate());
  }, []);
  const todayValue = useMemo(() => formatDateValue(today), [today]);
  const currentYear = today.getFullYear();
  const currentMonthIndex = today.getMonth();
  const [draftRange, setDraftRange] = useState({ start: startDate || '', end: endDate || '' });
  const [hoverDate, setHoverDate] = useState('');
  const [visibleMonth, setVisibleMonth] = useState(() => {
    const baseDate = parseDateValue(startDate) || parseDateValue(endDate) || getAppNow();
    return startOfMonth(baseDate > today ? today : baseDate);
  });
  const [viewMode, setViewMode] = useState('days');
  const [activeBoundary, setActiveBoundary] = useState(startDate && !endDate ? 'end' : 'start');
  const [yearPageStart, setYearPageStart] = useState(() => {
    const baseDate = parseDateValue(startDate) || parseDateValue(endDate) || getAppNow();
    return clampYearPageStart(getYearPageStart(baseDate), currentYear);
  });

  const previewEndDate = useMemo(() => {
    if (draftRange.end) {
      return draftRange.end;
    }

    if (
      activeBoundary === 'end'
      && draftRange.start
      && hoverDate
      && compareDateValues(hoverDate, draftRange.start) >= 0
    ) {
      return hoverDate;
    }

    return '';
  }, [activeBoundary, draftRange.end, draftRange.start, hoverDate]);

  const selectedDayCount = getInclusiveDayCount(draftRange.start, draftRange.end || draftRange.start);
  const isEndSelectionDisabled = !draftRange.start;
  const calendarDays = useMemo(() => getCalendarDays(visibleMonth), [visibleMonth]);
  const visibleYearOptions = useMemo(
    () => Array.from({ length: 12 }, (_, index) => yearPageStart + index).filter((year) => year <= currentYear),
    [currentYear, yearPageStart]
  );
  const isAtCurrentMonth = visibleMonth.getFullYear() === currentYear && visibleMonth.getMonth() === currentMonthIndex;
  const isAtCurrentYear = visibleMonth.getFullYear() === currentYear;
  const isAtCurrentYearPage = yearPageStart + 11 >= currentYear;

  const beginStartSelection = () => {
    setActiveBoundary('start');
    setViewMode('days');
    const nextBaseDate = parseDateValue(draftRange.start) || parseDateValue(draftRange.end) || visibleMonth;
    const boundedBaseDate = nextBaseDate > today ? today : nextBaseDate;
    setVisibleMonth(startOfMonth(boundedBaseDate));
    setYearPageStart(clampYearPageStart(getYearPageStart(boundedBaseDate), currentYear));
  };

  const beginEndSelection = () => {
    if (!draftRange.start) {
      beginStartSelection();
      return;
    }

    setActiveBoundary('end');
    setViewMode('days');
    const nextBaseDate = parseDateValue(draftRange.end || draftRange.start) || visibleMonth;
    const boundedBaseDate = nextBaseDate > today ? today : nextBaseDate;
    setVisibleMonth(startOfMonth(boundedBaseDate));
    setYearPageStart(clampYearPageStart(getYearPageStart(boundedBaseDate), currentYear));
  };

  const handleSelectDay = (value) => {
    const selectedDate = parseDateValue(value);
    if (!selectedDate || compareDateValues(value, todayValue) > 0) {
      return;
    }

    if (isSingle) {
      // Single-date mode: one click selects the day and commits immediately.
      setDraftRange({ start: value, end: value });
      onApply(value);
      return;
    }

    if (activeBoundary === 'start' || !draftRange.start) {
      setDraftRange({ start: value, end: '' });
      setActiveBoundary('end');
      setVisibleMonth(startOfMonth(selectedDate));
      setYearPageStart(getYearPageStart(selectedDate));
      setHoverDate('');
      return;
    }

    if (compareDateValues(value, draftRange.start) < 0) {
      setDraftRange({ start: value, end: '' });
      setActiveBoundary('end');
      setVisibleMonth(startOfMonth(selectedDate));
      setYearPageStart(getYearPageStart(selectedDate));
      setHoverDate('');
      return;
    }

    setDraftRange((previous) => ({
      start: previous.start,
      end: value,
    }));
    setVisibleMonth(startOfMonth(selectedDate));
    setYearPageStart(getYearPageStart(selectedDate));
    setHoverDate('');
  };

  const handleSelectMonth = (monthIndex) => {
    if (visibleMonth.getFullYear() === currentYear && monthIndex > currentMonthIndex) {
      return;
    }
    const nextMonth = new Date(visibleMonth.getFullYear(), monthIndex, 1);
    setVisibleMonth(nextMonth);
    setViewMode('days');
    setYearPageStart(clampYearPageStart(getYearPageStart(nextMonth), currentYear));
  };

  const handleSelectYear = (year) => {
    if (year > currentYear) {
      return;
    }
    const nextMonth = new Date(year, visibleMonth.getMonth(), 1);
    setVisibleMonth(nextMonth);
    setViewMode('months');
    setYearPageStart(clampYearPageStart(getYearPageStart(nextMonth), currentYear));
  };

  const handlePreviousView = () => {
    if (viewMode === 'days') {
      setVisibleMonth((previous) => addMonths(previous, -1));
      return;
    }

    if (viewMode === 'months') {
      setVisibleMonth((previous) => new Date(previous.getFullYear() - 1, previous.getMonth(), 1));
      return;
    }

    setYearPageStart((previous) => previous - 12);
  };

  const handleNextView = () => {
    if (viewMode === 'days') {
      if (isAtCurrentMonth) {
        return;
      }
      setVisibleMonth((previous) => addMonths(previous, 1));
      return;
    }

    if (viewMode === 'months') {
      if (isAtCurrentYear) {
        return;
      }
      setVisibleMonth((previous) => new Date(previous.getFullYear() + 1, previous.getMonth(), 1));
      return;
    }

    if (isAtCurrentYearPage) {
      return;
    }

    setYearPageStart((previous) => clampYearPageStart(previous + 12, currentYear));
  };

  const handleHeaderTitleClick = () => {
    if (viewMode === 'days') {
      setViewMode('months');
      return;
    }

    if (viewMode === 'months') {
      setYearPageStart(getYearPageStart(visibleMonth));
      setViewMode('years');
    }
  };

  const handleApply = () => {
    if (!draftRange.start && !draftRange.end) {
      return;
    }

    const normalizedRange = {
      start: draftRange.start || draftRange.end,
      end: draftRange.end || draftRange.start,
    };

    onApply(normalizedRange);
  };

  const rangeStepLabel = isSingle
    ? 'Choose a date'
    : activeBoundary === 'start' ? 'Choosing start date' : 'Choosing end date';
  const rangeStepMeta = isSingle
    ? 'Pick a day from the calendar.'
    : activeBoundary === 'start'
      ? 'Pick the first day in the range.'
      : 'Pick the last day in the range.';
  const headerTitle = viewMode === 'days'
    ? formatMonthHeading(visibleMonth)
    : viewMode === 'months'
      ? String(visibleMonth.getFullYear())
      : `${yearPageStart} - ${Math.min(yearPageStart + 11, currentYear)}`;

  return (
    <div className="timeline-range-picker">
      <div className="timeline-range-picker-header">
        <div className="timeline-range-picker-header-copy">
          <span className="timeline-range-picker-step">{rangeStepLabel}</span>
          <span className="timeline-range-picker-step-meta">{rangeStepMeta}</span>
        </div>
      </div>

      {!isSingle && (
        <div className="timeline-range-picker-summary">
          <button
            type="button"
            className={`timeline-range-picker-summary-item ${activeBoundary === 'start' ? 'is-active' : ''}`.trim()}
            onClick={beginStartSelection}
          >
            <span className="timeline-range-picker-summary-label">Start</span>
            <span className="timeline-range-picker-summary-value">{formatSummaryDate(draftRange.start)}</span>
          </button>
          <button
            type="button"
            className={`timeline-range-picker-summary-item ${activeBoundary === 'end' ? 'is-active' : ''} ${isEndSelectionDisabled ? 'is-disabled' : ''}`.trim()}
            disabled={isEndSelectionDisabled}
            onClick={beginEndSelection}
          >
            <span className="timeline-range-picker-summary-label">End</span>
            <span className="timeline-range-picker-summary-value">{formatSummaryDate(draftRange.end || draftRange.start)}</span>
          </button>
          <div className="timeline-range-picker-summary-item">
            <span className="timeline-range-picker-summary-label">Length</span>
            <span className="timeline-range-picker-summary-value">
              {selectedDayCount ? `${selectedDayCount} ${selectedDayCount === 1 ? 'day' : 'days'}` : 'Select range'}
            </span>
          </div>
        </div>
      )}

      <section className="timeline-range-picker-calendar" aria-label="Custom timeline range picker">
        <div className="timeline-range-picker-calendar-header">
          <button
            type="button"
            className="timeline-range-picker-nav app-control-root"
            onClick={handlePreviousView}
            aria-label={viewMode === 'days' ? 'Previous month' : viewMode === 'months' ? 'Previous year' : 'Previous years'}
          >
            <span className="app-control-icon" aria-hidden="true"><TriangleIcon direction="left" /></span>
          </button>
          <button
            type="button"
            className={`timeline-range-picker-title app-control-root ${viewMode === 'years' ? 'is-static' : ''}`.trim()}
            onClick={handleHeaderTitleClick}
            disabled={viewMode === 'years'}
          >
            <span className="app-control-label">{headerTitle}</span>
          </button>
          <button
            type="button"
            className="timeline-range-picker-nav app-control-root"
            onClick={handleNextView}
            aria-label={viewMode === 'days' ? 'Next month' : viewMode === 'months' ? 'Next year' : 'Next years'}
            disabled={viewMode === 'days' ? isAtCurrentMonth : viewMode === 'months' ? isAtCurrentYear : isAtCurrentYearPage}
          >
            <span className="app-control-icon" aria-hidden="true"><TriangleIcon direction="right" /></span>
          </button>
        </div>

        {viewMode === 'days' ? (
          <>
            <div className="timeline-range-picker-weekdays" aria-hidden="true">
              {WEEKDAY_LABELS.map((dayLabel) => (
                <span key={dayLabel} className="timeline-range-picker-weekday">
                  {dayLabel}
                </span>
              ))}
            </div>

            <div className="timeline-range-picker-grid timeline-range-picker-grid-days">
              {calendarDays.map((day) => {
                const isFutureDay = compareDateValues(day.value, todayValue) > 0;
                const isCurrentDay = day.value === todayValue;
                const isSelectedStart = isSameDate(day.value, draftRange.start);
                const resolvedEnd = previewEndDate;
                const isSelectedEnd = isSameDate(day.value, resolvedEnd);
                const isInRange = Boolean(
                  draftRange.start
                  && resolvedEnd
                  && !isFutureDay
                  && compareDateValues(day.value, draftRange.start) >= 0
                  && compareDateValues(day.value, resolvedEnd) <= 0
                );

                return (
                  <button
                    key={day.value}
                    type="button"
                    className={[
                      'timeline-range-picker-day',
                      day.isCurrentMonth ? '' : 'is-outside-month',
                      isFutureDay ? 'is-disabled' : '',
                      isCurrentDay ? 'is-current' : '',
                      isInRange ? 'is-in-range' : '',
                      isSelectedStart ? 'is-range-start' : '',
                      isSelectedEnd ? 'is-range-end' : '',
                    ].filter(Boolean).join(' ')}
                    onClick={() => handleSelectDay(day.value)}
                    disabled={isFutureDay}
                    onMouseEnter={() => {
                      if (!isFutureDay && activeBoundary === 'end' && draftRange.start && !draftRange.end) {
                        setHoverDate(day.value);
                      }
                    }}
                    onMouseLeave={() => {
                      if (activeBoundary === 'end' && draftRange.start && !draftRange.end) {
                        setHoverDate('');
                      }
                    }}
                  >
                    <span>{day.dayNumber}</span>
                  </button>
                );
              })}
            </div>
          </>
        ) : null}

        {viewMode === 'months' ? (
          <div className="timeline-range-picker-grid timeline-range-picker-grid-months">
            {MONTH_LABELS.map((label, monthIndex) => {
              const isFutureMonth = visibleMonth.getFullYear() === currentYear && monthIndex > currentMonthIndex;
              const isCurrentMonth = visibleMonth.getFullYear() === currentYear && monthIndex === currentMonthIndex;

              return (
                <button
                  key={label}
                  type="button"
                  className={`timeline-range-picker-cell ${isCurrentMonth ? 'is-selected' : ''} ${isFutureMonth ? 'is-disabled' : ''}`.trim()}
                  onClick={() => handleSelectMonth(monthIndex)}
                  disabled={isFutureMonth}
                >
                  {label}
                </button>
              );
            })}
          </div>
        ) : null}

        {viewMode === 'years' ? (
          <div className="timeline-range-picker-grid timeline-range-picker-grid-years">
            {visibleYearOptions.map((year) => {
              const isFutureYear = year > currentYear;

              return (
                <button
                  key={year}
                  type="button"
                  className={[
                    'timeline-range-picker-cell',
                    year === currentYear ? 'is-selected' : '',
                    isFutureYear ? 'is-disabled' : '',
                    year === currentYear ? 'is-current' : '',
                  ].filter(Boolean).join(' ')}
                  onClick={() => handleSelectYear(year)}
                  disabled={isFutureYear}
                >
                  {year}
                </button>
              );
            })}
          </div>
        ) : null}
      </section>

      <div className="timeline-range-picker-actions">
        <button type="button" className="btn-secondary app-control-root" onClick={onCancel}>
          <span className="app-control-label">Cancel</span>
        </button>
        {!isSingle && (
          <button
            type="button"
            className="btn-primary app-control-root"
            onClick={handleApply}
            disabled={!draftRange.start && !draftRange.end}
          >
            <span className="app-control-label">Apply</span>
          </button>
        )}
      </div>
    </div>
  );
}

export default TimelineCustomRangePicker;
