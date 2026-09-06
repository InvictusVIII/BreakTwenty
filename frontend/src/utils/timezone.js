export const DEFAULT_USER_TIMEZONE = 'UTC';
export const DEFAULT_USER_TIME_FORMAT = '24h';

export const USER_TIME_FORMAT_OPTIONS = [
  { key: '24h', label: '24-hour' },
  { key: '12h', label: '12-hour (AM/PM)' },
];

export function normalizeUserTimeFormat(value) {
  return String(value || '').trim().toLowerCase() === '12h'
    ? '12h'
    : DEFAULT_USER_TIME_FORMAT;
}

const FALLBACK_TIMEZONES = [
  'UTC',
  'America/St_Johns',
  'America/Halifax',
  'America/Moncton',
  'America/Glace_Bay',
  'America/Toronto',
  'America/Winnipeg',
  'America/Regina',
  'America/Edmonton',
  'America/Vancouver',
  'America/Whitehorse',
  'America/Dawson',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'Europe/London',
  'Europe/Paris',
  'Asia/Tokyo',
  'Australia/Sydney',
];

export function getBrowserTimezone() {
  try {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return String(timezone || '').trim() || DEFAULT_USER_TIMEZONE;
  } catch (_) {
    return DEFAULT_USER_TIMEZONE;
  }
}

export function getAvailableTimezones() {
  const runtimeTimezones =
    typeof Intl !== 'undefined' && typeof Intl.supportedValuesOf === 'function'
      ? Intl.supportedValuesOf('timeZone')
      : [];

  return [...new Set([
    DEFAULT_USER_TIMEZONE,
    getBrowserTimezone(),
    ...(runtimeTimezones.length > 0 ? runtimeTimezones : FALLBACK_TIMEZONES),
  ])].sort((left, right) => left.localeCompare(right));
}
