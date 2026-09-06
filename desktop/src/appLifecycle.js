const { spawn } = require('node:child_process');

function wait(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function childIsRunning(child) {
  return Boolean(child && child.exitCode === null && child.signalCode === null);
}

function waitForChildExit(child, timeoutMs = 5000) {
  if (!childIsRunning(child)) {
    return Promise.resolve(true);
  }
  return new Promise((resolve) => {
    let settled = false;
    const finish = (exited) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener('exit', onExit);
      child.removeListener('error', onExit);
      resolve(exited);
    };
    const onExit = () => finish(true);
    const timer = setTimeout(() => finish(!childIsRunning(child)), timeoutMs);
    child.once('exit', onExit);
    child.once('error', onExit);
  });
}

function processGroupIsRunning(pid, killProcess = process.kill) {
  if (!pid) return false;
  try {
    killProcess(-pid, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM';
  }
}

async function waitForProcessGroupExit(pid, timeoutMs, killProcess = process.kill) {
  const deadline = Date.now() + timeoutMs;
  while (processGroupIsRunning(pid, killProcess) && Date.now() < deadline) {
    await wait(50);
  }
  return !processGroupIsRunning(pid, killProcess);
}

function runTaskkill(pid, force, spawnProcess = spawn) {
  return new Promise((resolve) => {
    if (!pid) {
      resolve(false);
      return;
    }
    const args = ['/PID', String(pid), '/T'];
    if (force) args.push('/F');
    let child;
    try {
      child = spawnProcess('taskkill', args, {
        stdio: 'ignore',
        windowsHide: true,
      });
    } catch (_error) {
      resolve(false);
      return;
    }
    child.once('error', () => resolve(false));
    child.once('exit', (code) => resolve(code === 0));
  });
}

async function signalChildProcess(child, signal, {
  killProcess = process.kill,
  platform = process.platform,
  processTree = false,
  spawnProcess = spawn,
} = {}) {
  if (!child?.pid) return false;
  if (processTree && platform === 'win32') {
    const killed = await runTaskkill(child.pid, signal === 'SIGKILL', spawnProcess);
    if (killed) return true;
  }
  if (processTree && platform !== 'win32') {
    try {
      killProcess(-child.pid, signal);
      return true;
    } catch (_error) {
    }
  }
  try {
    return child.kill(signal);
  } catch (_error) {
    return false;
  }
}

async function terminateChildProcess(child, {
  forceWaitMs = 2000,
  graceMs = 5000,
  killProcess = process.kill,
  platform = process.platform,
  processTree = false,
  spawnProcess = spawn,
} = {}) {
  const detachedTreeIsRunning = processTree
    && platform !== 'win32'
    && processGroupIsRunning(child?.pid, killProcess);
  if (!childIsRunning(child) && !detachedTreeIsRunning) {
    return true;
  }
  const pid = child.pid;
  await signalChildProcess(child, 'SIGTERM', {
    killProcess,
    platform,
    processTree,
    spawnProcess,
  });
  const childExited = await waitForChildExit(child, graceMs);
  const treeExited = !processTree || platform === 'win32'
    ? childExited
    : (childExited && await waitForProcessGroupExit(pid, 100, killProcess));
  if (childExited && treeExited) {
    return true;
  }
  await signalChildProcess(child, 'SIGKILL', {
    killProcess,
    platform,
    processTree,
    spawnProcess,
  });
  const forcedChildExit = await waitForChildExit(child, forceWaitMs);
  const forcedTreeExit = !processTree || platform === 'win32'
    ? forcedChildExit
    : (forcedChildExit && await waitForProcessGroupExit(pid, forceWaitMs, killProcess));
  return forcedChildExit && forcedTreeExit;
}

function closeHttpServer(server, timeoutMs = 3000) {
  if (!server) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (closed) => {
      if (settled) return;
      settled = true;
      clearTimeout(forceTimer);
      clearTimeout(fallbackTimer);
      resolve(closed);
    };
    const forceTimer = setTimeout(() => {
      try {
        server.closeAllConnections?.();
      } catch (_error) {
      }
    }, timeoutMs);
    const fallbackTimer = setTimeout(() => finish(false), timeoutMs + 500);
    try {
      server.close((error) => finish(!error || error.code === 'ERR_SERVER_NOT_RUNNING'));
      server.closeIdleConnections?.();
    } catch (error) {
      finish(error?.code === 'ERR_SERVER_NOT_RUNNING');
    }
  });
}

function focusExistingWindow(window) {
  if (!window || window.isDestroyed?.()) return false;
  if (window.isMinimized?.()) window.restore();
  window.show?.();
  window.focus?.();
  return true;
}

class DesktopLifecycleCoordinator {
  constructor({ phases = [], logger = null } = {}) {
    this.phases = phases;
    this.logger = logger;
    this.state = 'running';
    this.shutdownPromise = null;
    this.shutdownReason = null;
  }

  isShuttingDown() {
    return this.state !== 'running';
  }

  shutdown(reason = 'quit') {
    if (this.shutdownPromise) return this.shutdownPromise;
    this.shutdownReason = reason;
    this.state = 'quiescing';
    this.shutdownPromise = this.runShutdown();
    return this.shutdownPromise;
  }

  async runShutdown() {
    const report = [];
    for (const phase of this.phases) {
      this.state = phase.state || 'stopping';
      const tasks = Array.isArray(phase.tasks) ? phase.tasks : [];
      const results = await Promise.all(tasks.map((task) => this.runTask(
        phase.name || 'shutdown',
        task,
        phase.timeoutMs,
      )));
      report.push(...results);
    }
    this.state = 'stopped';
    return {
      reason: this.shutdownReason,
      state: this.state,
      tasks: report,
    };
  }

  async runTask(phaseName, task, phaseTimeoutMs) {
    const name = task.name || 'unnamed';
    const timeoutMs = Number(task.timeoutMs || phaseTimeoutMs || 10000);
    let timer = null;
    try {
      const result = await Promise.race([
        Promise.resolve().then(() => task.run()),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(`timed out after ${timeoutMs}ms`)), timeoutMs);
        }),
      ]);
      if (result === false) {
        throw new Error('reported incomplete shutdown');
      }
      return { phase: phaseName, name, status: 'completed', result };
    } catch (error) {
      this.logger?.warn?.(`Shutdown task ${phaseName}/${name} failed: ${error.message}`);
      return { phase: phaseName, name, status: 'failed', message: error.message };
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
}

module.exports = {
  DesktopLifecycleCoordinator,
  childIsRunning,
  closeHttpServer,
  focusExistingWindow,
  terminateChildProcess,
  waitForChildExit,
};
