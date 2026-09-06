const APP_CLOCK_OVERRIDE_KEY = '__breaktwentyAppClockOverride';

function readOverride() {
  if (typeof window === 'undefined') return '';
  return String(window[APP_CLOCK_OVERRIDE_KEY] || '').trim();
}

export function setAppClockOverride(isoValue = null) {
  if (typeof window === 'undefined') return;
  const normalized = String(isoValue || '').trim();
  if (normalized) {
    window[APP_CLOCK_OVERRIDE_KEY] = normalized;
  } else {
    delete window[APP_CLOCK_OVERRIDE_KEY];
  }
}

export function getAppNow() {
  const override = readOverride();
  if (override) {
    const parsed = new Date(override);
    if (!Number.isNaN(parsed.getTime())) return parsed;
  }
  return new Date();
}

export function getAppNowMs() {
  return getAppNow().getTime();
}

export function getAppClockOverride() {
  return readOverride() || null;
}
