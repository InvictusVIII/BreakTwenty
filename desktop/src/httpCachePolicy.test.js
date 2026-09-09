const assert = require('node:assert/strict');
const test = require('node:test');

const {
  clearLegacyDesktopHttpCaches,
  DESKTOP_HTTP_CACHE_POLICY_PREFERENCE,
  DESKTOP_HTTP_CACHE_POLICY_VERSION,
} = require('./httpCachePolicy');

test('clears legacy HTTP and code caches without dropping desktop preferences', async () => {
  const calls = [];
  let savedPreferences = null;
  const cleared = await clearLegacyDesktopHttpCaches({
    electronSession: {
      clearCache: async () => calls.push('http'),
      clearCodeCaches: async (options) => calls.push(['code', options]),
    },
    readPreferences: () => ({
      mainWindowZoomFactor: 1.2,
      rendererPreferences: { breaktwenty_theme_mode_v1: 'light' },
    }),
    writePreferences: (preferences) => {
      savedPreferences = preferences;
      return true;
    },
  });

  assert.equal(cleared, true);
  assert.deepEqual(calls, ['http', ['code', { urls: [] }]]);
  assert.equal(savedPreferences.mainWindowZoomFactor, 1.2);
  assert.deepEqual(savedPreferences.rendererPreferences, { breaktwenty_theme_mode_v1: 'light' });
  assert.equal(
    savedPreferences[DESKTOP_HTTP_CACHE_POLICY_PREFERENCE],
    DESKTOP_HTTP_CACHE_POLICY_VERSION,
  );
});

test('does not repeat cleanup after the current policy was recorded', async () => {
  let cacheClears = 0;
  const cleared = await clearLegacyDesktopHttpCaches({
    electronSession: {
      clearCache: async () => { cacheClears += 1; },
      clearCodeCaches: async () => { cacheClears += 1; },
    },
    readPreferences: () => ({
      [DESKTOP_HTTP_CACHE_POLICY_PREFERENCE]: DESKTOP_HTTP_CACHE_POLICY_VERSION,
    }),
    writePreferences: () => true,
  });

  assert.equal(cleared, false);
  assert.equal(cacheClears, 0);
});
