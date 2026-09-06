import { getAppNow } from './appClock';

export function parseLocalDateValue(dateStr) {
  const value = String(dateStr || '').slice(0, 10);
  const [year, month, day] = value.split('-').map(Number);
  if (!year || !month || !day) return null;
  return new Date(year, month - 1, day);
}

export function toLocalDateValue(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

export function todayLocalDateValue() {
  return toLocalDateValue(getAppNow());
}

export function formatShortDateValue(dateStr, fallback = '') {
  if (!dateStr) return fallback;
  const date = parseLocalDateValue(dateStr);
  if (!date) return fallback;

  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

export function formatLongDateValue(dateStr, fallback = '') {
  const date = parseLocalDateValue(dateStr);
  if (!date) return fallback;

  return date.toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  });
}
