const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { APP_BRAND_NAME } = require('./brand');
const {
  APP_DATABASE_FILE_NAME,
  resolveWindowsLocalAppRoot,
} = require('./appIdentity');
const { terminateChildProcess } = require('./appLifecycle');
const {
  normalizeLocalBackendApiUrl,
  normalizeLocalFrontendUrl,
} = require('./backendApiUrl');
const { BreakTwentyKeyManager } = require('./keyManager');
const { generateLaunchTokenBundle, readLaunchTokenFile } = require('./launchAuth');
const { runMigrationsSafely } = require('./migrationSafety');

const DEFAULT_EMBEDDED_BACKEND_PORT = 8765;
const EMBEDDED_BACKEND_LISTENING_PREFIX = 'BREAKTWENTY_EMBEDDED_BACKEND_LISTENING ';
const EMBEDDED_BACKEND_BIND_ERROR_PREFIX = 'BREAKTWENTY_EMBEDDED_BACKEND_BIND_ERROR ';
const EMBEDDED_BACKEND_LISTEN_TIMEOUT_MS = 10000;
const KEY_BOOTSTRAP_ENV = 'BREAKTWENTY_KEY_BOOTSTRAP';
const KEY_BOOTSTRAP_STDIN_V2 = 'stdin-v2';
const DATABASE_KEY_ENV = 'BREAKTWENTY_DATABASE_ENCRYPTION_KEY';
const APP_ENCRYPTION_KEY_ENV = 'BREAKTWENTY_APP_ENCRYPTION_KEY';
const RECOVERY_RESTART_ENV = 'BREAKTWENTY_RECOVERY_RESTART';
const RECOVERY_RESTART_MARKER_FILE = 'embedded-backend-recovery-pending.json';
const PROCESS_LOG_MAX_BYTES = 2 * 1024 * 1024;
const PROCESS_LOG_BACKUP_COUNT = 2;

function normalizeBackendMode(value) {
  return String(value || 'docker').toLowerCase() === 'embedded' ? 'embedded' : 'docker';
}

function pathExists(filePath) {
  try {
    return fs.existsSync(filePath);
  } catch (_error) {
    return false;
  }
}

function nonEmptyFileExists(filePath) {
  try {
    const stats = fs.statSync(filePath);
    return stats.isFile() && stats.size > 0;
  } catch (_error) {
    return false;
  }
}

function pythonCandidatesForRuntime(pythonRoot) {
  const candidates = [
    path.join(pythonRoot, 'bin', 'python3'),
    path.join(pythonRoot, 'bin', 'python'),
    path.join(pythonRoot, 'python.exe'),
  ];
  try {
    const binDir = path.join(pythonRoot, 'bin');
    candidates.push(
      ...fs.readdirSync(binDir)
        .filter((name) => /^python3(?:\.\d+)?$/.test(name))
        .sort((left, right) => right.localeCompare(left))
        .map((name) => path.join(binDir, name)),
    );
  } catch (_error) {
    // The fixed candidates cover Windows and venv layouts.
  }
  return candidates;
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function sqliteUrl(filePath) {
  return `sqlite+sqlcipher_aiosqlite:///${String(filePath).split(path.sep).join('/')}`;
}

function numericPort(value, fallback) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function envFlag(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value || '').trim().toLowerCase());
}

function appendLine(filePath, line) {
  try {
    ensureDir(path.dirname(filePath));
    try {
      const stats = fs.statSync(filePath);
      if (stats.isFile() && stats.size >= PROCESS_LOG_MAX_BYTES) {
        for (let index = PROCESS_LOG_BACKUP_COUNT; index >= 1; index -= 1) {
          const source = index === 1 ? filePath : `${filePath}.${index - 1}`;
          const destination = `${filePath}.${index}`;
          if (!fs.existsSync(source)) continue;
          fs.rmSync(destination, { force: true });
          fs.renameSync(source, destination);
        }
      }
    } catch (_error) {
    }
    fs.appendFileSync(filePath, `${new Date().toISOString()} ${line}\n`, 'utf8');
  } catch (_error) {
    // Startup logging is best-effort; process state is still returned to the UI.
  }
}

function wait(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

class BackendManager {
  constructor({
    app,
    appRoot,
    backendMode,
    backendApiUrl,
    frontendOrigin,
    dynamicBackendPort = false,
  }) {
    this.app = app;
    this.appRoot = appRoot;
    this.backendApiUrl = normalizeLocalBackendApiUrl(backendApiUrl);
    this.frontendOrigin = normalizeLocalFrontendUrl(frontendOrigin);
    this.mode = normalizeBackendMode(
      backendMode || process.env.BREAKTWENTY_BACKEND_MODE || (this.app.isPackaged ? 'embedded' : 'docker'),
    );
    this.dynamicBackendPort = this.mode === 'embedded' && dynamicBackendPort === true;
    this.backendHost = new URL(this.backendApiUrl).hostname.replace(/^\[|\]$/g, '');
    this.backendPort = this.dynamicBackendPort
      ? 0
      : numericPort(new URL(this.backendApiUrl).port, DEFAULT_EMBEDDED_BACKEND_PORT);
    this.process = null;
    this.startPromise = null;
    this.restartPromise = null;
    this.acceptingStarts = true;
    this.shuttingDown = false;
    this.recovering = false;
    this.status = {
      mode: this.mode,
      state: this.mode === 'embedded' ? 'idle' : 'external',
      message: '',
      pid: null,
      logPath: '',
      runtimePaths: null,
    };
    this.desktopRuntime = this.resolveDesktopRuntime();
    this.runtimeEnv = this.desktopRuntime.env;
    this.status.runtimePaths = this.desktopRuntime.runtimePaths;
    this.keyManager = null;
    this.embeddedLaunchTokens = this.mode === 'embedded' ? generateLaunchTokenBundle() : null;
  }

  describe() {
    return {
      ...this.status,
      backendApiUrl: this.backendApiUrl,
      running: Boolean(this.process && this.process.exitCode === null),
    };
  }

  getRuntimeEnv() {
    return { ...this.runtimeEnv };
  }

  getBackendApiUrl() {
    return this.backendApiUrl;
  }

  setFrontendOrigin(frontendOrigin) {
    const normalizedFrontendOrigin = normalizeLocalFrontendUrl(frontendOrigin);
    if (
      this.process
      && this.process.exitCode === null
      && normalizedFrontendOrigin !== this.frontendOrigin
    ) {
      throw new Error('Frontend origin cannot change while the embedded backend is running.');
    }
    this.frontendOrigin = normalizedFrontendOrigin;
  }

  getLaunchAuthTokens() {
    return this.mode === 'embedded'
      ? this.embeddedLaunchTokens
      : readLaunchTokenFile(this.desktopRuntime.desktopAuthDir);
  }

  getDesktopLaunchAuthToken() {
    return this.getLaunchAuthTokens().desktopToken;
  }

  getRendererLaunchAuthToken() {
    return this.getLaunchAuthTokens().rendererToken;
  }

  prepareRuntimeEnv() {
    if (this.mode !== 'embedded') {
      this.desktopRuntime = this.resolveDesktopRuntime();
      this.runtimeEnv = this.desktopRuntime.env;
      this.status.runtimePaths = this.desktopRuntime.runtimePaths;
      return this.getRuntimeEnv();
    }

    const runtime = this.resolveRuntime();
    this.runtimeEnv = runtime.env;
    this.status.logPath = runtime.processLogPath;
    this.status.runtimePaths = runtime.runtimePaths;
    return this.getRuntimeEnv();
  }

  getRecoveryRestartMarkerPath() {
    return path.join(this.desktopRuntime.dataDir, RECOVERY_RESTART_MARKER_FILE);
  }

  hasRecoveryRestartMarker() {
    return this.mode === 'embedded' && pathExists(this.getRecoveryRestartMarkerPath());
  }

  shouldUseRecoveryRestart(explicitRecoveryRestart = false) {
    return explicitRecoveryRestart === true || this.hasRecoveryRestartMarker();
  }

  markRecoveryRestartPending(reason = 'owned_backend_stopped') {
    if (this.mode !== 'embedded') return false;
    const markerPath = this.getRecoveryRestartMarkerPath();
    try {
      ensureDir(path.dirname(markerPath));
      fs.writeFileSync(markerPath, `${JSON.stringify({
        version: 1,
        reason,
        recordedAt: new Date().toISOString(),
      })}\n`, {
        encoding: 'utf8',
        mode: 0o600,
      });
      appendLine(this.status.logPath, 'recorded pending embedded backend recovery');
      return true;
    } catch (error) {
      appendLine(this.status.logPath, `could not record pending embedded backend recovery: ${error.message}`);
      return false;
    }
  }

  acknowledgeHealthyStartup() {
    if (this.mode !== 'embedded') return false;
    const markerPath = this.getRecoveryRestartMarkerPath();
    if (!pathExists(markerPath)) return false;
    try {
      fs.rmSync(markerPath, { force: true });
      appendLine(this.status.logPath, 'cleared pending embedded backend recovery after health check');
      return true;
    } catch (error) {
      appendLine(this.status.logPath, `could not clear pending embedded backend recovery: ${error.message}`);
      return false;
    }
  }

  async start(options = {}) {
    if (this.startPromise) {
      return this.startPromise;
    }
    this.startPromise = this.startOnce(options).finally(() => {
      this.startPromise = null;
    });
    return this.startPromise;
  }

  async startOnce({ recoveryRestart = false } = {}) {
    if (!this.acceptingStarts) {
      throw new Error(`${APP_BRAND_NAME} backend cannot start while the desktop app is shutting down.`);
    }
    if (this.mode !== 'embedded') {
      this.desktopRuntime = this.resolveDesktopRuntime();
      this.runtimeEnv = this.desktopRuntime.env;
      this.status = {
        mode: this.mode,
        state: 'external',
        message: `${APP_BRAND_NAME} backend is expected from the Docker backend service.`,
        pid: null,
        logPath: '',
        runtimePaths: this.desktopRuntime.runtimePaths,
      };
      return this.describe();
    }
    if (this.process && this.process.exitCode === null) {
      return this.describe();
    }

    const pendingRecoveryRestart = this.hasRecoveryRestartMarker();
    const effectiveRecoveryRestart = this.shouldUseRecoveryRestart(recoveryRestart);
    const runtime = this.resolveRuntime({ recoveryRestart: effectiveRecoveryRestart });
    this.runtimeEnv = runtime.env;
    this.status = {
      mode: this.mode,
      state: 'starting',
      message: `Preparing embedded ${APP_BRAND_NAME} backend.`,
      pid: null,
      logPath: runtime.processLogPath,
      runtimePaths: runtime.runtimePaths,
    };
    appendLine(runtime.processLogPath, 'embedded backend startup requested');
    if (pendingRecoveryRestart) {
      appendLine(runtime.processLogPath, 'pending recovery marker enabled startup lease recovery');
    }

    try {
      const databaseExisted = nonEmptyFileExists(runtime.runtimePaths.dbPath);
      this.keyManager = new BreakTwentyKeyManager({
        dataDir: runtime.runtimePaths.dataDir,
        dbPath: runtime.runtimePaths.dbPath,
      });
      await this.keyManager.loadOrCreate();
      if (!this.acceptingStarts) {
        throw new Error(`${APP_BRAND_NAME} backend startup was cancelled during shutdown.`);
      }
      this.runMigrations(runtime, { databaseExisted });
      if (!this.acceptingStarts) {
        throw new Error(`${APP_BRAND_NAME} backend startup was cancelled during shutdown.`);
      }
      await this.spawnBackend(runtime);
      this.status.state = 'running';
      this.status.message = `Embedded ${APP_BRAND_NAME} backend is starting.`;
      this.status.pid = this.process.pid;
      return this.describe();
    } catch (error) {
      const child = this.process;
      if (child && child.exitCode === null) {
        await terminateChildProcess(child, { graceMs: 1000, forceWaitMs: 1000 });
      }
      if (this.process === child) this.process = null;
      if (this.keyManager) {
        this.keyManager.clear();
      }
      this.status.state = 'failed';
      this.status.message = error.message;
      appendLine(runtime.processLogPath, `startup failed: ${error.message}`);
      throw error;
    }
  }

  quiesce() {
    this.acceptingStarts = false;
  }

  async shutdown() {
    this.quiesce();
    this.shuttingDown = true;
    const child = this.process;
    if (!child) {
      return true;
    }
    if (child.exitCode !== null) {
      this.markRecoveryRestartPending('owned_backend_already_stopped');
      return true;
    }
    appendLine(this.status.logPath, 'embedded backend stop requested');
    const stopped = await terminateChildProcess(child);
    if (!stopped) {
      appendLine(this.status.logPath, 'embedded backend did not stop after forced termination');
      this.status.state = 'failed';
      this.status.message = 'Embedded backend did not stop cleanly.';
    } else {
      this.markRecoveryRestartPending('owned_backend_shutdown');
    }
    return stopped;
  }

  async restart() {
    if (this.restartPromise) return this.restartPromise;
    this.restartPromise = this.restartOnce().finally(() => {
      this.restartPromise = null;
    });
    return this.restartPromise;
  }

  async restartOnce() {
    if (!this.acceptingStarts || this.shuttingDown) {
      throw new Error(`${APP_BRAND_NAME} backend cannot restart while the desktop app is shutting down.`);
    }
    if (this.mode !== 'embedded') {
      throw new Error('Only the owned embedded backend can be restarted automatically.');
    }
    if (this.startPromise) await this.startPromise;
    const child = this.process;
    this.recovering = true;
    this.status.state = 'recovering';
    this.status.message = `Recovering embedded ${APP_BRAND_NAME} backend.`;
    appendLine(this.status.logPath, 'embedded backend controlled recovery requested');
    try {
      if (child && child.exitCode === null) {
        const stopped = await terminateChildProcess(child, {
          graceMs: 3000,
          forceWaitMs: 3000,
        });
        if (!stopped) {
          throw new Error('The unresponsive embedded backend could not be terminated safely.');
        }
      }
      if (child) {
        this.markRecoveryRestartPending('owned_backend_controlled_restart');
      }
      if (this.process === child) this.process = null;
      try {
        return await this.start({ recoveryRestart: true });
      } catch (error) {
        if (
          !this.dynamicBackendPort
          || this.backendPort === 0
          || error.code !== 'BACKEND_BIND_FAILED'
        ) throw error;
        appendLine(
          this.status.logPath,
          'embedded backend could not reclaim its prior port; retrying with a new OS-assigned port',
        );
        this.backendPort = 0;
        return await this.start({ recoveryRestart: true });
      }
    } finally {
      this.recovering = false;
    }
  }

  requestStackDump({ sampleIndex = 1, sampleCount = 1, killProcess = process.kill } = {}) {
    const child = this.process;
    if (this.mode !== 'embedded' || !child || child.exitCode !== null) {
      return { requested: false, reason: 'backend_not_running' };
    }
    if (process.platform === 'win32') {
      return { requested: false, reason: 'unsupported_on_windows' };
    }
    try {
      killProcess(child.pid, 'SIGUSR2');
      appendLine(
        this.status.logPath,
        `requested embedded backend all-thread stack dump sample=${sampleIndex}/${sampleCount}`,
      );
      return {
        requested: true,
        signal: 'SIGUSR2',
        sampleIndex,
        sampleCount,
        requestedAt: new Date().toISOString(),
      };
    } catch (error) {
      appendLine(this.status.logPath, `embedded backend stack dump request failed: ${error.message}`);
      return { requested: false, reason: 'signal_failed' };
    }
  }

  async requestStackDumps({
    sampleCount = 3,
    intervalMs = 250,
    killProcess = process.kill,
    waitForInterval = wait,
  } = {}) {
    const count = Math.max(1, Math.min(Number(sampleCount) || 1, 8));
    const samples = [];
    for (let index = 0; index < count; index += 1) {
      const sample = this.requestStackDump({
        sampleIndex: index + 1,
        sampleCount: count,
        killProcess,
      });
      samples.push(sample);
      if (!sample.requested) break;
      await waitForInterval(Math.max(0, Number(intervalMs) || 0));
    }
    return {
      samples,
      requestedCount: samples.filter((sample) => sample.requested).length,
      intervalMs: Math.max(0, Number(intervalMs) || 0),
    };
  }

  async revokeLaunchAuthentication() {
    try {
      const response = await fetch(`${this.backendApiUrl}/auth/launch/revoke`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${this.getDesktopLaunchAuthToken()}`,
        },
        signal: AbortSignal.timeout(5000),
      });
      return response.ok || response.status === 401 || response.status === 409;
    } catch (_error) {
      return false;
    }
  }

  stop() {
    return this.shutdown();
  }

  resolveDesktopRuntime() {
    const userDataDir = this.app.getPath('userData');
    const localAppRoot = resolveWindowsLocalAppRoot({ fallbackDir: userDataDir });
    const resourceRoot = path.resolve(process.env.BREAKTWENTY_RESOURCE_ROOT || this.appRoot);
    const configRoot = path.resolve(
      process.env.BREAKTWENTY_CONFIG_DIR || path.join(resourceRoot, 'config'),
    );
    const packagedRuntime = this.app.isPackaged || envFlag(process.env.BREAKTWENTY_DESKTOP_USE_USER_DATA);
    const dataDir = path.resolve(
      process.env.BREAKTWENTY_DATA_DIR ||
        (packagedRuntime ? path.join(userDataDir, 'data') : path.join(this.appRoot, 'data')),
    );
    const configuredDesktopAuthDir = String(
      process.env.BREAKTWENTY_DESKTOP_AUTH_DIR || '',
    ).trim();
    if (this.mode === 'docker' && this.app.isPackaged && !configuredDesktopAuthDir) {
      throw new Error(
        `${APP_BRAND_NAME} external-Docker packages require BREAKTWENTY_DESKTOP_AUTH_DIR to name the same private launch-auth directory mounted into Docker Compose.`,
      );
    }
    const desktopAuthDir = path.resolve(
      configuredDesktopAuthDir ||
        (packagedRuntime ? path.join(userDataDir, 'desktop-auth') : path.join(this.appRoot, '.desktop-auth')),
    );
    const diagnosticDir = path.resolve(
      process.env.BREAKTWENTY_DIAGNOSTIC_DIR ||
        (packagedRuntime
          ? path.join(dataDir, 'logs', 'providers')
          : path.join(this.appRoot, 'data', 'logs', 'providers')),
    );
    const browserRuntimeDir = path.resolve(
      process.env.BREAKTWENTY_BROWSER_RUNTIME_DIR ||
        (packagedRuntime && process.platform === 'win32'
          ? path.join(localAppRoot, 'browser-runtimes')
          : path.join(desktopAuthDir, 'browser-runtimes')),
    );
    const logDir = path.resolve(
      process.env.BREAKTWENTY_LOG_DIR ||
        (packagedRuntime ? path.join(dataDir, 'logs') : path.join(this.appRoot, 'data', 'logs')),
    );
    const dbPath = path.resolve(
      process.env.BREAKTWENTY_DB_PATH || path.join(dataDir, APP_DATABASE_FILE_NAME),
    );
    const providerCatalogPath = path.resolve(
      process.env.PROVIDER_CATALOG_PATH || path.join(configRoot, 'provider_catalog.json'),
    );
    const visibleAuthRequirementsPath = path.resolve(
      process.env.BREAKTWENTY_VISIBLE_AUTH_REQUIREMENTS_PATH ||
        path.join(resourceRoot, 'scripts', 'desktop_visible_auth_requirements.lock'),
    );

    for (const dirPath of [desktopAuthDir, diagnosticDir, browserRuntimeDir, logDir]) {
      ensureDir(dirPath);
    }
    if (this.mode === 'docker' && this.app.isPackaged) {
      try {
        readLaunchTokenFile(desktopAuthDir);
      } catch (_error) {
        throw new Error(
          `${APP_BRAND_NAME} external-Docker package launch authentication is unavailable. BREAKTWENTY_DESKTOP_AUTH_DIR must contain a valid private token bundle generated for this launch and mounted into Docker Compose.`,
        );
      }
    }
    if (this.mode === 'embedded' || packagedRuntime || process.env.BREAKTWENTY_DATA_DIR || process.env.BREAKTWENTY_DB_PATH) {
      ensureDir(dataDir);
    }

    const env = {
      BREAKTWENTY_RESOURCE_ROOT: resourceRoot,
      BREAKTWENTY_DESKTOP_AUTH_DIR: desktopAuthDir,
      BREAKTWENTY_DIAGNOSTIC_DIR: diagnosticDir,
      BREAKTWENTY_BROWSER_RUNTIME_DIR: browserRuntimeDir,
      BREAKTWENTY_LOG_DIR: logDir,
      PROVIDER_CATALOG_PATH: providerCatalogPath,
      BREAKTWENTY_VISIBLE_AUTH_REQUIREMENTS_PATH: visibleAuthRequirementsPath,
    };

    if (this.mode === 'embedded' || packagedRuntime || process.env.BREAKTWENTY_DATA_DIR || process.env.BREAKTWENTY_DB_PATH) {
      env.BREAKTWENTY_DATA_DIR = dataDir;
      env.BREAKTWENTY_DB_PATH = dbPath;
      env.DATABASE_URL = sqliteUrl(dbPath);
    }

    return {
      resourceRoot,
      configRoot,
      dataDir,
      dbPath,
      desktopAuthDir,
      diagnosticDir,
      browserRuntimeDir,
      logDir,
      providerCatalogPath,
      visibleAuthRequirementsPath,
      env,
      runtimePaths: {
        dataDir,
        dbPath,
        desktopAuthDir,
        diagnosticDir,
        browserRuntimeDir,
        logDir,
      },
    };
  }

  resolveRuntime({ recoveryRestart = false } = {}) {
    const desktopRuntime = this.resolveDesktopRuntime();
    const resourceRoot = desktopRuntime.resourceRoot;
    const backendRoot = path.resolve(
      process.env.BREAKTWENTY_BACKEND_SOURCE_DIR || path.join(resourceRoot, 'backend'),
    );
    const {
      dataDir,
      desktopAuthDir,
      diagnosticDir,
      browserRuntimeDir,
      logDir,
      dbPath,
      providerCatalogPath,
    } = desktopRuntime;
    const pythonPath = this.resolvePython(resourceRoot);

    if (!pathExists(path.join(backendRoot, 'alembic.ini'))) {
      throw new Error(`Embedded backend files are missing at ${backendRoot}.`);
    }
    if (!pathExists(providerCatalogPath)) {
      throw new Error(`Provider catalog is missing at ${providerCatalogPath}.`);
    }

    const corsOrigins = this.frontendOrigin;
    const env = {
      ...process.env,
      ...desktopRuntime.env,
      BREAKTWENTY_BACKEND_MODE: 'embedded',
      BREAKTWENTY_DATA_DIR: dataDir,
      BREAKTWENTY_DESKTOP_AUTH_DIR: desktopAuthDir,
      BREAKTWENTY_DIAGNOSTIC_DIR: diagnosticDir,
      BREAKTWENTY_BROWSER_RUNTIME_DIR: browserRuntimeDir,
      BREAKTWENTY_LOG_DIR: logDir,
      BREAKTWENTY_DB_PATH: dbPath,
      BREAKTWENTY_DESKTOP_APP_VERSION: String(this.app.getVersion()),
      BREAKTWENTY_DESKTOP_PLATFORM: process.platform,
      BREAKTWENTY_VISIBLE_AUTH_PYTHON: pythonPath,
      BREAKTWENTY_EMBEDDED_BACKEND_PYTHON: pythonPath,
      DATABASE_URL: sqliteUrl(dbPath),
      PROVIDER_CATALOG_PATH: providerCatalogPath,
      BREAKTWENTY_CORS_ALLOW_ORIGINS: corsOrigins,
      PYTHONPATH: backendRoot,
      PYTHONUNBUFFERED: '1',
    };
    delete env[DATABASE_KEY_ENV];
    delete env[APP_ENCRYPTION_KEY_ENV];
    delete env[RECOVERY_RESTART_ENV];
    env[KEY_BOOTSTRAP_ENV] = KEY_BOOTSTRAP_STDIN_V2;
    if (recoveryRestart) env[RECOVERY_RESTART_ENV] = '1';

    return {
      pythonPath,
      backendRoot,
      env,
      host: this.resolveBackendHost(),
      port: this.resolveBackendPort(),
      processLogPath: path.join(logDir, 'embedded-backend-process.log'),
      runtimePaths: {
        dataDir,
        dbPath,
        desktopAuthDir,
        diagnosticDir,
        browserRuntimeDir,
        logDir,
      },
    };
  }

  resolvePython(resourceRoot) {
    const packagedPythonRoot = path.join(resourceRoot, 'python');
    const candidates = [
      process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON,
      ...pythonCandidatesForRuntime(packagedPythonRoot),
      path.join(this.appRoot, '.venv-backend', 'bin', 'python'),
      !this.app.isPackaged ? 'python3' : '',
    ].filter(Boolean);
    for (const candidate of candidates) {
      if (!candidate.includes(path.sep) || pathExists(candidate)) {
        return candidate;
      }
    }
    throw new Error('Embedded backend Python runtime is missing.');
  }

  resolveBackendHost() {
    return this.backendHost;
  }

  resolveBackendPort() {
    return this.backendPort;
  }

  runMigrations(runtime, { databaseExisted }) {
    return runMigrationsSafely({
      databasePath: runtime.runtimePaths.dbPath,
      databaseExisted,
      inspect: () => this.runMigrationSafetyHelper(runtime, [
        'inspect',
        '--database', runtime.runtimePaths.dbPath,
        '--alembic-ini', path.join(runtime.backendRoot, 'alembic.ini'),
      ]),
      createSnapshot: (snapshotPath) => this.runMigrationSafetyHelper(runtime, [
        'snapshot',
        '--database', runtime.runtimePaths.dbPath,
        '--snapshot', snapshotPath,
      ]),
      migrate: () => this.runAlembicUpgrade(runtime),
      restoreSnapshot: (snapshotPath, expectedSha256) => this.runMigrationSafetyHelper(runtime, [
        'restore',
        '--database', runtime.runtimePaths.dbPath,
        '--snapshot', snapshotPath,
        '--expected-sha256', expectedSha256,
      ]),
      log: (message) => appendLine(runtime.processLogPath, message),
    });
  }

  runAlembicUpgrade(runtime) {
    appendLine(runtime.processLogPath, 'running alembic upgrade head');
    const status = this.runKeyedSyncProcess(
      runtime,
      runtime.pythonPath,
      ['-m', 'alembic', '-c', path.join(runtime.backendRoot, 'alembic.ini'), 'upgrade', 'head'],
      Number(process.env.BREAKTWENTY_EMBEDDED_MIGRATION_TIMEOUT_MS || 120000),
    );
    if (status.error || status.status !== 0) {
      throw new Error('Embedded backend migrations failed; the database was not reset.');
    }
    appendLine(runtime.processLogPath, 'alembic upgrade head complete');
  }

  runMigrationSafetyHelper(runtime, args) {
    const result = this.runKeyedSyncProcess(
      runtime,
      runtime.pythonPath,
      ['-m', 'app.migration_safety', ...args],
      Number(process.env.BREAKTWENTY_EMBEDDED_MIGRATION_TIMEOUT_MS || 120000),
    );
    if (result.error || result.status !== 0) {
      throw new Error('Embedded database migration safety check failed.');
    }
    try {
      return JSON.parse(String(result.stdout || '').trim());
    } catch (_error) {
      throw new Error('Embedded database migration safety check returned invalid output.');
    }
  }

  runKeyedSyncProcess(runtime, command, args, timeout) {
    const payload = this.keyManager.bootstrapPayload(this.getLaunchAuthTokens());
    try {
      return spawnSync(
        command,
        args,
        {
          cwd: runtime.backendRoot,
          env: runtime.env,
          input: payload,
          encoding: 'utf8',
          timeout,
          maxBuffer: 1024 * 1024,
        },
      );
    } finally {
      payload.fill(0);
    }
  }

  spawnBackend(runtime) {
    appendLine(
      runtime.processLogPath,
      `starting embedded backend on ${runtime.host}:${runtime.port || 'dynamic'}`,
    );
    const child = spawn(
      runtime.pythonPath,
      [
        '-m',
        'app.desktop_server',
        '--host',
        runtime.host,
        '--port',
        String(runtime.port),
      ],
      {
        cwd: runtime.backendRoot,
        env: runtime.env,
        stdio: ['pipe', 'pipe', 'pipe'],
      },
    );
    this.process = child;
    const payload = this.keyManager.bootstrapPayload(this.getLaunchAuthTokens());
    let keyMaterialCleared = false;
    const clearKeyMaterial = () => {
      if (keyMaterialCleared) return;
      keyMaterialCleared = true;
      payload.fill(0);
      this.keyManager.clear();
    };
    child.stdin.once('error', clearKeyMaterial);
    child.stdin.end(payload, clearKeyMaterial);
    let stdoutBuffer = '';
    let endpointSettled = false;
    let endpointTimer = null;
    let resolveEndpoint;
    let rejectEndpoint;
    const endpointPromise = new Promise((resolve, reject) => {
      resolveEndpoint = resolve;
      rejectEndpoint = reject;
    });
    const settleEndpoint = (error = null) => {
      if (endpointSettled) return;
      endpointSettled = true;
      if (endpointTimer) clearTimeout(endpointTimer);
      if (error) rejectEndpoint(error);
      else resolveEndpoint();
    };
    const acceptEndpoint = (line) => {
      if (line.startsWith(EMBEDDED_BACKEND_BIND_ERROR_PREFIX)) {
        const error = new Error('Embedded backend could not bind its loopback endpoint.');
        error.code = 'BACKEND_BIND_FAILED';
        settleEndpoint(error);
        return;
      }
      if (!line.startsWith(EMBEDDED_BACKEND_LISTENING_PREFIX)) return;
      try {
        const endpoint = JSON.parse(line.slice(EMBEDDED_BACKEND_LISTENING_PREFIX.length));
        const host = String(endpoint?.host || '');
        const port = Number(endpoint?.port || 0);
        if (
          host !== runtime.host
          || !Number.isInteger(port)
          || port < 1
          || port > 65535
          || (runtime.port > 0 && port !== runtime.port)
        ) {
          throw new Error('Embedded backend reported an invalid loopback endpoint.');
        }
        this.backendHost = host;
        this.backendPort = port;
        this.backendApiUrl = normalizeLocalBackendApiUrl(
          `http://${host.includes(':') ? `[${host}]` : host}:${port}/api`,
        );
        this.runtimeEnv = {
          ...this.runtimeEnv,
          BREAKTWENTY_BACKEND_API_URL: this.backendApiUrl,
        };
        appendLine(runtime.processLogPath, `embedded backend reserved ${host}:${port}`);
        settleEndpoint();
      } catch (error) {
        settleEndpoint(error);
      }
    };
    child.stdout.on('data', (chunk) => {
      const output = String(chunk || '');
      appendLine(runtime.processLogPath, `stdout ${output.trim()}`);
      stdoutBuffer += output;
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() || '';
      lines.forEach(acceptEndpoint);
    });
    child.stderr.on('data', (chunk) => {
      appendLine(runtime.processLogPath, `stderr ${String(chunk || '').trim()}`);
    });
    child.on('exit', (code, signal) => {
      if (this.process !== child) return;
      settleEndpoint(new Error(`Embedded backend exited before reserving its loopback endpoint (code ${code}).`));
      this.status.state = this.shuttingDown || this.recovering || code === 0 ? 'stopped' : 'failed';
      this.status.message = `Embedded backend exited with code ${code} signal ${signal || ''}`.trim();
      this.status.pid = null;
      appendLine(runtime.processLogPath, this.status.message);
    });
    child.on('error', (error) => {
      if (this.process !== child) return;
      settleEndpoint(error);
      this.status.state = 'failed';
      this.status.message = error.message;
      appendLine(runtime.processLogPath, `process error: ${error.message}`);
    });
    endpointTimer = setTimeout(() => {
      settleEndpoint(new Error('Embedded backend did not reserve a loopback endpoint in time.'));
    }, Number(process.env.BREAKTWENTY_EMBEDDED_LISTEN_TIMEOUT_MS || EMBEDDED_BACKEND_LISTEN_TIMEOUT_MS));
    return endpointPromise;
  }
}

module.exports = {
  BackendManager,
  EMBEDDED_BACKEND_LISTENING_PREFIX,
  normalizeBackendMode,
};
