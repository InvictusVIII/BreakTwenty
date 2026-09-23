const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { PassThrough } = require('node:stream');
const test = require('node:test');
const {
  isolatedEnvironment,
  launchCommand,
  parseArgs,
  runPackagedExecutable,
} = require('./run_packaged_native_smoke');
const {
  ISOLATED_ELECTRON_JOURNEY_FLAG,
  ISOLATED_ELECTRON_RECEIPT_PREFIX,
} = require('../desktop/src/isolatedElectronJourneyProtocol');

test('packaged smoke arguments keep build opt-in and resolve the artifact inputs', () => {
  const parsed = parseArgs(['--dist', 'desktop/dist/electron', '--app-name', 'TestBuild']);
  assert.equal(parsed.build, false);
  assert.equal(parsed.appName, 'TestBuild');
  assert.ok(path.isAbsolute(parsed.distDir));
  assert.equal(parseArgs(['--build']).build, true);
  assert.throws(() => parseArgs(['--unknown']), /Unknown argument/);
});

test('packaged smoke environment isolates every mutable application path', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-isolated-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const environment = isolatedEnvironment({
    root,
    backendPort: 42001,
    frontendPort: 42002,
    marker: 'breaktwenty-isolated-test-v1:fixture',
    appId: 'app.breaktwenty.test.fixture',
  });
  for (const key of [
    'APPDATA',
    'BREAKTWENTY_BROWSER_RUNTIME_DIR',
    'BREAKTWENTY_DATA_DIR',
    'BREAKTWENTY_DB_PATH',
    'BREAKTWENTY_DESKTOP_AUTH_DIR',
    'BREAKTWENTY_DIAGNOSTIC_DIR',
    'BREAKTWENTY_LOG_DIR',
    'HOME',
    'LOCALAPPDATA',
    'TMPDIR',
    'XDG_CACHE_HOME',
    'XDG_CONFIG_HOME',
    'XDG_DATA_HOME',
  ]) {
    assert.ok(environment[key].startsWith(root), `${key} escaped the isolated root`);
  }
  for (const key of [
    'APPDATA',
    'BREAKTWENTY_DESKTOP_AUTH_DIR',
    'HOME',
    'LOCALAPPDATA',
    'TMPDIR',
    'XDG_CACHE_HOME',
    'XDG_CONFIG_HOME',
    'XDG_DATA_HOME',
  ]) {
    assert.ok(fs.statSync(environment[key]).isDirectory(), `${key} was not created`);
  }
  assert.equal(environment.BREAKTWENTY_DISABLE_AUTO_UPDATE, '1');
  assert.equal(environment.BREAKTWENTY_DESKTOP_PREWARM_BROWSER_RUNTIME, '0');
  assert.equal(environment.BREAKTWENTY_BACKEND_MODE, 'embedded');
  assert.equal(environment.BREAKTWENTY_DESKTOP_FRONTEND_MODE, 'build');
});

test('packaged smoke canonicalizes a symlinked temporary root', (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-smoke-parent-'));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const physicalParent = path.join(parent, 'physical');
  const linkedParent = path.join(parent, 'linked');
  fs.mkdirSync(physicalParent);
  fs.symlinkSync(physicalParent, linkedParent, process.platform === 'win32' ? 'junction' : 'dir');
  const root = fs.mkdtempSync(path.join(linkedParent, 'breaktwenty-isolated-'));
  const environment = isolatedEnvironment({
    root,
    backendPort: 42001,
    frontendPort: 42002,
    marker: 'breaktwenty-isolated-test-v1:canonical-fixture',
    appId: 'app.breaktwenty.test.canonical-fixture',
  });
  assert.equal(environment.BREAKTWENTY_ISOLATED_TEST_ROOT, fs.realpathSync(root));
  assert.ok(environment.HOME.startsWith(fs.realpathSync(root)));
});

test('Linux GitHub smoke disables the unavailable Chromium sandbox only when explicit', () => {
  const ordinary = launchCommand('/tmp/BreakTwenty', {
    platform: 'linux',
    hostEnvironment: {},
  });
  assert.deepEqual(ordinary, {
    command: 'xvfb-run',
    args: ['-a', '/tmp/BreakTwenty', ISOLATED_ELECTRON_JOURNEY_FLAG],
  });

  const hosted = launchCommand('/tmp/BreakTwenty', {
    platform: 'linux',
    hostEnvironment: { BREAKTWENTY_PACKAGED_SMOKE_NO_SANDBOX: '1' },
  });
  assert.deepEqual(hosted, {
    command: 'xvfb-run',
    args: ['-a', '/tmp/BreakTwenty', '--no-sandbox', ISOLATED_ELECTRON_JOURNEY_FLAG],
  });

  const windows = launchCommand('C:\\BreakTwenty.exe', {
    platform: 'win32',
    hostEnvironment: { BREAKTWENTY_PACKAGED_SMOKE_NO_SANDBOX: '1' },
  });
  assert.deepEqual(windows, {
    command: 'C:\\BreakTwenty.exe',
    args: [ISOLATED_ELECTRON_JOURNEY_FLAG],
  });
});

test('packaged smoke accepts a flushed receipt without waiting for inherited pipes to close', async () => {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.exitCode = null;
  child.signalCode = null;
  let terminated = false;
  const receipt = {
    marker: 'breaktwenty-isolated-test-v1:receipt-fixture',
    routes: [],
    assertions: { packagedExecutable: true },
  };

  const pending = runPackagedExecutable('/fixture/BreakTwenty', {}, {
    launch: { command: '/fixture/BreakTwenty', args: [ISOLATED_ELECTRON_JOURNEY_FLAG] },
    spawnProcess: () => child,
    terminateProcess: async () => {
      terminated = true;
      child.exitCode = 0;
      return true;
    },
    waitForExit: async () => false,
  });
  child.stdout.write(`${ISOLATED_ELECTRON_RECEIPT_PREFIX}${JSON.stringify(receipt)}\n`);

  assert.deepEqual(await pending, receipt);
  assert.equal(terminated, true);
});
