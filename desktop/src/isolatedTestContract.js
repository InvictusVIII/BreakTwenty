const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ISOLATED_TEST_MARKER_PREFIX = 'breaktwenty-isolated-test-v1:';
const ISOLATED_TEST_ROOT_PREFIX = 'breaktwenty-isolated-';
const PRODUCTION_APP_IDS = new Set([
  'app.breaktwenty.desktop',
]);
const FORBIDDEN_SECRET_ENVIRONMENT_KEYS = [
  'BREAKTWENTY_APP_ENCRYPTION_KEY',
  'BREAKTWENTY_DATABASE_ENCRYPTION_KEY',
  'BREAKTWENTY_KEY_BOOTSTRAP',
  'BREAKTWENTY_PRIVATE_UPDATE_TOKEN',
  'GH_TOKEN',
  'GITHUB_TOKEN',
  'CSC_LINK',
  'CSC_KEY_PASSWORD',
  'APPLE_API_KEY',
  'APPLE_API_KEY_ID',
  'APPLE_API_ISSUER',
  'APPLE_ID_PASSWORD',
  'AZURE_CLIENT_SECRET',
];

function isPathInside(parentPath, candidatePath) {
  const relative = path.relative(parentPath, candidatePath);
  return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
}

function requireIsolatedPath(rootPath, candidate, label) {
  if (!candidate || !path.isAbsolute(candidate)) {
    throw new Error(`${label} must be an absolute path.`);
  }
  const resolved = path.resolve(candidate);
  if (!isPathInside(rootPath, resolved)) {
    throw new Error(`${label} must stay beneath the isolated test root.`);
  }
  return resolved;
}

function requireLoopbackOrigin(value, label) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch (_error) {
    throw new Error(`${label} must be a valid URL.`);
  }
  if (parsed.protocol !== 'http:' || parsed.username || parsed.password) {
    throw new Error(`${label} must be a credential-free HTTP URL.`);
  }
  if (parsed.hostname !== '127.0.0.1' || !parsed.port) {
    throw new Error(`${label} must use an explicitly allocated 127.0.0.1 port.`);
  }
  return parsed.origin;
}

function assertIsolatedTestEnvironment(environment = process.env) {
  const marker = String(environment.BREAKTWENTY_ISOLATED_TEST_MARKER || '');
  if (!marker.startsWith(ISOLATED_TEST_MARKER_PREFIX) || marker.length <= ISOLATED_TEST_MARKER_PREFIX.length) {
    throw new Error('The isolated desktop test marker is missing or invalid.');
  }

  const root = path.resolve(String(environment.BREAKTWENTY_ISOLATED_TEST_ROOT || ''));
  const rootStats = fs.lstatSync(root);
  if (!rootStats.isDirectory() || rootStats.isSymbolicLink()) {
    throw new Error('The isolated desktop test root must be a real directory.');
  }
  const configuredHostTempRoot = String(environment.BREAKTWENTY_ISOLATED_HOST_TEMP_ROOT || os.tmpdir());
  const temporaryRoot = fs.realpathSync(configuredHostTempRoot);
  const realRoot = fs.realpathSync(root);
  if (!isPathInside(temporaryRoot, realRoot) || !path.basename(realRoot).startsWith(ISOLATED_TEST_ROOT_PREFIX)) {
    throw new Error('The isolated desktop test root must be a newly named directory under the OS temporary root.');
  }

  const userData = requireIsolatedPath(
    realRoot,
    environment.BREAKTWENTY_DEVELOPMENT_USER_DATA_DIR,
    'Development user data',
  );
  const desktopAuth = requireIsolatedPath(
    realRoot,
    environment.BREAKTWENTY_DESKTOP_AUTH_DIR,
    'Desktop authentication state',
  );
  const home = requireIsolatedPath(realRoot, environment.HOME || environment.USERPROFILE, 'Test home');
  const appId = String(environment.BREAKTWENTY_ISOLATED_TEST_APP_ID || '').trim();
  if (!appId.startsWith('app.breaktwenty.test.') || PRODUCTION_APP_IDS.has(appId)) {
    throw new Error('The isolated desktop test must use a test-only application identity.');
  }

  for (const key of FORBIDDEN_SECRET_ENVIRONMENT_KEYS) {
    if (String(environment[key] || '').trim()) {
      throw new Error(`The isolated desktop test refuses inherited secret environment variable ${key}.`);
    }
  }

  const backendOrigin = requireLoopbackOrigin(
    environment.BREAKTWENTY_BACKEND_API_URL,
    'Backend API URL',
  );
  const frontendOrigin = requireLoopbackOrigin(
    environment.BREAKTWENTY_FRONTEND_URL,
    'Frontend URL',
  );
  if (backendOrigin === frontendOrigin) {
    throw new Error('The isolated backend and frontend must use separate allocated ports.');
  }
  if (String(environment.BREAKTWENTY_ISOLATED_NETWORK_POLICY || '') !== 'loopback-only') {
    throw new Error('The isolated desktop test must enforce the loopback-only network policy.');
  }

  return Object.freeze({
    marker,
    root: realRoot,
    userData,
    desktopAuth,
    home,
    appId,
    backendOrigin,
    frontendOrigin,
  });
}

function assertPackagedNativeSmokeEnvironment(environment = process.env) {
  const contract = assertIsolatedTestEnvironment(environment);
  const requiredPaths = {
    'Packaged data': environment.BREAKTWENTY_DATA_DIR,
    'Packaged database': environment.BREAKTWENTY_DB_PATH,
    'Packaged diagnostics': environment.BREAKTWENTY_DIAGNOSTIC_DIR,
    'Packaged logs': environment.BREAKTWENTY_LOG_DIR,
    'Packaged browser runtime': environment.BREAKTWENTY_BROWSER_RUNTIME_DIR,
  };
  for (const [label, candidate] of Object.entries(requiredPaths)) {
    requireIsolatedPath(contract.root, candidate, label);
  }
  if (String(environment.BREAKTWENTY_ISOLATED_JOURNEY_MODE || '') !== 'packaged-native') {
    throw new Error('The packaged native smoke mode is missing.');
  }
  if (String(environment.BREAKTWENTY_BACKEND_MODE || '') !== 'embedded') {
    throw new Error('The packaged native smoke requires the embedded backend.');
  }
  if (String(environment.BREAKTWENTY_DESKTOP_FRONTEND_MODE || '') !== 'build') {
    throw new Error('The packaged native smoke requires the production frontend build.');
  }
  if (String(environment.BREAKTWENTY_DISABLE_AUTO_UPDATE || '') !== '1') {
    throw new Error('The packaged native smoke must disable update networking.');
  }
  if (String(environment.BREAKTWENTY_DESKTOP_PREWARM_BROWSER_RUNTIME || '') !== '0') {
    throw new Error('The packaged native smoke must disable browser-runtime prewarming.');
  }
  return contract;
}

function configureIsolatedElectronPaths(app, environment, contract) {
  const mappings = {
    appData: environment.APPDATA,
    cache: environment.XDG_CACHE_HOME,
    home: contract.home,
    temp: environment.TMPDIR || environment.TEMP,
  };
  for (const [name, candidate] of Object.entries(mappings)) {
    const isolatedPath = requireIsolatedPath(contract.root, candidate, `Electron ${name}`);
    fs.mkdirSync(isolatedPath, { recursive: true, mode: 0o700 });
    app.setPath(name, isolatedPath);
  }
  return Object.freeze({ ...mappings });
}

module.exports = {
  FORBIDDEN_SECRET_ENVIRONMENT_KEYS,
  ISOLATED_TEST_MARKER_PREFIX,
  ISOLATED_TEST_ROOT_PREFIX,
  assertPackagedNativeSmokeEnvironment,
  assertIsolatedTestEnvironment,
  configureIsolatedElectronPaths,
  isPathInside,
  requireLoopbackOrigin,
};
