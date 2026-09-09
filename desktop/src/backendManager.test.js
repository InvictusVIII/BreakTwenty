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
    getVersion() {
      return '1.2.3-test.4';
    },
    getPath(name) {
      assert.equal(name, 'userData');
      return userDataDir;
    },
  };
}

test('embedded backend receives the authoritative desktop release identity', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-release-identity-'));
  const priorPython = process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON;
  try {
    fs.mkdirSync(path.join(root, 'backend'), { recursive: true });
    fs.mkdirSync(path.join(root, 'config'), { recursive: true });
    fs.writeFileSync(path.join(root, 'backend', 'alembic.ini'), '[alembic]\n', 'utf8');
    fs.writeFileSync(path.join(root, 'config', 'provider_catalog.json'), '{}\n', 'utf8');
    process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON = process.execPath;
    const manager = new BackendManager({
      app: packagedApp(path.join(root, 'user-data')),
      appRoot: root,
      backendMode: 'embedded',
      backendApiUrl: 'http://127.0.0.1:8765/api',
      frontendOrigin: 'http://127.0.0.1:32100',
    });

    const runtime = manager.resolveRuntime();

    assert.equal(runtime.env.BREAKTWENTY_DESKTOP_APP_VERSION, '1.2.3-test.4');
    assert.equal(runtime.env.BREAKTWENTY_DESKTOP_PLATFORM, process.platform);
  } finally {
    if (priorPython === undefined) delete process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON;
    else process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON = priorPython;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('packaged embedded backend can request an operating-system-assigned port', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-dynamic-backend-'));
  const priorPython = process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON;
  try {
    fs.mkdirSync(path.join(root, 'backend'), { recursive: true });
    fs.mkdirSync(path.join(root, 'config'), { recursive: true });
    fs.writeFileSync(path.join(root, 'backend', 'alembic.ini'), '[alembic]\n', 'utf8');
    fs.writeFileSync(path.join(root, 'config', 'provider_catalog.json'), '{}\n', 'utf8');
    process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON = process.execPath;
    const manager = new BackendManager({
      app: packagedApp(path.join(root, 'user-data')),
      appRoot: root,
      backendMode: 'embedded',
      backendApiUrl: 'http://127.0.0.1:1/api',
      frontendOrigin: 'http://127.0.0.1:32100',
      dynamicBackendPort: true,
    });

    const runtime = manager.resolveRuntime();

    assert.equal(runtime.host, '127.0.0.1');
    assert.equal(runtime.port, 0);
  } finally {
    if (priorPython === undefined) delete process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON;
    else process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON = priorPython;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('running backend accepts an unchanged frontend origin idempotently', () => {
  const manager = Object.create(BackendManager.prototype);
  manager.frontendOrigin = 'http://127.0.0.1:49152';
  manager.process = { exitCode: null };

  assert.doesNotThrow(() => manager.setFrontendOrigin('http://127.0.0.1:49152/'));
  assert.equal(manager.frontendOrigin, 'http://127.0.0.1:49152');
  assert.throws(
    () => manager.setFrontendOrigin('http://127.0.0.1:49153'),
    /Frontend origin cannot change while the embedded backend is running/,
  );
});

test('a confirmed prior backend stop enables one-shot recovery until startup is healthy', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-recovery-marker-'));
  const priorPython = process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON;
  try {
    fs.mkdirSync(path.join(root, 'backend'), { recursive: true });
    fs.mkdirSync(path.join(root, 'config'), { recursive: true });
    fs.writeFileSync(path.join(root, 'backend', 'alembic.ini'), '[alembic]\n', 'utf8');
    fs.writeFileSync(path.join(root, 'config', 'provider_catalog.json'), '{}\n', 'utf8');
    process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON = process.execPath;
    const manager = new BackendManager({
      app: packagedApp(path.join(root, 'user-data')),
      appRoot: root,
      backendMode: 'embedded',
      backendApiUrl: 'http://127.0.0.1:8765/api',
      frontendOrigin: 'http://127.0.0.1:32100',
    });
    manager.process = { exitCode: 0 };

    assert.equal(await manager.shutdown(), true);
    assert.equal(manager.hasRecoveryRestartMarker(), true);
    assert.equal(manager.shouldUseRecoveryRestart(false), true);
    const relaunchedManager = new BackendManager({
      app: packagedApp(path.join(root, 'user-data')),
      appRoot: root,
      backendMode: 'embedded',
      backendApiUrl: 'http://127.0.0.1:8765/api',
      frontendOrigin: 'http://127.0.0.1:32100',
    });
    let startRequestedRecovery = false;
    relaunchedManager.resolveRuntime = ({ recoveryRestart }) => {
      startRequestedRecovery = recoveryRestart;
      throw new Error('stop after recovery option probe');
    };
    await assert.rejects(
      relaunchedManager.startOnce(),
      /stop after recovery option probe/,
    );
    assert.equal(startRequestedRecovery, true);
    assert.equal(relaunchedManager.hasRecoveryRestartMarker(), true);
    assert.equal(
      manager.resolveRuntime({ recoveryRestart: manager.shouldUseRecoveryRestart(false) })
        .env.BREAKTWENTY_RECOVERY_RESTART,
      '1',
    );

    assert.equal(relaunchedManager.acknowledgeHealthyStartup(), true);
    assert.equal(relaunchedManager.hasRecoveryRestartMarker(), false);
    assert.equal(relaunchedManager.shouldUseRecoveryRestart(false), false);
  } finally {
    if (priorPython === undefined) delete process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON;
    else process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON = priorPython;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('dynamic embedded backend recovery falls back to a fresh port when reclaim fails', async () => {
  const manager = Object.create(BackendManager.prototype);
  manager.acceptingStarts = true;
  manager.shuttingDown = false;
  manager.mode = 'embedded';
  manager.dynamicBackendPort = true;
  manager.backendPort = 49152;
  manager.process = null;
  manager.startPromise = null;
  manager.restartPromise = null;
  manager.status = { logPath: '' };
  const requestedPorts = [];
  manager.start = async () => {
    requestedPorts.push(manager.backendPort);
    if (requestedPorts.length === 1) {
      const error = new Error('address unavailable');
      error.code = 'BACKEND_BIND_FAILED';
      throw error;
    }
    return { requestedPort: manager.backendPort };
  };

  const result = await manager.restart();

  assert.equal(result.requestedPort, 0);
  assert.deepEqual(requestedPorts, [49152, 0]);
});

test('dynamic embedded backend recovery does not retry a non-bind startup failure', async () => {
  const manager = Object.create(BackendManager.prototype);
  manager.acceptingStarts = true;
  manager.shuttingDown = false;
  manager.mode = 'embedded';
  manager.dynamicBackendPort = true;
  manager.backendPort = 49152;
  manager.process = null;
  manager.startPromise = null;
  manager.restartPromise = null;
  manager.status = { logPath: '' };
  let attempts = 0;
  manager.start = async () => {
    attempts += 1;
    throw new Error('migration failed');
  };

  await assert.rejects(manager.restart(), /migration failed/);
  assert.equal(attempts, 1);
  assert.equal(manager.backendPort, 49152);
});

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
