const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { BackendManager } = require('./backendManager');
const { rotateLaunchTokenFile } = require('./launchAuth');

function packagedApp(userDataDir) {
  return {
    isPackaged: true,
    getPath(name) {
      assert.equal(name, 'userData');
      return userDataDir;
    },
  };
}

function createManager(root) {
  return new BackendManager({
    app: packagedApp(path.join(root, 'user-data')),
    appRoot: root,
    backendMode: 'docker',
    backendApiUrl: 'http://127.0.0.1:8000/api',
    frontendOrigin: 'http://127.0.0.1:32100',
  });
}

test('external-Docker package fails closed without an explicit shared auth directory', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-external-docker-'));
  const prior = process.env.BREAKTWENTY_DESKTOP_AUTH_DIR;
  try {
    delete process.env.BREAKTWENTY_DESKTOP_AUTH_DIR;
    assert.throws(
      () => createManager(root),
      /require BREAKTWENTY_DESKTOP_AUTH_DIR.*mounted into Docker Compose/,
    );
  } finally {
    if (prior === undefined) delete process.env.BREAKTWENTY_DESKTOP_AUTH_DIR;
    else process.env.BREAKTWENTY_DESKTOP_AUTH_DIR = prior;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('external-Docker package accepts a valid bundle in the explicit shared directory', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-external-docker-'));
  const authDirectory = path.join(root, 'shared-auth');
  const prior = process.env.BREAKTWENTY_DESKTOP_AUTH_DIR;
  try {
    rotateLaunchTokenFile(authDirectory);
    process.env.BREAKTWENTY_DESKTOP_AUTH_DIR = authDirectory;
    const manager = createManager(root);
    assert.equal(manager.describe().mode, 'docker');
    assert.equal(manager.getRuntimeEnv().BREAKTWENTY_DESKTOP_AUTH_DIR, authDirectory);
    assert.notEqual(
      manager.getDesktopLaunchAuthToken(),
      manager.getRendererLaunchAuthToken(),
    );
  } finally {
    if (prior === undefined) delete process.env.BREAKTWENTY_DESKTOP_AUTH_DIR;
    else process.env.BREAKTWENTY_DESKTOP_AUTH_DIR = prior;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('backend manager rejects a remote frontend origin before CORS or tokens are prepared', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-frontend-origin-'));
  try {
    assert.throws(
      () => new BackendManager({
        app: { ...packagedApp(path.join(root, 'user-data')), isPackaged: false },
        appRoot: root,
        backendMode: 'embedded',
        backendApiUrl: 'http://127.0.0.1:8765/api',
        frontendOrigin: 'http://attacker.test:3000',
      }),
      /frontend URL must use a private local origin/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('backend recovery requests several timestamped stack samples without changing process state', async () => {
  const manager = Object.create(BackendManager.prototype);
  manager.mode = 'embedded';
  manager.process = { pid: 4321, exitCode: null };
  manager.status = { logPath: '' };
  const signals = [];
  const waits = [];

  const result = await manager.requestStackDumps({
    sampleCount: 3,
    intervalMs: 25,
    killProcess: (pid, signal) => signals.push([pid, signal]),
    waitForInterval: async (milliseconds) => waits.push(milliseconds),
  });

  assert.equal(result.requestedCount, 3);
  assert.deepEqual(signals, [
    [4321, 'SIGUSR2'],
    [4321, 'SIGUSR2'],
    [4321, 'SIGUSR2'],
  ]);
  assert.deepEqual(waits, [25, 25, 25]);
  assert.equal(result.samples.every((sample) => sample.requestedAt), true);
});
