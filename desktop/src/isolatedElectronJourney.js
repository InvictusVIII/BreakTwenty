const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const electron = require('electron');
const { ISOLATED_ELECTRON_RECEIPT_PREFIX } = require('./isolatedElectronJourneyProtocol');
const { ISOLATED_JOURNEY_ROUTES, ISOLATED_JOURNEY_VIEWPORTS } = require('./isolatedJourneyRoutes');
const {
  assertIsolatedTestEnvironment,
  assertPackagedNativeSmokeEnvironment,
  configureIsolatedElectronPaths,
} = require('./isolatedTestContract');

const packagedNative = process.env.BREAKTWENTY_ISOLATED_JOURNEY_MODE === 'packaged-native';
const contract = packagedNative
  ? assertPackagedNativeSmokeEnvironment(process.env)
  : assertIsolatedTestEnvironment(process.env);
configureIsolatedElectronPaths(electron.app, process.env, contract);
const frontendOrigin = new URL(contract.frontendOrigin).origin;
const backendOrigin = new URL(contract.backendOrigin).origin;
const screenshotsDir = path.join(contract.root, 'screenshots');
const baselineMode = packagedNative
  ? 'disabled'
  : String(process.env.BREAKTWENTY_VISUAL_BASELINE_MODE || 'verify');
const baselineRoot = path.resolve(__dirname, '..', 'test', 'visual-baselines', process.platform);
const visualFailureRoot = String(process.env.BREAKTWENTY_VISUAL_FAILURE_DIR || '').trim();
const rendererErrors = [];
const blockedRequests = [];
const routeResults = [];
const visualResults = [];
let journeyStarted = false;
let journeyFinished = false;
let journeyWindow = null;
let journeyPhase = 'bootstrap-loaded';
let journeyDeadline = null;

fs.mkdirSync(screenshotsDir, { recursive: true, mode: 0o700 });

if (!packagedNative) {
  for (const method of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) {
    childProcess[method] = () => {
      throw new Error(`The isolated Electron journey blocked subprocess method ${method}.`);
    };
  }
}

class IsolatedBackendManager {
  constructor() {
    this.runtimePaths = {
      dataDir: path.join(contract.root, 'data'),
      logDir: path.join(contract.root, 'logs'),
    };
  }

  prepareRuntimeEnv() {}
  async start() {}
  async shutdown() { return true; }
  async restart() {}
  quiesce() {}
  async revokeLaunchAuthentication() { return true; }
  acknowledgeHealthyStartup() {}
  setFrontendOrigin() {}
  getBackendApiUrl() { return process.env.BREAKTWENTY_BACKEND_API_URL; }
  getDesktopLaunchAuthToken() { return process.env.BREAKTWENTY_ISOLATED_OWNERSHIP_PROOF_KEY; }
  getRendererLaunchAuthToken() { return process.env.BREAKTWENTY_ISOLATED_OWNERSHIP_PROOF_KEY; }
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

class IsolatedPackagedKeyManager {
  constructor() {
    this.keyMaterial = null;
  }

  async loadOrCreate() {
    this.keyMaterial = {
      databaseKey: randomBytes(32),
      appEncryptionKey: randomBytes(32),
    };
    return this.keyMaterial;
  }

  bootstrapPayload({ desktopToken, rendererToken }) {
    if (!this.keyMaterial) throw new Error('Isolated packaged key material is unavailable.');
    return Buffer.from(`${JSON.stringify({
      version: 2,
      databaseKey: this.keyMaterial.databaseKey.toString('base64'),
      appEncryptionKey: this.keyMaterial.appEncryptionKey.toString('base64'),
      desktopLaunchToken: desktopToken,
      rendererLaunchToken: rendererToken,
    })}\n`, 'utf8');
  }

  clear() {
    this.keyMaterial?.databaseKey.fill(0);
    this.keyMaterial?.appEncryptionKey.fill(0);
    this.keyMaterial = null;
  }
}

class DisabledVisibleAuthBroker {
  constructor() {}
  setBackendApiUrl() {}
  setRuntimeEnv() {}
  describe() { return { status: 'disabled-in-isolated-electron-journey' }; }
  quiesce() {}
  async shutdown() { return true; }
  launch() { throw new Error('Provider authentication is disabled in isolated Electron journeys.'); }
  status() { return { status: 'disabled' }; }
  cancel() { return { status: 'disabled' }; }
}

function consoleMessageDetails(eventDetails, legacyLevel, legacyMessage) {
  if (eventDetails && typeof eventDetails === 'object') {
    return {
      level: String(eventDetails.level || '').toLowerCase(),
      message: String(eventDetails.message || ''),
    };
  }
  return {
    level: Number(legacyLevel) === 3 ? 'error' : String(legacyLevel || '').toLowerCase(),
    message: String(legacyMessage || ''),
  };
}

function permittedRendererUrl(value) {
  let parsed;
  try {
    parsed = new URL(String(value));
  } catch (_error) {
    return false;
  }
  if (['data:', 'devtools:'].includes(parsed.protocol)) return true;
  if (!['http:', 'ws:'].includes(parsed.protocol)) return false;
  return [frontendOrigin, backendOrigin].includes(parsed.origin.replace(/^ws:/, 'http:'));
}

function installSessionNetworkBoundary(targetSession) {
  if (targetSession.__breaktwentyIsolatedBoundaryInstalled) return;
  targetSession.__breaktwentyIsolatedBoundaryInstalled = true;
  targetSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
    const allowed = permittedRendererUrl(details.url);
    if (!allowed) blockedRequests.push(details.url);
    callback({ cancel: !allowed });
  });
}

async function waitForRenderer(window, predicateSource, description, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (window.isDestroyed()) throw new Error(`Renderer closed while waiting for ${description}.`);
    const result = await window.webContents.executeJavaScript(`Boolean(${predicateSource})`, true);
    if (result) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Timed out waiting for ${description}.`);
}

async function navigate(window, route) {
  await window.webContents.executeJavaScript(`(() => {
    if (window.location.pathname === ${JSON.stringify(route.path)}) return;
    const link = document.querySelector(${JSON.stringify(`a[href="${route.path}"]`)});
    if (link) {
      link.click();
      return;
    }
    window.history.pushState({}, '', ${JSON.stringify(route.path)});
    window.dispatchEvent(new PopStateEvent('popstate'));
  })()`, true);
}

async function setViewport(window, viewport) {
  if (window.isMaximized()) window.unmaximize();
  window.setBounds({ x: 0, y: 0, width: viewport.width, height: viewport.height }, false);
  window.webContents.setZoomFactor(1);
  await new Promise((resolve) => setTimeout(resolve, 150));
}

async function setTheme(window, theme) {
  await window.webContents.executeJavaScript(`(() => {
    const button = document.querySelector(${JSON.stringify(`[aria-label="Use ${theme} theme"]`)});
    if (!button) return false;
    button.click();
    return true;
  })()`, true);
  await waitForRenderer(
    window,
    `document.documentElement.dataset.breaktwentyTheme === ${JSON.stringify(theme)}`,
    `${theme} theme`,
  );
  await new Promise((resolve) => setTimeout(resolve, 200));
}

async function inspectRoute(window, route, viewport) {
  await navigate(window, route);
  await waitForRenderer(
    window,
    `window.location.pathname === ${JSON.stringify(route.path)} && document.querySelector(${JSON.stringify(route.selector)})`,
    `${route.name} primary landmark`,
  );
  await waitForRenderer(
    window,
    `!document.querySelector(${JSON.stringify(route.loadingSelector)})`,
    `${route.name} loading state to settle`,
  );
  const inspection = await window.webContents.executeJavaScript(`(() => {
    const primary = document.querySelector(${JSON.stringify(route.selector)});
    const populated = document.querySelector(${JSON.stringify(route.populatedSelector)});
    const sidebar = document.querySelector('.floating-nav-rail');
    const toolbar = document.querySelector('.page-shell-toolbar');
    const rect = primary?.getBoundingClientRect();
    const sidebarRect = sidebar?.getBoundingClientRect();
    const toolbarRect = toolbar?.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth;
    return {
      pathname: window.location.pathname,
      primaryPresent: Boolean(primary),
      populatedPresent: Boolean(populated),
      primaryWidth: rect?.width || 0,
      primaryHeight: rect?.height || 0,
      primaryLeft: rect?.left || 0,
      primaryRight: rect?.right || 0,
      rootTextLength: document.getElementById('root')?.textContent?.trim().length || 0,
      viewportWidth,
      viewportHeight: document.documentElement.clientHeight,
      documentOverflowX: Math.max(0, document.documentElement.scrollWidth - viewportWidth),
      sidebarContentOverlap: sidebarRect && rect
        ? Math.max(0, sidebarRect.right - rect.left)
        : 0,
      toolbarOverflowLeft: toolbarRect ? Math.max(0, -toolbarRect.left) : 0,
      toolbarOverflowRight: toolbarRect ? Math.max(0, toolbarRect.right - viewportWidth) : 0,
    };
  })()`, true);
  assert.equal(inspection.pathname, route.path);
  assert.equal(inspection.primaryPresent, true);
  assert.equal(inspection.populatedPresent, true);
  assert.ok(inspection.primaryWidth > 0 && inspection.primaryHeight > 0);
  assert.ok(inspection.rootTextLength > 100);
  assert.ok(inspection.primaryRight > 0 && inspection.primaryLeft < inspection.viewportWidth);
  assert.ok(inspection.documentOverflowX <= 1, `${route.name} overflowed the document horizontally.`);
  assert.ok(inspection.sidebarContentOverlap <= 1, `${route.name} content overlapped the sidebar.`);
  assert.ok(
    inspection.toolbarOverflowLeft <= 1 && inspection.toolbarOverflowRight <= 1,
    `${route.name} toolbar escaped the viewport.`,
  );
  routeResults.push({ ...inspection, name: route.name, viewport: viewport.name });
  return inspection;
}

function bitmapDifference(actual, expected, diffPath = '') {
  const size = actual.getSize();
  assert.deepEqual(size, expected.getSize(), 'Visual baseline dimensions changed.');
  const actualBytes = actual.toBitmap();
  const expectedBytes = expected.toBitmap();
  assert.equal(actualBytes.length, expectedBytes.length);
  const diffBytes = diffPath ? Buffer.alloc(actualBytes.length) : null;
  let changedPixels = 0;
  let totalDelta = 0;
  for (let index = 0; index < actualBytes.length; index += 4) {
    const delta = (
      Math.abs(actualBytes[index] - expectedBytes[index])
      + Math.abs(actualBytes[index + 1] - expectedBytes[index + 1])
      + Math.abs(actualBytes[index + 2] - expectedBytes[index + 2])
    );
    totalDelta += delta;
    if (delta > 72) changedPixels += 1;
    if (diffBytes) {
      const changed = delta > 72;
      diffBytes[index] = changed ? 32 : Math.round(actualBytes[index] * 0.2);
      diffBytes[index + 1] = changed ? 32 : Math.round(actualBytes[index + 1] * 0.2);
      diffBytes[index + 2] = changed ? 255 : Math.round(actualBytes[index + 2] * 0.2);
      diffBytes[index + 3] = 255;
    }
  }
  if (diffBytes) {
    const diffImage = electron.nativeImage.createFromBitmap(diffBytes, {
      width: size.width,
      height: size.height,
      scaleFactor: 1,
    });
    fs.writeFileSync(diffPath, diffImage.toPNG(), { mode: 0o600 });
  }
  const pixels = actualBytes.length / 4;
  return {
    changedPixelRatio: changedPixels / pixels,
    meanChannelDelta: totalDelta / (pixels * 3),
  };
}

async function captureVisualBaseline(window, route, theme) {
  await new Promise((resolve) => setTimeout(resolve, 750));
  const screenshot = await window.webContents.capturePage();
  const actualPath = path.join(screenshotsDir, `${theme}-${route.name}.png`);
  fs.writeFileSync(actualPath, screenshot.toPNG(), { mode: 0o600 });
  const screenshotBytes = fs.statSync(actualPath).size;
  assert.ok(screenshotBytes > 1000);
  const screenshotSize = screenshot.getSize();
  const comparisonImage = screenshot.resize({
    width: Math.max(1, Math.round(screenshotSize.width / 2)),
    height: Math.max(1, Math.round(screenshotSize.height / 2)),
    quality: 'good',
  });
  if (baselineMode === 'disabled') {
    visualResults.push({ name: route.name, theme, screenshotBytes, status: 'captured' });
    return;
  }
  const baselinePath = path.join(baselineRoot, theme, `${route.name}.png`);
  if (baselineMode === 'update') {
    fs.mkdirSync(path.dirname(baselinePath), { recursive: true });
    fs.writeFileSync(baselinePath, comparisonImage.toPNG(), { mode: 0o644 });
    visualResults.push({ name: route.name, theme, screenshotBytes, status: 'updated' });
    return;
  }
  assert.equal(baselineMode, 'verify', `Unsupported visual baseline mode: ${baselineMode}`);
  assert.ok(fs.existsSync(baselinePath), `Visual baseline is missing: ${baselinePath}`);
  const baseline = electron.nativeImage.createFromPath(baselinePath);
  assert.equal(baseline.isEmpty(), false, `Visual baseline could not be loaded: ${baselinePath}`);
  const difference = bitmapDifference(comparisonImage, baseline);
  const matched = difference.changedPixelRatio <= 0.015 && difference.meanChannelDelta <= 2.5;
  if (!matched && visualFailureRoot) {
    const failureDir = path.join(visualFailureRoot, theme, route.name);
    fs.mkdirSync(failureDir, { recursive: true, mode: 0o700 });
    fs.writeFileSync(path.join(failureDir, 'actual.png'), comparisonImage.toPNG(), { mode: 0o600 });
    fs.writeFileSync(path.join(failureDir, 'expected.png'), baseline.toPNG(), { mode: 0o600 });
    bitmapDifference(comparisonImage, baseline, path.join(failureDir, 'diff.png'));
  }
  assert.ok(matched, `${theme}/${route.name} exceeded its visual baseline tolerance: ${JSON.stringify(difference)}`);
  visualResults.push({ name: route.name, theme, screenshotBytes, status: 'matched', ...difference });
}

async function packagedRuntimeStatus(window) {
  return window.webContents.executeJavaScript('window.breaktwentyDesktop.getStatus()', true);
}

async function runJourney(window) {
  await waitForRenderer(
    window,
    `document.querySelector('.dashboard-page-content') && document.querySelector('.dashboard-layout')`,
    'Dashboard startup route',
    packagedNative ? 45000 : 15000,
  );
  await window.webContents.insertCSS(`
    *, *::before, *::after {
      animation-delay: 0s !important;
      animation-duration: 0s !important;
      caret-color: transparent !important;
      scroll-behavior: auto !important;
      transition-delay: 0s !important;
      transition-duration: 0s !important;
    }
  `);
  await setTheme(window, 'dark');
  for (const viewport of ISOLATED_JOURNEY_VIEWPORTS) {
    await setViewport(window, viewport);
    for (const route of ISOLATED_JOURNEY_ROUTES) {
      await inspectRoute(window, route, viewport);
      if (!packagedNative && viewport.name === 'canonical' && route.visual) {
        await captureVisualBaseline(window, route, 'dark');
      }
    }
  }
  if (!packagedNative) {
    const canonical = ISOLATED_JOURNEY_VIEWPORTS[0];
    await setViewport(window, canonical);
    await setTheme(window, 'light');
    for (const route of ISOLATED_JOURNEY_ROUTES.filter((candidate) => candidate.visual)) {
      await inspectRoute(window, route, canonical);
      await captureVisualBaseline(window, route, 'light');
    }
  }
  const runtimeStatus = packagedNative ? await packagedRuntimeStatus(window) : null;
  if (packagedNative) {
    assert.equal(runtimeStatus?.backendMode, 'embedded');
    assert.equal(runtimeStatus?.backendProcess?.mode, 'embedded');
    assert.equal(runtimeStatus?.backendProcess?.state, 'running');
    assert.equal(new URL(runtimeStatus.backendApiUrl).origin, backendOrigin);
  }
  await new Promise((resolve) => setTimeout(resolve, 250));
  assert.deepEqual(rendererErrors, []);
  assert.deepEqual(blockedRequests, []);
  return {
    marker: contract.marker,
    mode: packagedNative ? 'packaged-native' : 'development-electron',
    routes: routeResults,
    visuals: visualResults,
    packagedRuntime: runtimeStatus?.backendProcess || null,
    assertions: {
      realElectronMain: true,
      realChromiumRenderer: true,
      normalMainInitialization: true,
      authenticatedBackendReadiness: true,
      promoSyntheticDataOnly: true,
      rendererConsoleClean: true,
      unintendedNetworkBlocked: true,
      geometryChecked: true,
      broaderRouteCoverage: true,
      controlledVisualBaselines: !packagedNative,
      packagedExecutable: packagedNative,
      productionFrontendBundle: packagedNative,
      embeddedPackagedBackend: packagedNative,
    },
  };
}

async function collectFailureSnapshot(window) {
  if (!window || window.isDestroyed()) return { windowDestroyed: true };
  try {
    return await window.webContents.executeJavaScript(`(() => ({
      location: window.location.href,
      pathname: window.location.pathname,
      title: document.title,
      readyState: document.readyState,
      rootHtml: document.getElementById('root')?.innerHTML?.slice(0, 4000) || '',
      bodyText: document.body?.innerText?.slice(0, 2000) || '',
      promoActive: window.localStorage.getItem('breaktwenty_promo_demo_active_v1'),
    }))()`, true);
  } catch (error) {
    return { snapshotError: error.stack || error.message };
  }
}

function finish(code, payload) {
  if (journeyFinished) return;
  journeyFinished = true;
  if (journeyDeadline) clearTimeout(journeyDeadline);
  process.exitCode = code;
  if (payload) {
    const receipt = `${ISOLATED_ELECTRON_RECEIPT_PREFIX}${JSON.stringify(payload)}\n`;
    if (packagedNative && process.platform === 'darwin') {
      process.stdout.write(receipt, () => electron.app.exit(code));
      setTimeout(() => electron.app.exit(code), 5000);
      return;
    }
    process.stdout.write(receipt);
  }
  if (journeyWindow && !journeyWindow.isDestroyed()) journeyWindow.close();
  else electron.app.quit();
  setTimeout(() => electron.app.exit(code), 5000);
}

class IsolatedBrowserWindow extends electron.BrowserWindow {
  constructor(options = {}) {
    journeyPhase = 'browser-window-constructing';
    super({
      ...options,
      show: false,
      webPreferences: {
        ...options.webPreferences,
        additionalArguments: [
          ...(options.webPreferences?.additionalArguments || []),
          '--breaktwenty-isolated-electron-preload=1',
          '--breaktwenty-isolated-ui-test=1',
        ],
        preload: path.join(__dirname, 'preload.js'),
        partition: `breaktwenty-isolated-${contract.marker.slice(-12)}`,
      },
    });
    journeyWindow = this;
    journeyPhase = 'browser-window-created';
    installSessionNetworkBoundary(this.webContents.session);
    this.webContents.on('console-message', (eventDetails, legacyLevel, legacyMessage) => {
      const details = consoleMessageDetails(eventDetails, legacyLevel, legacyMessage);
      if (details.level === 'error') rendererErrors.push(details.message);
    });
    this.webContents.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
      if (isMainFrame) rendererErrors.push(`did-fail-load ${code} ${description} ${url}`);
    });
    this.webContents.on('render-process-gone', (_event, details) => {
      rendererErrors.push(`render-process-gone ${details?.reason || 'unknown'}`);
    });
    this.webContents.on('did-finish-load', () => {
      journeyPhase = `renderer-loaded:${this.webContents.getURL().slice(0, 120)}`;
      if (journeyStarted || !this.webContents.getURL().startsWith(frontendOrigin)) return;
      journeyStarted = true;
      journeyPhase = 'route-journey-running';
      void runJourney(this).then(
        (receipt) => finish(0, receipt),
        async (error) => finish(1, {
          marker: contract.marker,
          error: error.stack || error.message,
          rendererErrors,
          blockedRequests,
          rendererSnapshot: await collectFailureSnapshot(this),
        }),
      );
    });
  }

  show() {}
  showInactive() {}
}

const electronProxy = {
  ...electron,
  BrowserWindow: IsolatedBrowserWindow,
  dialog: {
    ...electron.dialog,
    showErrorBox: (title, content) => finish(1, {
      marker: contract.marker,
      error: `Native startup error dialog: ${title}: ${content}`,
      phase: journeyPhase,
      rendererErrors,
      blockedRequests,
    }),
  },
  shell: {
    ...electron.shell,
    openExternal: async () => { throw new Error('External navigation is disabled in isolated Electron journeys.'); },
    showItemInFolder: () => { throw new Error('Filesystem reveal is disabled in isolated Electron journeys.'); },
  },
};

const originalLoad = Module._load;
Module._load = function isolatedElectronLoad(request, parent, isMain) {
  if (request === 'electron') return electronProxy;
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
          { ...options, metadata: { breaktwentyDesktopIdentity: testIdentity } },
        ),
        resolvePackagedIdentity: () => testIdentity,
      };
    }
    if (!packagedNative && request === './backendManager') {
      return { ...loaded, BackendManager: IsolatedBackendManager };
    }
    if (request === './visibleAuthBroker') return { ...loaded, VisibleAuthBroker: DisabledVisibleAuthBroker };
  }
  if (
    packagedNative
    && parent?.filename?.endsWith(`${path.sep}backendManager.js`)
    && request === './keyManager'
  ) {
    return { ...loaded, BreakTwentyKeyManager: IsolatedPackagedKeyManager };
  }
  return loaded;
};

process.on('uncaughtException', (error) => finish(1, {
  marker: contract.marker,
  error: error.stack || error.message,
  rendererErrors,
  blockedRequests,
}));
process.on('unhandledRejection', (error) => finish(1, {
  marker: contract.marker,
  error: error?.stack || String(error),
  rendererErrors,
  blockedRequests,
}));

journeyDeadline = setTimeout(() => {
  finish(1, {
    marker: contract.marker,
    error: 'The isolated Electron journey did not reach a terminal result within 150000 ms.',
    phase: journeyPhase,
    rendererErrors,
    blockedRequests,
  });
}, 150000);

require('./main');
