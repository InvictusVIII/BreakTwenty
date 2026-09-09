const assert = require('node:assert/strict');
const test = require('node:test');
const {
  getRendererPreference,
  listRendererPreferenceKeys,
  normalizeRendererPreferences,
  removeRendererPreference,
  setRendererPreference,
} = require('./rendererPreferences');

test('stores only bounded BreakTwenty renderer preferences', () => {
  const preferences = setRendererPreference({}, 'breaktwenty_theme_mode_v1', 'light');

  assert.equal(getRendererPreference(preferences, 'breaktwenty_theme_mode_v1'), 'light');
  assert.equal(getRendererPreference(preferences, 'breaktwenty_last_auto_sync'), null);
  assert.deepEqual(listRendererPreferenceKeys(preferences), ['breaktwenty_theme_mode_v1']);
  assert.throws(() => setRendererPreference(preferences, 'other_app_key', 'value'));
  assert.throws(() => setRendererPreference(preferences, 'breaktwenty_bad', 'x'.repeat((256 * 1024) + 1)));
});

test('normalizes invalid persisted entries and removes requested preferences', () => {
  const normalized = normalizeRendererPreferences({
    breaktwenty_hide_balances: 'true',
    unrelated: 'discarded',
    breaktwenty_invalid_value: 42,
  });

  assert.deepEqual(normalized, { breaktwenty_hide_balances: 'true' });
  assert.deepEqual(removeRendererPreference(normalized, 'breaktwenty_hide_balances'), {});
});
