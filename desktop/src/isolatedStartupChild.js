const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const { EventEmitter } = require('node:events');
const Module = require('node:module');
const net = require('node:net');
const path = require('node:path');
const {
  assertIsolatedTestEnvironment,
  isPathInside,
} = require('./isolatedTestContract');

const RECEIPT_PREFIX = 'BREAKTWENTY_ISOLATED_STARTUP_RECEIPT ';
const STARTUP_TIMEOUT_MS = 7000;
const contract = assertIsolatedTestEnvironment(process.env);
const events = [];
const requests = [];
const windows = [];
let failure = null;

function record(name, details = {}) {
  events.push({ index: events.length, ...details, name });
}

function eventIndex(name) {
  return events.find((event) => event.name === name)?.index ?? -1;
}

function assertAllowedUrl(value) {
  const parsed = new URL(String(value));
  if (![contract.backendOrigin, contract.frontendOrigin].includes(parsed.origin)) {
    throw new Error(`The isolated startup blocked external network access to ${parsed.origin}.`);
  }
  requests.push(parsed.toString());
}

const allowedSocketPorts = new Set([
  Number(new URL(contract.backendOrigin).port),
  Number(new URL(contract.frontendOrigin).port),
]);

function assertAllowedSocket(args) {
  const options = args[0] && typeof args[0] === 'object'
    ? args[0]
    : { port: args[0], host: args[1] };
  const host = String(options.host || '127.0.0.1');
  const port = Number(options.port);
  if (host !== '127.0.0.1' || !allowedSocketPorts.has(port)) {
    throw new Error(`The isolated startup blocked socket access to ${host}:${port || 'unknown'}.`);
  }
}

const originalNetConnect = net.connect.bind(net);
const originalNetCreateConnection = net.createConnection.bind(net);
net.connect = (...args) => {
  assertAllowedSocket(args);
  return originalNetConnect(...args);
};
net.createConnection = (...args) => {
  assertAllowedSocket(args);
  return originalNetCreateConnection(...args);
};

for (const method of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) {
  childProcess[method] = () => {
    throw new Error(`The isolated startup blocked subprocess method ${method}.`);
  };
}

const originalFetch = global.fetch.bind(global);
global.fetch = (input, init) => {
  const value = typeof input === 'string' || input instanceof URL ? input : input.url;
  assertAllowedUrl(value);
  return originalFetch(input, init);
};

const httpModule = require('node:http');
const originalHttpRequest = httpModule.request.bind(httpModule);
const originalHttpGet = httpModule.get.bind(httpModule);
httpModule.request = (input, ...args) => {
  assertAllowedUrl(input instanceof URL ? input : String(input));
  return originalHttpRequest(input, ...args);
};
httpModule.get = (input, ...args) => {
  assertAllowedUrl(input instanceof URL ? input : String(input));
  return originalHttpGet(input, ...args);
};

const httpsModule = require('node:https');
httpsModule.request = () => {
  throw new Error('The isolated startup blocks every HTTPS request.');
};
httpsModule.get = httpsModule.request;

class MockWebContents extends EventEmitter {
  constructor() {
    super();
    this.url = '';
    this.zoomFactor = 1;
  }

  getURL() { return this.url; }
  isDestroyed() { return false; }
  setWindowOpenHandler(handler) { this.windowOpenHandler = handler; }
  setZoomFactor(value) { this.zoomFactor = value; }
  getZoomFactor() { return this.zoomFactor; }
  send() {}
  reload() {}
  reloadIgnoringCache() {}
}

class MockBrowserWindow extends EventEmitter {
  static getAllWindows() { return windows.filter((window) => !window.destroyed); }

  constructor(options) {
    super();
    this.options = options;
    this.destroyed = false;
    this.visible = true;
    this.maximized = false;
    this.webContents = new MockWebContents();
    windows.push(this);
    record('browser-window-created', {
      sandbox: options?.webPreferences?.sandbox,
      contextIsolation: options?.webPreferences?.contextIsolation,
      nodeIntegration: options?.webPreferences?.nodeIntegration,
    });
  }

  async loadURL(url) {
    this.webContents.url = String(url);
    record('window-load-url', {
      kind: String(url).startsWith('data:') ? 'loading' : 'frontend',
      url: String(url).startsWith('data:') ? 'data:' : String(url),
    });
    setImmediate(() => this.webContents.emit('did-finish-load'));
  }

  getBounds() { return { width: this.options.width, height: this.options.height, x: 0, y: 0 }; }
  isDestroyed() { return this.destroyed; }
  isVisible() { return this.visible; }
  isMinimized() { return false; }
  maximize() { this.maximized = true; }
  focus() { record('window-focused'); }
  restore() {}
  show() { this.visible = true; }
  hide() { this.visible = false; }
  setIcon() {}
  setAppDetails() {}
}

const appPaths = new Map([
  ['appData', path.join(contract.root, 'app-data')],
  ['userData', contract.userData],
  ['sessionData', contract.userData],
  ['home', contract.home],
  ['temp', path.join(contract.root, 'tmp')],
  ['exe', process.execPath],
]);
const app = new EventEmitter();
app.isPackaged = false;
app.commandLine = {
  appendSwitch(name, value) { record('command-line-switch', { name, value: value || '' }); },
  hasSwitch() { return false; },
};
app.getPath = (name) => appPaths.get(name) || path.join(contract.root, name);
app.setPath = (name, value) => {
  const resolved = path.resolve(value);
  assert.ok(isPathInside(contract.root, resolved), `${name} escaped the isolated root`);
  appPaths.set(name, resolved);
  record('app-path-set', { name });
};
app.setName = (name) => record('app-name-set', { name });
app.setAppUserModelId = (id) => record('app-user-model-id-set', { id });
app.setDesktopName = (name) => record('desktop-name-set', { name });
app.requestSingleInstanceLock = () => {
  record('single-instance-lock');
  return true;
};
app.enableSandbox = () => record('sandbox-enabled');
app.whenReady = () => {
  record('when-ready-requested');
  return Promise.resolve();
};
app.isReady = () => true;
app.getVersion = () => '0.0.0-isolated-test';
app.quit = () => record('app-quit');
app.relaunch = () => { throw new Error('The isolated startup cannot relaunch.'); };
app.exit = () => { throw new Error('The isolated startup cannot exit through Electron.'); };

const electronLog = {
  transports: {
    file: {
      maxSize: 0,
      getFile: () => ({ path: path.join(contract.root, 'logs', 'electron.log') }),
    },
  },
  debug: () => {},
  error: (...args) => record('electron-log-error', { message: args.join(' ') }),
  info: () => {},
  warn: () => {},
};

const ipcMain = new EventEmitter();
ipcMain.handle = (channel) => record('ipc-handler', { channel });
ipcMain.removeHandler = () => {};
const screen = new EventEmitter();
const display = {
  scaleFactor: 1,
  workAreaSize: { width: 1920, height: 1080 },
};
screen.getPrimaryDisplay = () => display;
screen.getDisplayMatching = () => display;

const electron = {
  app,
  BrowserWindow: MockBrowserWindow,
  dialog: {
    showErrorBox: (title, message) => record('dialog-error', { title, message }),
    showMessageBox: async () => ({ response: 0 }),
    showSaveDialog: async () => ({ canceled: true }),
  },
  ipcMain,
  Menu: {
    buildFromTemplate: (template) => ({ template }),
    setApplicationMenu: () => record('application-menu-installed'),
  },
  safeStorage: {
    isEncryptionAvailable: () => false,
  },
  screen,
  session: {
    defaultSession: {
      clearCache: async () => {},
      clearStorageData: async () => {},
    },
  },
  shell: {
    openExternal: async () => { throw new Error('External navigation is blocked in isolated startup.'); },
    showItemInFolder: () => { throw new Error('Filesystem reveal is blocked in isolated startup.'); },
  },
};

class FakeBackendManager {
  constructor() {
    record('backend-manager-created');
    this.runtimePaths = {
      dataDir: path.join(contract.root, 'data'),
      logDir: path.join(contract.root, 'logs'),
    };
  }

  prepareRuntimeEnv() { record('backend-runtime-prepared'); }
  async start() { record('backend-started'); }
  async shutdown() { record('backend-stopped'); return true; }
  async restart() { record('backend-restarted'); }
  quiesce() { record('backend-quiesced'); }
  async revokeLaunchAuthentication() { return true; }
  acknowledgeHealthyStartup() { record('backend-health-acknowledged'); }
  setFrontendOrigin() {}
  getBackendApiUrl() { return process.env.BREAKTWENTY_BACKEND_API_URL; }
  getDesktopLaunchAuthToken() { return process.env.BREAKTWENTY_ISOLATED_OWNERSHIP_PROOF_KEY; }
  getRendererLaunchAuthToken() { return 'isolated-renderer-token'; }
  getRuntimeEnv() {
    return {
      BREAKTWENTY_DESKTOP_AUTH_DIR: contract.desktopAuth,
      BREAKTWENTY_LOG_DIR: this.runtimePaths.logDir,
      BREAKTWENTY_RESOURCE_ROOT: path.resolve(__dirname, '..', '..'),
    };
  }
  describe() { return { state: 'ready', runtimePaths: this.runtimePaths, logPath: null }; }
  async requestStackDumps() { return []; }
}

class FakeVisibleAuthBroker {
  constructor() { record('visible-auth-created'); }
  setBackendApiUrl() {}
  setRuntimeEnv() {}
  describe() { return { status: 'isolated' }; }
  quiesce() {}
  async shutdown() { return true; }
}

class FakeUpdater {
  constructor() { record('updater-created'); }
  scheduleStartupCheck() { record('updater-startup-check-suppressed'); }
  getStatus() { return { status: 'disabled' }; }
  quiesce() {}
}

class FakeAppDiagnostics {
  constructor() { record('app-diagnostics-created'); }
  list() { return []; }
  policy() { return {}; }
}

const originalLoad = Module._load;
Module._load = function isolatedStartupLoad(request, parent, isMain) {
  if (request === 'electron') return electron;
  if (request === 'electron-log') return electronLog;
  if (request === 'electron-updater') return { autoUpdater: new EventEmitter() };
  if (request === 'electron-updater/out/types') return { DOWNLOAD_PROGRESS: 'download-progress' };

  const loaded = originalLoad.call(this, request, parent, isMain);
  if (parent?.filename?.endsWith(`${path.sep}main.js`)) {
    if (request === './appIdentity') {
      const testIdentity = {
        technicalId: contract.appId,
        storageNamespace: `BreakTwenty Test ${contract.marker.slice(-12)}`,
        windowsLocalAppDataNamespace: `BreakTwenty Test ${contract.marker.slice(-12)}`,
      };
      return {
        ...loaded,
        configureApplicationIdentity: (targetApp, options = {}) => loaded.configureApplicationIdentity(
          targetApp,
          {
            ...options,
            metadata: { breaktwentyDesktopIdentity: testIdentity },
          },
        ),
        resolvePackagedIdentity: () => testIdentity,
      };
    }
    if (request === './backendManager') return { ...loaded, BackendManager: FakeBackendManager };
    if (request === './visibleAuthBroker') return { ...loaded, VisibleAuthBroker: FakeVisibleAuthBroker };
    if (request === './appUpdater') return { ...loaded, BreakTwentyAppUpdater: FakeUpdater };
    if (request === './appDiagnostics') return { ...loaded, AppDiagnostics: FakeAppDiagnostics };
    if (request === './browserRuntimePrewarm') {
      return {
        ...loaded,
        startBrowserRuntimePrewarm: () => {
          record('browser-prewarm-suppressed');
          return { state: 'skipped', shutdown: async () => true };
        },
      };
    }
    if (request === './updateInstallHelper') {
      return { ...loaded, markUpdateInstallWindowVisible: () => true };
    }
  }
  return loaded;
};

function fail(error) {
  if (failure) return;
  failure = error instanceof Error ? error : new Error(String(error));
}

process.on('uncaughtException', fail);
process.on('unhandledRejection', fail);

async function waitForStartup() {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (failure) throw failure;
    const frontendLoad = events.find(
      (event) => event.name === 'window-load-url' && event.kind === 'frontend',
    );
    if (frontendLoad) return frontendLoad;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(
    `The isolated startup did not reach the frontend handoff within ${STARTUP_TIMEOUT_MS} ms. `
    + `Requests: ${JSON.stringify(requests)} Events: ${JSON.stringify(events)}`,
  );
}

async function main() {
  record('bootstrap-required');
  require('./bootstrap');
  const frontendLoad = await waitForStartup();

  assert.equal(frontendLoad.url, process.env.BREAKTWENTY_FRONTEND_URL);
  assert.equal(windows.length, 1);
  assert.equal(windows[0].options.webPreferences.sandbox, true);
  assert.equal(windows[0].options.webPreferences.contextIsolation, true);
  assert.equal(windows[0].options.webPreferences.nodeIntegration, false);
  assert.ok(events.some(
    (event) => event.name === 'app-user-model-id-set' && event.id === contract.appId,
  ));
  assert.ok(eventIndex('single-instance-lock') > eventIndex('bootstrap-required'));
  assert.ok(eventIndex('single-instance-lock') < eventIndex('when-ready-requested'));
  assert.ok(eventIndex('when-ready-requested') < eventIndex('backend-manager-created'));
  assert.ok(eventIndex('backend-manager-created') < eventIndex('browser-window-created'));
  assert.ok(eventIndex('browser-window-created') < eventIndex('backend-started'));
  assert.ok(eventIndex('backend-started') < eventIndex('backend-health-acknowledged'));
  assert.ok(eventIndex('backend-health-acknowledged') < frontendLoad.index);
  assert.ok(requests.some((request) => request.startsWith(`${contract.backendOrigin}/api/health/ownership`)));
  assert.ok(requests.some((request) => request === `${contract.backendOrigin}/api/health`));
  assert.ok(requests.some((request) => request === `${contract.frontendOrigin}/`));

  const receipt = {
    marker: contract.marker,
    assertions: {
      bootstrapLoadedMain: true,
      importsResolved: true,
      singleInstanceLockBeforeReadiness: true,
      backendReachedReadiness: true,
      browserWindowCreated: true,
      frontendHandoffReached: true,
      uncaughtFailuresRejected: true,
    },
  };
  process.stdout.write(`${RECEIPT_PREFIX}${JSON.stringify(receipt)}\n`);
}

main().then(
  () => process.exit(0),
  (error) => {
    process.stderr.write(`${error.stack || error.message}\n`);
    process.exit(1);
  },
);
