const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const {
  DesktopLifecycleCoordinator,
  closeHttpServer,
  focusExistingWindow,
  terminateChildProcess,
} = require('./appLifecycle');

test('runs shutdown phases in order and reuses one in-flight shutdown', async () => {
  const calls = [];
  const coordinator = new DesktopLifecycleCoordinator({
    phases: [
      {
        name: 'quiesce',
        tasks: [{ name: 'entrypoints', run: () => calls.push('quiesce') }],
      },
      {
        name: 'runtime-stop',
        tasks: [{ name: 'runtime', run: async () => calls.push('runtime') }],
      },
      {
        name: 'backend-stop',
        tasks: [{ name: 'backend', run: () => calls.push('backend') }],
      },
    ],
  });

  const firstShutdown = coordinator.shutdown('test');
  const secondShutdown = coordinator.shutdown('ignored');

  assert.equal(firstShutdown, secondShutdown);
  const report = await firstShutdown;
  assert.deepEqual(calls, ['quiesce', 'runtime', 'backend']);
  assert.equal(report.reason, 'test');
  assert.equal(report.state, 'stopped');
  assert.equal(coordinator.state, 'stopped');
});

test('continues later shutdown phases after a task fails', async () => {
  const calls = [];
  const coordinator = new DesktopLifecycleCoordinator({
    phases: [
      {
        name: 'runtime-stop',
        tasks: [{ name: 'broken', run: () => { throw new Error('broken runtime'); } }],
      },
      {
        name: 'backend-stop',
        tasks: [{ name: 'backend', run: () => calls.push('backend') }],
      },
    ],
  });

  const report = await coordinator.shutdown('test');

  assert.deepEqual(calls, ['backend']);
  assert.equal(report.tasks[0].status, 'failed');
  assert.equal(report.tasks[1].status, 'completed');
  assert.equal(coordinator.state, 'stopped');
});

test('restores, shows, and focuses an existing window', () => {
  const calls = [];
  const window = {
    focus: () => calls.push('focus'),
    isDestroyed: () => false,
    isMinimized: () => true,
    restore: () => calls.push('restore'),
    show: () => calls.push('show'),
  };

  assert.equal(focusExistingWindow(window), true);
  assert.deepEqual(calls, ['restore', 'show', 'focus']);
  assert.equal(focusExistingWindow({ isDestroyed: () => true }), false);
});

test('terminates a child gracefully before using the forced fallback', async () => {
  const child = new EventEmitter();
  const signals = [];
  child.exitCode = null;
  child.signalCode = null;
  child.pid = 123;
  child.kill = (signal) => {
    signals.push(signal);
    setImmediate(() => {
      child.exitCode = 0;
      child.emit('exit', 0, signal);
    });
    return true;
  };

  assert.equal(await terminateChildProcess(child, { graceMs: 50 }), true);
  assert.deepEqual(signals, ['SIGTERM']);
});

test('force-kills a child that exceeds the graceful timeout', async () => {
  const child = new EventEmitter();
  const signals = [];
  child.exitCode = null;
  child.signalCode = null;
  child.pid = 456;
  child.kill = (signal) => {
    signals.push(signal);
    if (signal === 'SIGKILL') {
      setImmediate(() => {
        child.exitCode = null;
        child.signalCode = 'SIGKILL';
        child.emit('exit', null, 'SIGKILL');
      });
    }
    return true;
  };

  assert.equal(await terminateChildProcess(child, { forceWaitMs: 50, graceMs: 5 }), true);
  assert.deepEqual(signals, ['SIGTERM', 'SIGKILL']);
});

test('closes the build server and drains idle connections', async () => {
  const calls = [];
  const server = {
    close: (callback) => {
      calls.push('close');
      setImmediate(() => callback());
    },
    closeAllConnections: () => calls.push('force'),
    closeIdleConnections: () => calls.push('idle'),
  };

  assert.equal(await closeHttpServer(server, 50), true);
  assert.deepEqual(calls, ['close', 'idle']);
});

test('main and updater keep the single-instance and awaited-install invariants', () => {
  const mainSource = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');
  const updaterSource = fs.readFileSync(path.join(__dirname, 'appUpdater.js'), 'utf8');
  const updateHelperSource = fs.readFileSync(path.join(__dirname, 'updateInstallHelper.js'), 'utf8');
  const backendManagerSource = fs.readFileSync(path.join(__dirname, 'backendManager.js'), 'utf8');
  const backendHealthProbeSource = fs.readFileSync(
    path.join(__dirname, 'backendHealthProbe.js'),
    'utf8',
  );

  assert.ok(mainSource.indexOf('app.requestSingleInstanceLock()') >= 0);
  assert.ok(mainSource.indexOf('app.requestSingleInstanceLock()') < mainSource.indexOf('app.whenReady()'));
  assert.ok(mainSource.indexOf('app.requestSingleInstanceLock()') < mainSource.indexOf('new BackendManager('));
  assert.match(mainSource, /app\.on\('second-instance',[\s\S]*focusRunningAppWindow\(\);/);
  assert.match(mainSource, /app\.on\('before-quit',[\s\S]*event\.preventDefault\(\);[\s\S]*requestApplicationShutdown\('app-quit'\)/);
  assert.match(mainSource, /app\.on\('window-all-closed',[\s\S]*if \(ownsSingleInstanceLock\) \{[\s\S]*app\.quit\(\);/);
  assert.doesNotMatch(mainSource, /window-all-closed'[\s\S]*process\.platform !== 'darwin'/);
  assert.ok(updaterSource.indexOf('await this.prepareForInstall()') >= 0);
  assert.ok(updaterSource.indexOf('await this.prepareForInstall()') < updaterSource.indexOf('autoUpdater.quitAndInstall(true, true)'));
  assert.ok(updaterSource.indexOf('installWindow.hide()') > updaterSource.indexOf('await this.prepareForInstall()'));
  assert.ok(updaterSource.indexOf('installWindow.hide()') < updaterSource.indexOf('await tryStartPersistentInstallHelper('));
  assert.match(updaterSource, /catch \(error\)[\s\S]*hiddenInstallWindow\.show\(\);[\s\S]*hiddenInstallWindow\.focus\(\);/);
  assert.ok(updaterSource.indexOf('await tryStartPersistentInstallHelper(') < updaterSource.indexOf('autoUpdater.quitAndInstall(true, true)'));
  assert.match(updaterSource, /if \(!helperStarted\)[\s\S]*continuing with update installation/);
  assert.doesNotMatch(updaterSource, /if \(!helperStarted\)[\s\S]*throw new Error/);
  assert.doesNotMatch(updaterSource, /update progress window could not stay open/);
  assert.doesNotMatch(updateHelperSource, /waitForHelperPrepared|HELPER_READY_TIMEOUT_MS|\.prepared/);
  assert.doesNotMatch(updateHelperSource, /powershell\.exe|windowsHelperScript/);
  assert.match(updateHelperSource, /BreakTwentyUpdateHelper-\$\{process\.pid\}\.exe/);
  assert.match(updateHelperSource, /BreakTwentyUpdateHelper-\$\{process\.pid\}`/);
  assert.match(updateHelperSource, /HELPER_DISPLAY_DELAY_SECONDS = 3/);
  assert.match(updateHelperSource, /resetUpdateReadySignal\(app\)/);
  assert.match(updateHelperSource, /markUpdateInstallWindowVisible/);
  assert.doesNotMatch(updateHelperSource, /IsApplicationRunning|breaktwentyRelaunched|osascript/);
  assert.match(mainSource, /markUpdateInstallWindowVisible\(app\)/);
  assert.match(
    mainSource,
    /on\('did-finish-load',[\s\S]*createdMainWindow\.isVisible\(\)[\s\S]*signalUpdateInstallVisibility\(\)/,
  );
  assert.match(updateHelperSource, /breaktwenty-mark-dark\.png/);
  assert.match(updateHelperSource, /breaktwenty-wordmark-dark-320\.png/);
  assert.doesNotMatch(updateHelperSource, /zenity|kdialog|xmessage/);
  assert.doesNotMatch(updaterSource, /UPDATE_INSTALL_HANDOFF_DELAY_MS/);
  assert.match(backendManagerSource, /if \(this\.startPromise\)[\s\S]*return this\.startPromise/);
  assert.match(backendManagerSource, /async restart\(\)[\s\S]*recoveryRestart: true/);
  assert.match(backendManagerSource, /BREAKTWENTY_RECOVERY_RESTART/);
  assert.doesNotMatch(mainSource, /\{ role: 'reload' \}|\{ role: 'forceReload' \}/);
  assert.match(mainSource, /reloadMainWindowThroughBackendReadiness/);
  assert.match(mainSource, /probeBackendHealth/);
  assert.match(mainSource, /app\.isPackaged[\s\S]*USE_PACKAGED_DYNAMIC_BACKEND_PORT/);
  assert.match(mainSource, /app\.isPackaged[\s\S]*USE_PACKAGED_DYNAMIC_FRONTEND_PORT/);
  assert.match(
    mainSource,
    /app\.isPackaged && USE_PACKAGED_DYNAMIC_FRONTEND_PORT[\s\S]*partition: PACKAGED_RENDERER_PARTITION/,
  );
  assert.doesNotMatch(mainSource, /PACKAGED_RENDERER_PARTITION\s*=\s*['"]persist:/);
  assert.match(mainSource, /clearLegacyDesktopHttpCaches/);
  assert.match(backendManagerSource, /'app\.desktop_server'/);
  assert.match(mainSource, /backendApiUrl !== previousBackendApiUrl[\s\S]*waitForDidFinishLoad/);
  assert.match(backendHealthProbeSource, /setTimeout[\s\S]*Backend health timed out during/);
  assert.match(mainSource, /breaktwenty:app-diagnostics-list/);
  assert.match(
    mainSource,
    /formatLocalFilenameTimestamp\([\s\S]*incident\.createdAt,[\s\S]*request\.userTimezone/,
  );
  assert.match(mainSource, /role: 'help'[\s\S]*Check for Updates/);
  assert.match(
    mainSource,
    /mainWindowIsOnStartupSurface[\s\S]*\['available', 'downloaded'\][\s\S]*runStartupUpdateRecovery\(\{ automatic: true \}\)/,
  );
  assert.match(
    mainSource,
    /void loadBreakTwentyApp\(createdMainWindow\);\s*appUpdater\?\.scheduleStartupCheck\(\);/,
  );
  assert.doesNotMatch(updaterSource, /scheduleStartupCheck\(delayMs = 10000, runCheck/);
  assert.ok(
    mainSource.indexOf('appUpdater?.scheduleStartupCheck(')
      < mainSource.indexOf("app.on('activate'"),
  );
  assert.match(mainSource, /createdMainWindow\.once\('closed',[\s\S]*startupUpdateOfferedVersion = ''/);
  assert.match(mainSource, /onStatusChange: handleDesktopUpdaterStatusChange/);
  assert.match(updaterSource, /this\.onStatusChange\(status\)/);
});
