const MAX_RENDERER_PREFERENCE_KEY_LENGTH = 192;
const MAX_RENDERER_PREFERENCE_VALUE_LENGTH = 256 * 1024;
const MAX_RENDERER_PREFERENCES_TOTAL_LENGTH = 1024 * 1024;
const RENDERER_PREFERENCE_KEY_PATTERN = /^breaktwenty(?:[._:-])[A-Za-z0-9._:-]+$/;

function validateRendererPreferenceKey(key) {
  const normalized = String(key || '');
  if (
    !normalized
    || normalized.length > MAX_RENDERER_PREFERENCE_KEY_LENGTH
    || !RENDERER_PREFERENCE_KEY_PATTERN.test(normalized)
  ) {
    throw new Error('BreakTwenty rejected an invalid renderer preference key.');
  }
  return normalized;
}

function validateRendererPreferenceValue(value) {
  if (typeof value !== 'string' || value.length > MAX_RENDERER_PREFERENCE_VALUE_LENGTH) {
    throw new Error('BreakTwenty rejected an invalid renderer preference value.');
  }
  return value;
}

function normalizeRendererPreferences(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {};
  }
  const normalized = {};
  for (const [key, preferenceValue] of Object.entries(value)) {
    try {
      normalized[validateRendererPreferenceKey(key)] = validateRendererPreferenceValue(preferenceValue);
    } catch (_error) {
    }
  }
  return normalized;
}

function assertRendererPreferencesSize(preferences) {
  const totalLength = Object.entries(preferences).reduce(
    (total, [key, value]) => total + key.length + value.length,
    0,
  );
  if (totalLength > MAX_RENDERER_PREFERENCES_TOTAL_LENGTH) {
    throw new Error('BreakTwenty renderer preferences exceed the storage limit.');
  }
}

function getRendererPreference(preferences, key) {
  const normalizedKey = validateRendererPreferenceKey(key);
  const normalized = normalizeRendererPreferences(preferences);
  return Object.prototype.hasOwnProperty.call(normalized, normalizedKey)
    ? normalized[normalizedKey]
    : null;
}

function setRendererPreference(preferences, key, value) {
  const normalizedKey = validateRendererPreferenceKey(key);
  const normalizedValue = validateRendererPreferenceValue(value);
  const next = {
    ...normalizeRendererPreferences(preferences),
    [normalizedKey]: normalizedValue,
  };
  assertRendererPreferencesSize(next);
  return next;
}

function removeRendererPreference(preferences, key) {
  const normalizedKey = validateRendererPreferenceKey(key);
  const next = normalizeRendererPreferences(preferences);
  delete next[normalizedKey];
  return next;
}

function listRendererPreferenceKeys(preferences) {
  return Object.keys(normalizeRendererPreferences(preferences)).sort();
}

module.exports = {
  getRendererPreference,
  listRendererPreferenceKeys,
  normalizeRendererPreferences,
  removeRendererPreference,
  setRendererPreference,
};
