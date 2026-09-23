const assert = require('node:assert/strict');
const test = require('node:test');
const {
  ISOLATED_JOURNEY_ROUTES,
  ISOLATED_JOURNEY_VIEWPORTS,
} = require('./isolatedJourneyRoutes');

test('isolated journey covers every application route and responsive viewport tier', () => {
  assert.deepEqual(
    ISOLATED_JOURNEY_ROUTES.map((route) => route.path),
    [
      '/',
      '/accounts',
      '/holdings',
      '/transactions',
      '/cash-flow',
      '/settings',
      '/settings/categories',
      '/faq',
      '/licenses',
    ],
  );
  assert.deepEqual(
    ISOLATED_JOURNEY_VIEWPORTS.map((viewport) => viewport.name),
    ['canonical', 'compact'],
  );
  assert.deepEqual(
    ISOLATED_JOURNEY_ROUTES.filter((route) => route.visual).map((route) => route.name),
    ['dashboard', 'accounts', 'investments', 'transactions', 'cash-flow', 'settings'],
  );
});
