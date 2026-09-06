const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { APP_BRAND_NAME } = require('./brand');
const { terminateChildProcess } = require('./appLifecycle');
const { resolveVisibleAuthPython } = require('./visibleAuthPython');

const PREWARM_LOG_MAX_BYTES = 2 * 1024 * 1024;
const PREWARM_LOG_BACKUP_COUNT = 2;

function envDisables(value) {
  return ['0', 'false', 'no', 'off'].includes(String(value || '').trim().toLowerCase());
}

function pathExists(filePath) {
  try {
    return fs.existsSync(filePath);
  } catch (_error) {
    return false;
  }
}

function appendLine(filePath, line) {
  if (!filePath) {
    return;
  }
  try {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    rotateLogFile(filePath, PREWARM_LOG_MAX_BYTES, PREWARM_LOG_BACKUP_COUNT);
    fs.appendFileSync(filePath, `${new Date().toISOString()} ${line}\n`, 'utf8');
  } catch (_error) {
    // Prewarm logging is best-effort; visible auth can still prepare the runtime on demand.
  }
}

function rotateLogFile(filePath, maxBytes, backupCount) {
  if (!filePath || !Number.isFinite(maxBytes) || maxBytes <= 0 || !Number.isInteger(backupCount) || backupCount <= 0) {
    return;
  }
  try {
    if (!fs.existsSync(filePath) || fs.statSync(filePath).size < maxBytes) {
      return;
    }
    for (let index = backupCount - 1; index >= 1; index -= 1) {
      const source = `${filePath}.${index}`;
      const target = `${filePath}.${index + 1}`;
      if (fs.existsSync(source)) {
        fs.renameSync(source, target);
      }
    }
    fs.renameSync(filePath, `${filePath}.1`);
  } catch (_error) {
  }
}

function shouldPrewarm() {
  const configured = process.env.BREAKTWENTY_DESKTOP_PREWARM_BROWSER_RUNTIME;
  return !envDisables(configured);
}

function startBrowserRuntimePrewarm({
  appRoot,
  runtimeEnv = {},
} = {}) {
  const resourceRoot = path.resolve(runtimeEnv.BREAKTWENTY_RESOURCE_ROOT || appRoot || process.cwd());
  const runtimeLogSubdir = path.join('runtimes', 'browser-runtime');
  const logDir = path.resolve(
    runtimeEnv.BREAKTWENTY_LOG_DIR
      ? path.join(runtimeEnv.BREAKTWENTY_LOG_DIR, runtimeLogSubdir)
      : (
          runtimeEnv.BREAKTWENTY_DESKTOP_AUTH_DIR
            ? path.join(runtimeEnv.BREAKTWENTY_DESKTOP_AUTH_DIR, 'logs', runtimeLogSubdir)
            : path.join(resourceRoot, 'data', 'logs', runtimeLogSubdir)
        ),
  );
  const logPath = path.join(logDir, 'prewarm.log');
  const state = {
    enabled: shouldPrewarm(),
    state: 'disabled',
    runtime: 'brave',
    pid: null,
    logPath,
    message: '',
  };
  let child = null;
  let shutdownRequested = false;
  Object.defineProperty(state, 'shutdown', {
    enumerable: false,
    value: async () => {
      shutdownRequested = true;
      if (!child || child.exitCode !== null) {
        state.pid = null;
        return true;
      }
      state.state = 'stopping';
      state.message = 'Browser runtime prewarm is stopping.';
      appendLine(logPath, state.message);
      const stopped = await terminateChildProcess(child, { processTree: true });
      state.pid = null;
      state.state = stopped ? 'stopped' : 'failed';
      state.message = stopped
        ? 'Browser runtime prewarm stopped.'
        : 'Browser runtime prewarm did not stop cleanly.';
      appendLine(logPath, state.message);
      return stopped;
    },
  });

  if (!state.enabled) {
    state.message = 'Browser runtime prewarm is disabled.';
    return state;
  }

  const pythonPath = resolveVisibleAuthPython({ runtimeEnv, resourceRoot });
  const scriptPath = path.join(resourceRoot, 'scripts', 'desktop_browser_runtime.py');
  if (!pythonPath) {
    state.state = 'skipped';
    state.message = `No ${APP_BRAND_NAME}-managed visible-auth Python runtime was available for browser runtime prewarm.`;
    appendLine(logPath, state.message);
    return state;
  }
  if (!pathExists(scriptPath)) {
    state.state = 'skipped';
    state.message = `Browser runtime helper is missing at ${scriptPath}.`;
    appendLine(logPath, state.message);
    return state;
  }

  appendLine(logPath, `starting managed browser runtime prewarm with ${pythonPath}`);
  child = spawn(
    pythonPath,
    [scriptPath, '--json', '--log-path', logPath],
    {
      cwd: resourceRoot,
      detached: true,
      env: {
        ...process.env,
        ...runtimeEnv,
        PYTHONUNBUFFERED: '1',
      },
      stdio: 'ignore',
      windowsHide: true,
    },
  );

  state.state = 'starting';
  state.pid = child.pid || null;
  state.message = 'Browser runtime prewarm started.';
  appendLine(logPath, `spawned browser runtime prewarm pid=${state.pid || 'unknown'}`);

  child.once('spawn', () => {
    if (shutdownRequested) return;
    state.state = 'running';
    state.message = 'Browser runtime prewarm is running.';
  });
  child.once('error', (error) => {
    if (shutdownRequested) {
      state.state = 'stopped';
      state.pid = null;
      state.message = 'Browser runtime prewarm stopped.';
      return;
    }
    state.state = 'failed';
    state.message = error.message;
    appendLine(logPath, `prewarm process error: ${error.message}`);
  });
  child.once('exit', (code, signal) => {
    state.pid = null;
    state.state = shutdownRequested ? 'stopped' : (code === 0 ? 'ready' : 'failed');
    state.message = shutdownRequested
      ? 'Browser runtime prewarm stopped.'
      : (code === 0
      ? 'Browser runtime prewarm completed.'
      : `Browser runtime prewarm exited with code ${code} signal ${signal || ''}`.trim());
    appendLine(logPath, state.message);
  });
  child.unref();

  return state;
}

module.exports = {
  startBrowserRuntimePrewarm,
};
