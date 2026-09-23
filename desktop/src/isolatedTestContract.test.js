const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
  ISOLATED_TEST_MARKER_PREFIX,
  ISOLATED_TEST_ROOT_PREFIX,
  assertIsolatedTestEnvironment,
  assertPackagedNativeSmokeEnvironment,
  configureIsolatedElectronPaths,
} = require('./isolatedTestContract');

function contractFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), ISOLATED_TEST_ROOT_PREFIX));
  const environment = {
    BREAKTWENTY_ISOLATED_TEST_MARKER: `${ISOLATED_TEST_MARKER_PREFIX}fixture`,
    BREAKTWENTY_ISOLATED_TEST_ROOT: root,
    BREAKTWENTY_ISOLATED_HOST_TEMP_ROOT: os.tmpdir(),
    BREAKTWENTY_DEVELOPMENT_USER_DATA_DIR: path.join(root, 'user-data'),
    BREAKTWENTY_DESKTOP_AUTH_DIR: path.join(root, 'desktop-auth'),
    BREAKTWENTY_ISOLATED_TEST_APP_ID: 'app.breaktwenty.test.fixture',
    BREAKTWENTY_BACKEND_API_URL: 'http://127.0.0.1:41001/api',
    BREAKTWENTY_FRONTEND_URL: 'http://127.0.0.1:41002',
    BREAKTWENTY_ISOLATED_NETWORK_POLICY: 'loopback-only',
    HOME: path.join(root, 'home'),
  };
  return { root, environment };
}

test('accepts a complete isolated desktop environment', (t) => {
  const fixture = contractFixture();
  t.after(() => fs.rmSync(fixture.root, { recursive: true, force: true }));
  const contract = assertIsolatedTestEnvironment(fixture.environment);
  assert.equal(contract.userData, path.join(fixture.root, 'user-data'));
  assert.equal(contract.backendOrigin, 'http://127.0.0.1:41001');
});

test('rejects normal profile paths and inherited secrets', (t) => {
  const fixture = contractFixture();
  t.after(() => fs.rmSync(fixture.root, { recursive: true, force: true }));
  assert.throws(
    () => assertIsolatedTestEnvironment({
      ...fixture.environment,
      BREAKTWENTY_DEVELOPMENT_USER_DATA_DIR: path.join(os.homedir(), '.config', 'BreakTwenty'),
    }),
    /must stay beneath the isolated test root/,
  );
  assert.throws(
    () => assertIsolatedTestEnvironment({
      ...fixture.environment,
      GH_TOKEN: 'must-not-enter-isolated-tests',
    }),
    /refuses inherited secret environment variable GH_TOKEN/,
  );
});

test('rejects external, shared-port, and production-identity configurations', (t) => {
  const fixture = contractFixture();
  t.after(() => fs.rmSync(fixture.root, { recursive: true, force: true }));
  assert.throws(
    () => assertIsolatedTestEnvironment({
      ...fixture.environment,
      BREAKTWENTY_FRONTEND_URL: 'https://example.com',
    }),
    /credential-free HTTP URL/,
  );
  assert.throws(
    () => assertIsolatedTestEnvironment({
      ...fixture.environment,
      BREAKTWENTY_FRONTEND_URL: 'http://127.0.0.1:41001',
    }),
    /separate allocated ports/,
  );
  assert.throws(
    () => assertIsolatedTestEnvironment({
      ...fixture.environment,
      BREAKTWENTY_ISOLATED_TEST_APP_ID: 'app.breaktwenty.desktop',
    }),
    /test-only application identity/,
  );
});

test('packaged native smoke requires every mutable path and external feature boundary', (t) => {
  const fixture = contractFixture();
  t.after(() => fs.rmSync(fixture.root, { recursive: true, force: true }));
  const packaged = {
    ...fixture.environment,
    BREAKTWENTY_BROWSER_RUNTIME_DIR: path.join(fixture.root, 'browser-runtimes'),
    BREAKTWENTY_DATA_DIR: path.join(fixture.root, 'data'),
    BREAKTWENTY_DB_PATH: path.join(fixture.root, 'data', 'breaktwenty.db'),
    BREAKTWENTY_DIAGNOSTIC_DIR: path.join(fixture.root, 'logs', 'providers'),
    BREAKTWENTY_LOG_DIR: path.join(fixture.root, 'logs'),
    BREAKTWENTY_ISOLATED_JOURNEY_MODE: 'packaged-native',
    BREAKTWENTY_BACKEND_MODE: 'embedded',
    BREAKTWENTY_DESKTOP_FRONTEND_MODE: 'build',
    BREAKTWENTY_DISABLE_AUTO_UPDATE: '1',
    BREAKTWENTY_DESKTOP_PREWARM_BROWSER_RUNTIME: '0',
  };
  assert.equal(assertPackagedNativeSmokeEnvironment(packaged).root, fixture.root);
  assert.throws(
    () => assertPackagedNativeSmokeEnvironment({
      ...packaged,
      BREAKTWENTY_DB_PATH: path.join(os.homedir(), 'breaktwenty.db'),
    }),
    /must stay beneath the isolated test root/,
  );
  assert.throws(
    () => assertPackagedNativeSmokeEnvironment({
      ...packaged,
      BREAKTWENTY_DISABLE_AUTO_UPDATE: '0',
    }),
    /must disable update networking/,
  );
});

test('configures Electron native paths beneath the isolated root before main loads', (t) => {
  const fixture = contractFixture();
  t.after(() => fs.rmSync(fixture.root, { recursive: true, force: true }));
  const environment = {
    ...fixture.environment,
    APPDATA: path.join(fixture.root, 'app-data'),
    TMPDIR: path.join(fixture.root, 'tmp'),
    XDG_CACHE_HOME: path.join(fixture.root, 'cache'),
  };
  const configured = {};
  const contract = assertIsolatedTestEnvironment(environment);
  const paths = configureIsolatedElectronPaths({
    setPath: (name, value) => { configured[name] = value; },
  }, environment, contract);

  assert.deepEqual(configured, paths);
  assert.deepEqual(Object.keys(configured).sort(), ['appData', 'cache', 'home', 'temp']);
  for (const target of Object.values(configured)) {
    assert.ok(fs.statSync(target).isDirectory());
    assert.ok(target.startsWith(contract.root));
  }
});
