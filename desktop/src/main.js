const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const {
  app,
  BrowserWindow,
  dialog,
  Menu,
  ipcMain,
  screen,
  shell,
} = require('electron');
const { APP_BRAND_NAME } = require('./brand');
const { AppDiagnostics } = require('./appDiagnostics');
const { probeBackendHealth } = require('./backendHealthProbe');
const {
  BackendRecoveryHandoff,
  completeBackendRecoveryHandoff,
} = require('./backendRecoveryHandoff');
const { configureApplicationIdentity, getApplicationIdentity } = require('./appIdentity');
const {
  normalizeLocalBackendApiUrl,
  normalizeLocalFrontendUrl,
} = require('./backendApiUrl');
const {
  createMainWindowIpcAuthorizer,
  registerPrivilegedIpcHandler,
} = require('./ipcAuthorization');
const {
  releaseDesktopStartupLock,
  takeOverDesktopStartupLock,
} = require('./startupLock');
const {
  isSameOriginUrl,
  openExternalHttps,
  openExternalHttpsOrMailto,
} = require('./externalNavigation');

app.setName(APP_BRAND_NAME);
configureApplicationIdentity(app, {
  developmentUserDataDir: process.env.BREAKTWENTY_DEVELOPMENT_USER_DATA_DIR,
});
configureRuntimeTaskbarIdentity();
const ownsSingleInstanceLock = app.requestSingleInstanceLock();
if (!ownsSingleInstanceLock) {
  app.quit();
}
app.enableSandbox();

const log = require('electron-log');
log.transports.file.maxSize = 2 * 1024 * 1024;
const { BreakTwentyAppUpdater } = require('./appUpdater');
const { markUpdateInstallWindowVisible } = require('./updateInstallHelper');
const { BackendManager, normalizeBackendMode } = require('./backendManager');
const { startBrowserRuntimePrewarm } = require('./browserRuntimePrewarm');
const {
  DesktopLifecycleCoordinator,
  closeHttpServer,
  focusExistingWindow,
} = require('./appLifecycle');
const {
  calculateMainWindowActualSizeZoomFactor,
  calculateMainWindowMinimumZoomFactor,
} = require('./mainWindowZoom');
const { VisibleAuthBroker } = require('./visibleAuthBroker');
const {
  LOCAL_DESKTOP_USER_ID,
  materializeOwnedSupportArchive,
} = require('./supportArchiveReveal');

const ELECTRON_DISK_CACHE_MAX_BYTES = 128 * 1024 * 1024;
const ELECTRON_MEDIA_CACHE_MAX_BYTES = 64 * 1024 * 1024;
const ELECTRON_PROFILE_CACHE_BUDGETS = Object.freeze({
  Cache: 160 * 1024 * 1024,
  'Code Cache': 96 * 1024 * 1024,
  GPUCache: 32 * 1024 * 1024,
  DawnCache: 32 * 1024 * 1024,
  ShaderCache: 32 * 1024 * 1024,
  GrShaderCache: 32 * 1024 * 1024,
});

app.commandLine.appendSwitch('disk-cache-size', String(ELECTRON_DISK_CACHE_MAX_BYTES));
app.commandLine.appendSwitch('media-cache-size', String(ELECTRON_MEDIA_CACHE_MAX_BYTES));

const BACKEND_MODE = normalizeBackendMode(
  process.env.BREAKTWENTY_BACKEND_MODE || (app.isPackaged ? 'embedded' : 'docker'),
);
const APP_ROOT = path.resolve(
  process.env.BREAKTWENTY_APP_ROOT || (app.isPackaged ? process.resourcesPath : path.join(__dirname, '..', '..')),
);
const LOADING_SCREEN_CONFIG = JSON.parse(
  fs.readFileSync(path.join(APP_ROOT, 'config', 'loading-screen.json'), 'utf8'),
);
if (typeof LOADING_SCREEN_CONFIG.catchphrase !== 'string' || !LOADING_SCREEN_CONFIG.catchphrase.trim()) {
  throw new Error('Loading-screen config must define a non-empty catchphrase.');
}
const LOADING_SCREEN_STYLES = fs.readFileSync(
  path.join(APP_ROOT, 'config', 'loading-screen.css'),
  'utf8',
);
const LOADING_SCREEN_THEME_STYLES = fs.readFileSync(
  app.isPackaged
    ? path.join(APP_ROOT, 'config', 'loading-screen-theme.css')
    : path.join(APP_ROOT, 'frontend', 'src', 'theme', 'tokens.generated.css'),
  'utf8',
);
const WINDOW_ICON_PATH = resolveWindowIconPath();
const FRONTEND_MODE = normalizeFrontendMode(
  process.env.BREAKTWENTY_DESKTOP_FRONTEND_MODE || (app.isPackaged ? 'build' : 'dev'),
);
const DEV_FRONTEND_URL = normalizeLocalFrontendUrl(
  process.env.BREAKTWENTY_FRONTEND_URL || 'http://localhost:3000',
);
const FRONTEND_BUILD_DIR = path.resolve(
  process.env.BREAKTWENTY_FRONTEND_BUILD_DIR || path.join(APP_ROOT, 'frontend', 'build'),
);
const RAW_BUILD_FRONTEND_HOST = String(
  process.env.BREAKTWENTY_DESKTOP_BUILD_HOST || '127.0.0.1',
).trim();
const BUILD_FRONTEND_HOST = RAW_BUILD_FRONTEND_HOST.replace(/^\[|\]$/g, '');
const HAS_BUILD_FRONTEND_PORT_OVERRIDE = String(
  process.env.BREAKTWENTY_DESKTOP_BUILD_PORT || '',
).trim() !== '';
const USE_PACKAGED_DYNAMIC_FRONTEND_PORT = (
  app.isPackaged
  && BACKEND_MODE === 'embedded'
  && FRONTEND_MODE === 'build'
  && !HAS_BUILD_FRONTEND_PORT_OVERRIDE
);
const BUILD_FRONTEND_PORT = USE_PACKAGED_DYNAMIC_FRONTEND_PORT
  ? 0
  : numericPort(process.env.BREAKTWENTY_DESKTOP_BUILD_PORT, 32100);
let activeBuildFrontendPort = BUILD_FRONTEND_PORT;
const EMBEDDED_BACKEND_PORT = numericPort(process.env.BREAKTWENTY_EMBEDDED_BACKEND_PORT, 8765);
const USE_PACKAGED_DYNAMIC_BACKEND_PORT = (
  app.isPackaged
  && BACKEND_MODE === 'embedded'
  && !String(process.env.BREAKTWENTY_BACKEND_API_URL || '').trim()
  && !String(process.env.BREAKTWENTY_EMBEDDED_BACKEND_PORT || '').trim()
);
const DEFAULT_BACKEND_API_URL = BACKEND_MODE === 'embedded' ? `http://127.0.0.1:${EMBEDDED_BACKEND_PORT}/api` : 'http://localhost:8000/api';
let backendApiUrl = normalizeLocalBackendApiUrl(
  USE_PACKAGED_DYNAMIC_BACKEND_PORT
    ? 'http://127.0.0.1:1/api'
    : process.env.BREAKTWENTY_BACKEND_API_URL || DEFAULT_BACKEND_API_URL,
);
const DESKTOP_AUTH_DIR = path.resolve(
  process.env.BREAKTWENTY_DESKTOP_AUTH_DIR || path.join(APP_ROOT, '.desktop-auth'),
);
let desktopStartupLock = null;
let desktopStartupOwnershipError = null;
const inheritedStartupOwnerPid = Number(
  process.env.BREAKTWENTY_DESKTOP_STARTUP_LOCK_OWNER_PID || 0,
);
const inheritedStartupNonce = String(
  process.env.BREAKTWENTY_DESKTOP_STARTUP_LOCK_NONCE || '',
);
if (inheritedStartupOwnerPid > 0 && inheritedStartupNonce) {
  try {
    desktopStartupLock = takeOverDesktopStartupLock(DESKTOP_AUTH_DIR, {
      ownerPid: inheritedStartupOwnerPid,
      nonce: inheritedStartupNonce,
    });
  } catch (error) {
    desktopStartupOwnershipError = error;
  }
} else if (BACKEND_MODE === 'docker') {
  desktopStartupOwnershipError = new Error(
    `${APP_BRAND_NAME} Docker desktop mode must be started through the private development launcher.`,
  );
}
process.once('exit', () => {
  if (desktopStartupLock) releaseDesktopStartupLock(desktopStartupLock);
});
const FRONTEND_READY_TIMEOUT_MS = Number(process.env.BREAKTWENTY_FRONTEND_READY_TIMEOUT_MS || 120000);
const FRONTEND_READY_POLL_MS = Number(process.env.BREAKTWENTY_FRONTEND_READY_POLL_MS || 1000);
const BACKEND_READY_TIMEOUT_MS = Number(process.env.BREAKTWENTY_BACKEND_READY_TIMEOUT_MS || 90000);
const BACKEND_READY_POLL_MS = Number(process.env.BREAKTWENTY_BACKEND_READY_POLL_MS || 1000);
const BACKEND_HEALTH_PROBE_TIMEOUT_MS = Number(process.env.BREAKTWENTY_BACKEND_HEALTH_PROBE_TIMEOUT_MS || 4000);
const BACKEND_HEALTH_SUPERVISOR_INTERVAL_MS = Number(process.env.BREAKTWENTY_BACKEND_HEALTH_SUPERVISOR_INTERVAL_MS || 10000);
const BACKEND_HEALTH_FAILURE_THRESHOLD = 3;
const BACKEND_RECOVERY_WINDOW_MS = 30 * 60 * 1000;
const BACKEND_RECOVERY_MAX_ATTEMPTS = 2;
const BACKEND_PROBE_HISTORY_LIMIT = 12;
const BACKEND_RECOVERY_SOFT_TIMEOUT_MS = Number(process.env.BREAKTWENTY_BACKEND_RECOVERY_SOFT_TIMEOUT_MS || 20000);
const BACKEND_RECOVERY_FALLBACK_TIMEOUT_MS = Number(process.env.BREAKTWENTY_BACKEND_RECOVERY_FALLBACK_TIMEOUT_MS || 30000);
const CONTENT_TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.gif': 'image/gif',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.txt': 'text/plain; charset=utf-8',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
};

function resolveWindowIconPath() {
  const rasterIconNames = process.platform === 'linux'
    ? ['window-icon-64.png']
    : ['window-icon-512.png', 'window-icon-64.png', 'window-icon-48.png', 'window-icon-32.png'];
  const candidates = [
    ...(process.platform === 'win32' ? [
      path.join(APP_ROOT, 'desktop', 'assets', 'icons', 'window-icon.ico'),
      path.join(process.resourcesPath || '', 'desktop', 'assets', 'icons', 'window-icon.ico'),
      path.join(__dirname, '..', 'assets', 'icons', 'window-icon.ico'),
    ] : []),
    ...rasterIconNames.flatMap((iconName) => [
      path.join(APP_ROOT, 'desktop', 'assets', 'icons', iconName),
      path.join(process.resourcesPath || '', 'desktop', 'assets', 'icons', iconName),
      path.join(__dirname, '..', 'assets', 'icons', iconName),
    ]),
  ];
  return candidates.find((candidate) => fs.existsSync(candidate)) || null;
}

function desktopIconWindowOptions() {
  return WINDOW_ICON_PATH ? { icon: WINDOW_ICON_PATH } : {};
}

function runtimeTaskbarAppId() {
  return `${getApplicationIdentity().technicalId}.window`;
}

function configureRuntimeTaskbarIdentity() {
  if (process.platform === 'linux' && typeof app.setDesktopName === 'function') {
    app.setDesktopName(runtimeTaskbarAppId());
  }
}

function applyDesktopWindowIcon(window) {
  if (!WINDOW_ICON_PATH) return;
  if (typeof window.setIcon === 'function') {
    window.setIcon(WINDOW_ICON_PATH);
  }
  if (process.platform === 'win32' && typeof window.setAppDetails === 'function') {
    window.setAppDetails({
      appId: runtimeTaskbarAppId(),
      appIconPath: WINDOW_ICON_PATH,
      relaunchCommand: process.execPath,
      relaunchDisplayName: APP_BRAND_NAME,
    });
  }
}

const MAIN_WINDOW_SIZE = {
  widthRatio: 0.92,
  heightRatio: 0.9,
  startMaximized: true,
  minWidth: 1180,
  minHeight: 720,
  maxWidth: 1800,
  maxHeight: 1100,
  fallbackWidth: 1280,
  fallbackHeight: 860,
};
const DESKTOP_PREFERENCES_FILE = 'desktop-preferences.json';
const MIN_MAIN_WINDOW_ZOOM_FACTOR = 0.5;
const MAX_MAIN_WINDOW_ZOOM_FACTOR = 3;
const MAIN_WINDOW_ZOOM_SOURCE_ACTUAL_SIZE = 'actual-size';
const MAIN_WINDOW_ZOOM_SOURCE_USER = 'user';

let mainWindow = null;
let backendManager = null;
let appDiagnostics = null;
let visibleAuthBroker = null;
let appUpdater = null;
let frontendUrl = FRONTEND_MODE === 'build' ? buildFrontendUrl() : DEV_FRONTEND_URL;
let frontendBuildServer = null;
let browserRuntimePrewarm = null;
let mainWindowZoomPreferenceWasStored = false;
let mainWindowZoomUserChanged = false;
let suppressNextMainWindowZoomSave = false;
let mainWindowZoomResetPreviewActive = false;
let mainWindowDisplayZoomTimer = null;
let desktopRuntimeInitialized = false;
let secondInstanceFocusPending = false;
let quitResumeScheduled = false;
let backendHealthSupervisorTimer = null;
let backendHealthSupervisorRunning = false;
let backendHealthFailureCount = 0;
let backendRecoveryPromise = null;
let backendProbeHistory = [];
let backendRecoveryAttempts = [];
let backendRecoveryRateLimitedAt = 0;
let lastBackendRecovery = null;
let startupUpdateRecoveryPromise = null;
let startupUpdateOfferedVersion = '';
const backendRecoveryHandoff = new BackendRecoveryHandoff({
  softTimeoutMs: BACKEND_RECOVERY_SOFT_TIMEOUT_MS,
  fallbackTimeoutMs: BACKEND_RECOVERY_FALLBACK_TIMEOUT_MS,
});

function normalizeFrontendMode(value) {
  return String(value || 'dev').toLowerCase() === 'build' ? 'build' : 'dev';
}

function numericPort(value, fallback) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function normalizeZoomFactor(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return null;
  }
  return clamp(parsed, MIN_MAIN_WINDOW_ZOOM_FACTOR, MAX_MAIN_WINDOW_ZOOM_FACTOR);
}

function usableDisplayDimension(value, fallback) {
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function desktopPreferencesPath() {
  return path.join(app.getPath('userData'), DESKTOP_PREFERENCES_FILE);
}

function readDesktopPreferences() {
  try {
    const rawPreferences = fs.readFileSync(desktopPreferencesPath(), 'utf8');
    const preferences = JSON.parse(rawPreferences);
    return preferences && typeof preferences === 'object' && !Array.isArray(preferences) ? preferences : {};
  } catch (_error) {
    return {};
  }
}

function writeDesktopPreferences(preferences) {
  try {
    fs.mkdirSync(path.dirname(desktopPreferencesPath()), { recursive: true });
    fs.writeFileSync(desktopPreferencesPath(), `${JSON.stringify(preferences, null, 2)}\n`);
  } catch (_error) {
  }
}

function directorySizeExceeds(dirPath, maxBytes) {
  let total = 0;
  const pending = [dirPath];
  while (pending.length > 0) {
    const current = pending.pop();
    let entries = [];
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (_error) {
      continue;
    }
    for (const entry of entries) {
      const entryPath = path.join(current, entry.name);
      try {
        if (entry.isDirectory()) {
          pending.push(entryPath);
          continue;
        }
        const stat = fs.statSync(entryPath);
        total += stat.size;
        if (total > maxBytes) {
          return true;
        }
      } catch (_error) {
      }
    }
  }
  return false;
}

function pruneElectronProfileCaches(userDataDir) {
  for (const [relativePath, maxBytes] of Object.entries(ELECTRON_PROFILE_CACHE_BUDGETS)) {
    const cachePath = path.join(userDataDir, relativePath);
    if (!fs.existsSync(cachePath)) {
      continue;
    }
    try {
      if (directorySizeExceeds(cachePath, maxBytes)) {
        fs.rmSync(cachePath, { recursive: true, force: true });
      }
    } catch (_error) {
    }
  }
}

function getMainWindowDisplay(windowBounds = null) {
  const hasDisplayCoordinates = Number.isFinite(windowBounds?.x) && Number.isFinite(windowBounds?.y);
  return hasDisplayCoordinates
    ? screen.getDisplayMatching(windowBounds)
    : screen.getPrimaryDisplay();
}

function getMinimumMainWindowZoomFactor(windowBounds = null) {
  const display = getMainWindowDisplay(windowBounds);
  return calculateMainWindowMinimumZoomFactor({
    displayScaleFactor: display.scaleFactor,
    minimumZoomFactor: MIN_MAIN_WINDOW_ZOOM_FACTOR,
  });
}

function clampMainWindowZoomFactorToDisplay(zoomFactor, windowBounds = null) {
  return clamp(
    zoomFactor,
    getMinimumMainWindowZoomFactor(windowBounds),
    MAX_MAIN_WINDOW_ZOOM_FACTOR,
  );
}

function getResolutionAwareDefaultMainWindowZoomFactor(windowBounds = null) {
  const display = getMainWindowDisplay(windowBounds);
  const availableWidth = usableDisplayDimension(
    display.workAreaSize.width,
    MAIN_WINDOW_SIZE.fallbackWidth,
  );
  return calculateMainWindowActualSizeZoomFactor({
    availableWidth,
    displayScaleFactor: display.scaleFactor,
    minimumZoomFactor: MIN_MAIN_WINDOW_ZOOM_FACTOR,
    maximumZoomFactor: MAX_MAIN_WINDOW_ZOOM_FACTOR,
  });
}

function getStoredMainWindowZoomFactor() {
  const preferences = readDesktopPreferences();
  const zoomFactor = normalizeZoomFactor(preferences.mainWindowZoomFactor);
  return preferences.mainWindowZoomFactorSource === MAIN_WINDOW_ZOOM_SOURCE_USER
    ? zoomFactor
    : null;
}

function getMainWindowZoomFactor(windowBounds = null) {
  const zoomFactor = getStoredMainWindowZoomFactor()
    || getResolutionAwareDefaultMainWindowZoomFactor(windowBounds);
  return clampMainWindowZoomFactorToDisplay(zoomFactor, windowBounds);
}

function saveMainWindowZoomFactor(zoomFactor, source = MAIN_WINDOW_ZOOM_SOURCE_USER) {
  const normalizedZoomFactor = normalizeZoomFactor(zoomFactor);
  if (!normalizedZoomFactor) {
    return;
  }
  const targetWindow = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
  const displaySafeZoomFactor = clampMainWindowZoomFactorToDisplay(
    normalizedZoomFactor,
    targetWindow ? targetWindow.getBounds() : null,
  );
  writeDesktopPreferences({
    ...readDesktopPreferences(),
    mainWindowZoomFactor: displaySafeZoomFactor,
    mainWindowZoomFactorSource: source,
  });
}

function isFrontendUrl(url) {
  return isSameOriginUrl(url, frontendUrl);
}

function openApprovedExternalUrl(url, source, { allowMailto = false } = {}) {
  const didOpen = allowMailto
    ? openExternalHttpsOrMailto(shell, url)
    : openExternalHttps(shell, url);
  if (didOpen) {
    return true;
  }
  // Never log a rejected URL: the rejection reasons include embedded
  // credentials, and query strings can carry equally sensitive material.
  log.warn(`[navigation] blocked unsafe ${source}`);
  return false;
}

function getMainWindowWebContents() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return null;
  }
  return mainWindow.webContents;
}

function cleanLogValue(value, maxLength = 1600) {
  const raw = String(value ?? '');
  if (raw.length <= maxLength) {
    return raw;
  }
  return `${raw.slice(0, maxLength)}...`;
}

function formatConsoleMessage(details = {}) {
  const parts = [
    `level=${details.level}`,
    cleanLogValue(details.message),
  ];
  if (details.sourceId) {
    parts.push(`source=${cleanLogValue(details.sourceId, 500)}`);
  }
  if (details.lineNumber) {
    parts.push(`line=${details.lineNumber}`);
  }
  return parts.filter(Boolean).join(' ');
}

function normalizeConsoleMessageDetails(eventDetails = {}, legacyLevel, legacyMessage, legacyLineNumber, legacySourceId) {
  const legacyLevelNames = ['verbose', 'info', 'warning', 'error'];
  const level = eventDetails.level !== undefined
    ? eventDetails.level
    : legacyLevelNames[legacyLevel] || legacyLevel || 'unknown';
  return {
    level,
    message: eventDetails.message !== undefined ? eventDetails.message : legacyMessage,
    lineNumber: eventDetails.lineNumber !== undefined ? eventDetails.lineNumber : legacyLineNumber,
    sourceId: eventDetails.sourceId !== undefined ? eventDetails.sourceId : legacySourceId,
  };
}

function saveCurrentMainWindowZoomFactor(webContents, options = {}) {
  if (suppressNextMainWindowZoomSave) {
    return;
  }
  if (mainWindowZoomResetPreviewActive) {
    return;
  }
  const targetWebContents = webContents || getMainWindowWebContents();
  if (!targetWebContents || targetWebContents.isDestroyed()) {
    return;
  }
  if (options.requireFrontendUrl && !isFrontendUrl(targetWebContents.getURL())) {
    return;
  }
  if (
    options.requireUserZoomPreference
    && !mainWindowZoomPreferenceWasStored
    && !mainWindowZoomUserChanged
  ) {
    return;
  }
  saveMainWindowZoomFactor(targetWebContents.getZoomFactor());
  mainWindowZoomPreferenceWasStored = true;
}

function resetMainWindowZoomToActualSize() {
  const targetWebContents = getMainWindowWebContents();
  if (!targetWebContents || targetWebContents.isDestroyed()) {
    return;
  }
  const defaultZoomFactor = getResolutionAwareDefaultMainWindowZoomFactor(
    mainWindow && !mainWindow.isDestroyed() ? mainWindow.getBounds() : null,
  );
  suppressNextMainWindowZoomSave = true;
  mainWindowZoomResetPreviewActive = false;
  targetWebContents.setZoomFactor(defaultZoomFactor);
  notifyMainWindowZoomChanged();
  saveMainWindowZoomFactor(defaultZoomFactor, MAIN_WINDOW_ZOOM_SOURCE_ACTUAL_SIZE);
  mainWindowZoomPreferenceWasStored = false;
  mainWindowZoomUserChanged = false;
  setImmediate(() => {
    suppressNextMainWindowZoomSave = false;
  });
}

function applyAdaptiveMainWindowZoomForCurrentDisplay() {
  const targetWindow = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
  const targetWebContents = targetWindow ? targetWindow.webContents : null;
  if (
    !targetWebContents
    || targetWebContents.isDestroyed()
  ) {
    return;
  }
  const displaySafeZoomFactor = clampMainWindowZoomFactorToDisplay(
    targetWebContents.getZoomFactor(),
    targetWindow.getBounds(),
  );
  if (Math.abs(targetWebContents.getZoomFactor() - displaySafeZoomFactor) >= 0.001) {
    suppressNextMainWindowZoomSave = true;
    targetWebContents.setZoomFactor(displaySafeZoomFactor);
    notifyMainWindowZoomChanged();
    const preferences = readDesktopPreferences();
    const source = preferences.mainWindowZoomFactorSource === MAIN_WINDOW_ZOOM_SOURCE_USER
      ? MAIN_WINDOW_ZOOM_SOURCE_USER
      : MAIN_WINDOW_ZOOM_SOURCE_ACTUAL_SIZE;
    saveMainWindowZoomFactor(displaySafeZoomFactor, source);
    setImmediate(() => {
      suppressNextMainWindowZoomSave = false;
    });
    return;
  }
  if (mainWindowZoomPreferenceWasStored || mainWindowZoomUserChanged) {
    return;
  }
  const zoomFactor = getResolutionAwareDefaultMainWindowZoomFactor(targetWindow.getBounds());
  if (Math.abs(targetWebContents.getZoomFactor() - zoomFactor) < 0.001) {
    return;
  }
  suppressNextMainWindowZoomSave = true;
  targetWebContents.setZoomFactor(zoomFactor);
  notifyMainWindowZoomChanged();
  if (readDesktopPreferences().mainWindowZoomFactorSource === MAIN_WINDOW_ZOOM_SOURCE_ACTUAL_SIZE) {
    saveMainWindowZoomFactor(zoomFactor, MAIN_WINDOW_ZOOM_SOURCE_ACTUAL_SIZE);
  }
  setImmediate(() => {
    suppressNextMainWindowZoomSave = false;
  });
}

function scheduleAdaptiveMainWindowZoomForCurrentDisplay() {
  if (mainWindowDisplayZoomTimer) {
    clearTimeout(mainWindowDisplayZoomTimer);
  }
  mainWindowDisplayZoomTimer = setTimeout(() => {
    mainWindowDisplayZoomTimer = null;
    applyAdaptiveMainWindowZoomForCurrentDisplay();
  }, 150);
}

function getMainWindowZoomStatus() {
  const targetWindow = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
  const targetWebContents = targetWindow ? targetWindow.webContents : null;
  const windowBounds = targetWindow ? targetWindow.getBounds() : null;
  const display = getMainWindowDisplay(windowBounds);
  const preferences = readDesktopPreferences();
  const storedZoomFactor = normalizeZoomFactor(preferences.mainWindowZoomFactor);
  const defaultZoomFactor = getResolutionAwareDefaultMainWindowZoomFactor(windowBounds);
  return {
    currentZoomFactor: targetWebContents && !targetWebContents.isDestroyed()
      ? targetWebContents.getZoomFactor()
      : null,
    defaultZoomFactor,
    minimumZoomFactor: getMinimumMainWindowZoomFactor(windowBounds),
    effectiveStartupZoomFactor: getMainWindowZoomFactor(windowBounds),
    storedZoomFactor,
    storedZoomFactorSource: preferences.mainWindowZoomFactorSource || null,
    hasStoredUserZoom: Boolean(storedZoomFactor && preferences.mainWindowZoomFactorSource === MAIN_WINDOW_ZOOM_SOURCE_USER),
    usesAdaptiveActualSize: preferences.mainWindowZoomFactorSource !== MAIN_WINDOW_ZOOM_SOURCE_USER,
    resetPreviewActive: mainWindowZoomResetPreviewActive,
    windowBounds,
    display: {
      scaleFactor: display.scaleFactor,
      workAreaSize: display.workAreaSize,
    },
  };
}

function notifyMainWindowZoomChanged() {
  const targetWebContents = getMainWindowWebContents();
  if (!targetWebContents || targetWebContents.isDestroyed()) {
    return;
  }
  targetWebContents.send('breaktwenty:main-window-zoom-changed', getMainWindowZoomStatus());
}

function installApplicationMenu() {
  const template = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    { role: 'fileMenu' },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        {
          label: 'Reload',
          accelerator: 'CmdOrCtrl+R',
          click: () => { void reloadMainWindowThroughBackendReadiness({ ignoreCache: false }); },
        },
        {
          label: 'Force Reload',
          accelerator: 'CmdOrCtrl+Shift+R',
          click: () => { void reloadMainWindowThroughBackendReadiness({ ignoreCache: true }); },
        },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        {
          label: 'Actual Size',
          accelerator: process.platform === 'darwin' ? 'Command+0' : 'Ctrl+0',
          click: resetMainWindowZoomToActualSize,
        },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
    {
      role: 'help',
      submenu: [
        {
          label: 'Check for Updates…',
          enabled: app.isPackaged,
          click: () => { void runStartupUpdateRecovery({ check: true }); },
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function mainWindowIsOnStartupSurface() {
  const window = mainWindow;
  if (!window || window.isDestroyed()) return false;
  return !isFrontendUrl(window.webContents.getURL());
}

function showMainWindowMessageBox(options) {
  const window = mainWindow;
  return window && !window.isDestroyed()
    ? dialog.showMessageBox(window, options)
    : dialog.showMessageBox(options);
}

async function runStartupUpdateRecovery({ check = false, automatic = false } = {}) {
  if (!appUpdater || lifecycleCoordinator.isShuttingDown()) return null;
  if (startupUpdateRecoveryPromise) return startupUpdateRecoveryPromise;
  startupUpdateRecoveryPromise = (async () => {
    let status = appUpdater.getStatus();
    if (check && !['available', 'downloaded', 'downloading', 'installing'].includes(status.status)) {
      status = await appUpdater.checkForUpdates();
    }
    if (status.status === 'checking') {
      return status;
    }
    if (status.status === 'downloading') {
      if (!automatic) {
        await showMainWindowMessageBox({
          type: 'info',
          title: `${APP_BRAND_NAME} Update`,
          message: 'The update is already downloading.',
          detail: `${APP_BRAND_NAME} will ask to restart when the download is ready.`,
          buttons: ['OK'],
        });
      }
      return status;
    }
    if (status.status === 'available') {
      const version = status.updateInfo?.version || '';
      if (automatic && version && startupUpdateOfferedVersion === version) return status;
      if (automatic && version) startupUpdateOfferedVersion = version;
      const prompt = await showMainWindowMessageBox({
        type: 'info',
        title: `${APP_BRAND_NAME} Update Available`,
        message: version
          ? `${APP_BRAND_NAME} ${version} is available.`
          : `A ${APP_BRAND_NAME} update is available.`,
        detail: `The update can be downloaded even while ${APP_BRAND_NAME} is still starting. You will be asked before it restarts.`,
        buttons: ['Download Update', 'Not Now'],
        defaultId: 0,
        cancelId: 1,
      });
      if (prompt.response !== 0) return status;
      status = await appUpdater.downloadUpdate();
    }
    if (status.status === 'downloaded') {
      const version = status.updateInfo?.version || '';
      const prompt = await showMainWindowMessageBox({
        type: 'info',
        title: `${APP_BRAND_NAME} Update Ready`,
        message: version
          ? `${APP_BRAND_NAME} ${version} is ready to install.`
          : `The ${APP_BRAND_NAME} update is ready to install.`,
        detail: `${APP_BRAND_NAME} will close, install the update, and reopen automatically.`,
        buttons: ['Restart and Install', 'Later'],
        defaultId: 0,
        cancelId: 1,
      });
      if (prompt.response === 0) await appUpdater.installUpdate();
      return status;
    }
    if (!automatic) {
      await showMainWindowMessageBox({
        type: status.status === 'error' ? 'error' : 'info',
        title: `${APP_BRAND_NAME} Update`,
        message: status.message || 'No update is currently available.',
        buttons: ['OK'],
      });
    }
    return status;
  })().finally(() => {
    startupUpdateRecoveryPromise = null;
  });
  return startupUpdateRecoveryPromise;
}

function handleDesktopUpdaterStatusChange(status) {
  const window = mainWindow;
  if (!window || window.isDestroyed()) return;
  if (status?.status === 'downloading') {
    const percent = Number(status.progress?.percent || 0);
    window.setProgressBar(percent > 0 ? Math.min(percent / 100, 1) : 2);
  } else {
    window.setProgressBar(-1);
  }
  if (
    mainWindowIsOnStartupSurface()
    && ['available', 'downloaded'].includes(status?.status)
  ) {
    void runStartupUpdateRecovery({ automatic: true });
  }
}

function getMainWindowBounds() {
  const { workAreaSize } = screen.getPrimaryDisplay();
  const availableWidth = usableDisplayDimension(workAreaSize.width, MAIN_WINDOW_SIZE.fallbackWidth);
  const availableHeight = usableDisplayDimension(workAreaSize.height, MAIN_WINDOW_SIZE.fallbackHeight);
  const minWidth = Math.min(MAIN_WINDOW_SIZE.minWidth, availableWidth);
  const minHeight = Math.min(MAIN_WINDOW_SIZE.minHeight, availableHeight);
  const preferredWidth = Math.min(
    Math.floor(availableWidth * MAIN_WINDOW_SIZE.widthRatio),
    MAIN_WINDOW_SIZE.maxWidth,
  );
  const preferredHeight = Math.min(
    Math.floor(availableHeight * MAIN_WINDOW_SIZE.heightRatio),
    MAIN_WINDOW_SIZE.maxHeight,
  );

  return {
    width: clamp(preferredWidth, minWidth, availableWidth),
    height: clamp(preferredHeight, minHeight, availableHeight),
    minWidth,
    minHeight,
  };
}

function apiUrl(pathname) {
  return new URL(pathname, backendApiUrl).toString();
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function recordBackendProbe(health, source = 'supervisor') {
  backendProbeHistory = [
    ...backendProbeHistory,
    {
      at: new Date().toISOString(),
      source,
      ok: Boolean(health?.ok),
      status: Number(health?.status || 0) || null,
      phase: String(health?.phase || '') || null,
      elapsedMs: Number.isFinite(health?.elapsedMs) ? health.elapsedMs : null,
      timings: health?.timings || null,
      message: health?.ok ? null : String(health?.message || 'Backend did not report healthy.'),
    },
  ].slice(-BACKEND_PROBE_HISTORY_LIMIT);
}

function captureApplicationIncident(payload) {
  if (!appDiagnostics) return null;
  try {
    return appDiagnostics.capture(payload);
  } catch (error) {
    log.error(`Application diagnostic capture failed: ${error.message}`);
    return null;
  }
}

async function getBackendHealth({ timeoutMs = BACKEND_HEALTH_PROBE_TIMEOUT_MS } = {}) {
  return probeBackendHealth({
    url: apiUrl('/api/health'),
    authorization: `Bearer ${backendManager.getDesktopLaunchAuthToken()}`,
    ownershipToken: BACKEND_MODE === 'embedded'
      ? backendManager.getDesktopLaunchAuthToken()
      : '',
    expectedAppName: APP_BRAND_NAME,
    timeoutMs: Math.max(250, Number(timeoutMs) || BACKEND_HEALTH_PROBE_TIMEOUT_MS),
  });
}

function finalizeRecoveryIncident(incident, { outcome, recovery, rendererCompletion }) {
  if (!incident) return null;
  const completion = String(rendererCompletion?.completion || 'bounded_timeout');
  const finalMarker = `[backend-recovery] final recovery marker incident=${incident.incidentId} `
    + `outcome=${outcome} renderer=${completion}`;
  log.info(finalMarker);
  appDiagnostics.refreshLogs?.(incident.incidentId, { electronMarker: finalMarker });
  return appDiagnostics.finalize(incident.incidentId, {
    outcome,
    recovery: {
      ...recovery,
      renderer: rendererCompletion,
    },
  });
}

function recentBackendRecoveryAttempts() {
  const cutoff = Date.now() - BACKEND_RECOVERY_WINDOW_MS;
  backendRecoveryAttempts = backendRecoveryAttempts.filter((timestamp) => timestamp >= cutoff);
  return backendRecoveryAttempts;
}

async function recoverEmbeddedBackend({ trigger, failedHealth = null } = {}) {
  if (backendRecoveryPromise) return backendRecoveryPromise;
  if (BACKEND_MODE !== 'embedded' || lifecycleCoordinator.isShuttingDown()) {
    return failedHealth || { ok: false, message: 'The external backend must be recovered outside the desktop app.' };
  }
  backendRecoveryPromise = (async () => {
    if (recentBackendRecoveryAttempts().length >= BACKEND_RECOVERY_MAX_ATTEMPTS) {
      const now = Date.now();
      const shouldCapture = now - backendRecoveryRateLimitedAt >= 5 * 60 * 1000;
      backendRecoveryRateLimitedAt = shouldCapture ? now : backendRecoveryRateLimitedAt;
      const incident = shouldCapture
        ? captureApplicationIncident({
            trigger: 'backend_recovery_rate_limited',
            summary: 'The embedded backend stayed unavailable after the automatic recovery limit was reached.',
            backend: backendManager.describe(),
            probes: backendProbeHistory,
            details: { requestedBy: trigger, failedHealth },
          })
        : null;
      if (incident) {
        await sleep(BACKEND_RECOVERY_SOFT_TIMEOUT_MS);
        finalizeRecoveryIncident(incident, {
          outcome: 'recovery_rate_limited',
          rendererCompletion: { ok: false, completion: 'bounded_timeout' },
          recovery: {
            attempted: false,
            limit: BACKEND_RECOVERY_MAX_ATTEMPTS,
            windowMinutes: BACKEND_RECOVERY_WINDOW_MS / (60 * 1000),
          },
        });
      }
      lastBackendRecovery = {
        status: 'rate_limited',
        trigger: String(trigger || 'backend_unresponsive'),
        completedAt: new Date().toISOString(),
        incidentId: incident?.incidentId || null,
      };
      return {
        ok: false,
        message: 'Automatic backend recovery paused to prevent a restart loop. Export Application diagnostics for support.',
      };
    }

    backendRecoveryAttempts.push(Date.now());
    const startedAtMs = Date.now();
    const stackDumps = await backendManager.requestStackDumps({
      sampleCount: 3,
      intervalMs: 250,
    });
    const incident = captureApplicationIncident({
      trigger: String(trigger || 'backend_unresponsive'),
      summary: 'The owned embedded backend stopped answering bounded health probes.',
      backend: backendManager.describe(),
      probes: backendProbeHistory,
      details: { failedHealth, stackDumps },
    });
    if (incident) {
      appDiagnostics.refreshLogs?.(incident.incidentId, { suffix: '.pre-recovery' });
    }
    const startedAt = new Date().toISOString();
    const previousBackendApiUrl = backendApiUrl;
    log.warn(`Controlled embedded backend recovery started trigger=${trigger || 'backend_unresponsive'}`);
    try {
      await backendManager.restart();
      synchronizeBackendEndpoint();
      const health = await waitForBackend({ timeoutMs: BACKEND_READY_TIMEOUT_MS, source: 'recovery' });
      const completedAt = new Date().toISOString();
      const recovered = Boolean(health.ok);
      lastBackendRecovery = {
        status: recovered ? 'recovered' : 'failed',
        trigger: String(trigger || 'backend_unresponsive'),
        startedAt,
        completedAt,
        incidentId: incident?.incidentId || null,
      };
      if (recovered) {
        backendHealthFailureCount = 0;
        log.info('Controlled embedded backend recovery completed successfully.');
        const finalizeRecovered = (completion) => finalizeRecoveryIncident(incident, {
          outcome: 'recovered',
          rendererCompletion: completion,
          recovery: {
            attempted: true,
            recovered: true,
            startedAt,
            completedAt,
            finalHealth: health,
          },
        });
        let rendererCompletion;
        if (backendApiUrl !== previousBackendApiUrl) {
          const webContents = mainWindow?.webContents;
          rendererCompletion = webContents && !webContents.isDestroyed()
            ? await backendRecoveryHandoff.waitForDidFinishLoad(webContents)
            : {
                ok: false,
                completion: 'renderer_unavailable',
                message: 'The renderer was unavailable for backend recovery.',
              };
          finalizeRecovered(rendererCompletion);
        } else {
          rendererCompletion = await completeBackendRecoveryHandoff({
            handoff: backendRecoveryHandoff,
            webContents: mainWindow?.webContents,
            recovery: {
              recoveryId: randomUUID(),
              backendRecoveredAt: completedAt,
            },
            finalize: finalizeRecovered,
          });
        }
        lastBackendRecovery = {
          ...lastBackendRecovery,
          rendererCompletion,
        };
        return health;
      }
      log.error('Controlled embedded backend recovery completed without a healthy backend.');
      const remainingFinalizationMs = Math.max(
        0,
        BACKEND_RECOVERY_SOFT_TIMEOUT_MS - (Date.now() - startedAtMs),
      );
      if (remainingFinalizationMs > 0) await sleep(remainingFinalizationMs);
      finalizeRecoveryIncident(incident, {
        outcome: 'recovery_failed',
        rendererCompletion: { ok: false, completion: 'bounded_timeout' },
        recovery: {
          attempted: true,
          recovered: false,
          startedAt,
          completedAt,
          finalHealth: health,
        },
      });
      return health;
    } catch (error) {
      const completedAt = new Date().toISOString();
      lastBackendRecovery = {
        status: 'failed',
        trigger: String(trigger || 'backend_unresponsive'),
        startedAt,
        completedAt,
        incidentId: incident?.incidentId || null,
      };
      log.error(`Controlled embedded backend recovery failed: ${error.message}`);
      const remainingFinalizationMs = Math.max(
        0,
        BACKEND_RECOVERY_SOFT_TIMEOUT_MS - (Date.now() - startedAtMs),
      );
      if (remainingFinalizationMs > 0) await sleep(remainingFinalizationMs);
      finalizeRecoveryIncident(incident, {
        outcome: 'recovery_failed',
        rendererCompletion: { ok: false, completion: 'bounded_timeout' },
        recovery: {
          attempted: true,
          recovered: false,
          startedAt,
          completedAt,
          error: error.message,
        },
      });
      return { ok: false, message: error.message || 'Embedded backend recovery failed.' };
    }
  })().finally(() => {
    backendRecoveryPromise = null;
  });
  return backendRecoveryPromise;
}

async function getFrontendHealth() {
  try {
    const response = await fetch(frontendUrl);
    return {
      ok: response.ok,
      status: response.status,
    };
  } catch (error) {
    return {
      ok: false,
      message: error.message,
    };
  }
}

async function waitForBackend({ timeoutMs = BACKEND_READY_TIMEOUT_MS, source = 'readiness' } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastHealth = null;

  while (!lifecycleCoordinator.isShuttingDown() && Date.now() < deadline) {
    lastHealth = await getBackendHealth();
    recordBackendProbe(lastHealth, source);
    if (lastHealth.ok) {
      return lastHealth;
    }
    await sleep(BACKEND_READY_POLL_MS);
  }

  return lastHealth || {
    ok: false,
    message: `Timed out waiting for the ${APP_BRAND_NAME} backend.`,
  };
}

async function restoreBackendReadinessForRunnerGrant() {
  let health = await getBackendHealth();
  recordBackendProbe(health, 'visible_auth_runner_grant');
  if (health.ok) return health;
  if (BACKEND_MODE !== 'embedded') return health;
  health = await recoverEmbeddedBackend({
    trigger: 'visible_auth_runner_grant_unavailable',
    failedHealth: health,
  });
  return health;
}

async function reloadMainWindowThroughBackendReadiness({ ignoreCache = false } = {}) {
  const window = mainWindow;
  if (!window || window.isDestroyed() || lifecycleCoordinator.isShuttingDown()) return;
  let health = await getBackendHealth();
  recordBackendProbe(health, ignoreCache ? 'force_reload' : 'reload');
  if (!health.ok && BACKEND_MODE === 'embedded') {
    health = await recoverEmbeddedBackend({
      trigger: ignoreCache ? 'force_reload_backend_unavailable' : 'reload_backend_unavailable',
      failedHealth: health,
    });
  }
  if (window.isDestroyed() || lifecycleCoordinator.isShuttingDown()) return;
  if (!health.ok) {
    await window.loadURL(loadingDataUrl(
      `${APP_BRAND_NAME} backend is not ready`,
      health.message || `Last backend status: ${health.status || 'unavailable'}`,
    ));
    return;
  }
  if (ignoreCache) window.webContents.reloadIgnoringCache();
  else window.webContents.reload();
}

async function runBackendHealthSupervisorProbe() {
  if (
    backendHealthSupervisorRunning
    || lifecycleCoordinator.isShuttingDown()
    || BACKEND_MODE !== 'embedded'
  ) {
    return;
  }
  backendHealthSupervisorRunning = true;
  try {
    const health = await getBackendHealth();
    recordBackendProbe(health, 'supervisor');
    if (health.ok) {
      backendHealthFailureCount = 0;
      return;
    }
    backendHealthFailureCount += 1;
    if (backendHealthFailureCount < BACKEND_HEALTH_FAILURE_THRESHOLD) return;
    backendHealthFailureCount = 0;
    await recoverEmbeddedBackend({
      trigger: 'health_supervisor_unresponsive',
      failedHealth: health,
    });
  } finally {
    backendHealthSupervisorRunning = false;
  }
}

function startBackendHealthSupervisor() {
  if (BACKEND_MODE !== 'embedded' || backendHealthSupervisorTimer) return;
  backendHealthSupervisorTimer = setInterval(() => {
    void runBackendHealthSupervisorProbe();
  }, Math.max(1000, BACKEND_HEALTH_SUPERVISOR_INTERVAL_MS));
}

function stopBackendHealthSupervisor() {
  if (backendHealthSupervisorTimer) clearInterval(backendHealthSupervisorTimer);
  backendHealthSupervisorTimer = null;
}

function buildFrontendUrl() {
  const host = BUILD_FRONTEND_HOST.includes(':')
    ? `[${BUILD_FRONTEND_HOST}]`
    : BUILD_FRONTEND_HOST;
  return normalizeLocalFrontendUrl(`http://${host}:${activeBuildFrontendPort || 1}`);
}

function frontendBuildIndexPath() {
  return path.join(FRONTEND_BUILD_DIR, 'index.html');
}

function resolveBuildRequestPath(requestUrl) {
  let pathname = '/';
  try {
    pathname = decodeURIComponent(new URL(requestUrl, buildFrontendUrl()).pathname);
  } catch (_error) {
    return null;
  }

  const candidatePath = path.resolve(FRONTEND_BUILD_DIR, `.${pathname}`);
  if (
    candidatePath !== FRONTEND_BUILD_DIR &&
    !candidatePath.startsWith(`${FRONTEND_BUILD_DIR}${path.sep}`)
  ) {
    return null;
  }
  return candidatePath;
}

function serveBuildFile(request, response, filePath) {
  const headers = {
    'Content-Type': CONTENT_TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream',
    'Cache-Control': 'no-store',
  };
  response.writeHead(200, headers);
  if (request.method === 'HEAD') {
    response.end();
    return;
  }
  fs.createReadStream(filePath)
    .on('error', () => {
      if (!response.headersSent) {
        response.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
      }
      response.end(`Could not read ${APP_BRAND_NAME} frontend asset.`);
    })
    .pipe(response);
}

function buildRuntimeConfigScript() {
  const json = JSON.stringify({
    backendApiUrl,
    backendMode: BACKEND_MODE,
    frontendMode: FRONTEND_MODE,
  })
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/&/g, '\\u0026');
  return `<script>window.BREAKTWENTY_RUNTIME_CONFIG=${json};</script>`;
}

function injectRuntimeConfig(html) {
  const script = buildRuntimeConfigScript();
  if (html.includes('</head>')) {
    return html.replace('</head>', `${script}</head>`);
  }
  return `${script}${html}`;
}

function serveBuildIndex(request, response, filePath) {
  const headers = {
    'Content-Type': 'text/html; charset=utf-8',
    'Cache-Control': 'no-store',
  };
  response.writeHead(200, headers);
  if (request.method === 'HEAD') {
    response.end();
    return;
  }
  fs.readFile(filePath, 'utf8', (error, html) => {
    if (error) {
      response.end(`Could not read ${APP_BRAND_NAME} frontend asset.`);
      return;
    }
    response.end(injectRuntimeConfig(html));
  });
}

function handleBuildFrontendRequest(request, response) {
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    response.writeHead(405, { Allow: 'GET, HEAD' });
    response.end();
    return;
  }

  const requestedPath = resolveBuildRequestPath(request.url || '/');
  if (!requestedPath) {
    response.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end(`Invalid ${APP_BRAND_NAME} frontend path.`);
    return;
  }

  const indexPath = frontendBuildIndexPath();
  let filePath = requestedPath;
  try {
    const stat = fs.statSync(filePath);
    if (stat.isDirectory()) {
      filePath = path.join(filePath, 'index.html');
    }
  } catch (_error) {
    filePath = path.extname(filePath) ? null : indexPath;
  }

  if (!filePath || !fs.existsSync(filePath)) {
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end(`${APP_BRAND_NAME} frontend asset was not found.`);
    return;
  }

  if (path.resolve(filePath) === path.resolve(indexPath)) {
    serveBuildIndex(request, response, filePath);
    return;
  }

  serveBuildFile(request, response, filePath);
}

function ensureBuildFrontendServer() {
  if (lifecycleCoordinator.isShuttingDown()) {
    return Promise.resolve({
      ok: false,
      message: `${APP_BRAND_NAME} is shutting down.`,
    });
  }
  if (frontendBuildServer) {
    return Promise.resolve({
      ok: true,
      status: 200,
    });
  }

  const indexPath = frontendBuildIndexPath();
  if (!fs.existsSync(indexPath)) {
    return Promise.resolve({
      ok: false,
      message: `Frontend build is missing at ${FRONTEND_BUILD_DIR}. Run npm --prefix ${path.join(APP_ROOT, 'frontend')} run build before starting build mode.`,
    });
  }

  return new Promise((resolve) => {
    const server = http.createServer(handleBuildFrontendRequest);
    server.once('error', (error) => {
      resolve({
        ok: false,
        message: `Could not start the ${APP_BRAND_NAME} frontend build server on ${buildFrontendUrl()}: ${error.message}`,
      });
    });
    server.listen(BUILD_FRONTEND_PORT, BUILD_FRONTEND_HOST, () => {
      if (lifecycleCoordinator.isShuttingDown()) {
        void closeHttpServer(server);
        resolve({
          ok: false,
          message: `${APP_BRAND_NAME} is shutting down.`,
        });
        return;
      }
      const address = server.address();
      const assignedPort = Number(address && typeof address === 'object' ? address.port : 0);
      if (!Number.isInteger(assignedPort) || assignedPort < 1 || assignedPort > 65535) {
        void closeHttpServer(server);
        resolve({
          ok: false,
          message: `${APP_BRAND_NAME} could not determine its frontend loopback port.`,
        });
        return;
      }
      activeBuildFrontendPort = assignedPort;
      frontendBuildServer = server;
      frontendUrl = buildFrontendUrl();
      resolve({
        ok: true,
        status: 200,
      });
    });
  });
}

function escapeLoadingHtml(value) {
  return String(value || '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[character]));
}

function readLoadingBrandAssetDataUrl(relativeAssetPath) {
  const candidatePaths = [
    path.join(FRONTEND_BUILD_DIR, relativeAssetPath),
    path.join(APP_ROOT, 'frontend', 'public', relativeAssetPath),
  ];
  for (const candidatePath of candidatePaths) {
    try {
      const image = fs.readFileSync(candidatePath);
      return `data:image/png;base64,${image.toString('base64')}`;
    } catch (_) {
      // Continue to the next packaged/development asset location.
    }
  }
  return '';
}

function loadingDataUrl(title, detail) {
  const escapedTitle = escapeLoadingHtml(title);
  const escapedDetail = escapeLoadingHtml(detail);
  const escapedCatchphrase = escapeLoadingHtml(LOADING_SCREEN_CONFIG.catchphrase);
  const markSrc = readLoadingBrandAssetDataUrl('assets/brand/breaktwenty-mark-dark.png');
  const wordmarkOneXSrc = readLoadingBrandAssetDataUrl('assets/brand/breaktwenty-wordmark-dark-160.png');
  const wordmarkTwoXSrc = readLoadingBrandAssetDataUrl('assets/brand/breaktwenty-wordmark-dark-320.png');
  const statusMarkup = escapedTitle || escapedDetail
    ? `<div class="loading-screen-status">${escapedTitle ? `<p class="loading-screen-status-title">${escapedTitle}</p>` : ''}${escapedDetail ? `<p class="loading-screen-status-detail">${escapedDetail}</p>` : ''}</div>`
    : '';
  return `data:text/html;charset=UTF-8,${encodeURIComponent(`
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>${APP_BRAND_NAME}</title>
        <style>
          ${LOADING_SCREEN_THEME_STYLES}
          :root {
            color-scheme: dark;
            --app-canvas-bg: var(--color-bg);
          }
          ${LOADING_SCREEN_STYLES}
        </style>
      </head>
      <body class="loading-screen-shell">
        <main class="loading-screen">
          ${markSrc ? `<img class="loading-screen-logo" src="${markSrc}" alt="" />` : ''}
          ${wordmarkOneXSrc
            ? `<img class="loading-screen-wordmark" src="${wordmarkOneXSrc}"${wordmarkTwoXSrc ? ` srcset="${wordmarkOneXSrc} 1x, ${wordmarkTwoXSrc} 2x"` : ''} alt="${APP_BRAND_NAME}" />`
            : '<h1 class="loading-screen-wordmark-fallback">Break<span class="loading-screen-wordmark-fallback-accent">Twenty</span></h1>'}
          <p class="loading-screen-catchphrase">${escapedCatchphrase}</p>
          <div class="loading-screen-spinner" aria-hidden="true"></div>
          ${statusMarkup}
        </main>
      </body>
    </html>
  `)}`;
}

async function prepareFrontend() {
  if (FRONTEND_MODE === 'build') {
    return ensureBuildFrontendServer();
  }
  return waitForFrontend();
}

function ensureBrowserRuntimePrewarm() {
  if (!backendManager || lifecycleCoordinator.isShuttingDown()) {
    return browserRuntimePrewarm;
  }
  if (browserRuntimePrewarm && !['failed', 'skipped'].includes(browserRuntimePrewarm.state)) {
    return browserRuntimePrewarm;
  }
  browserRuntimePrewarm = startBrowserRuntimePrewarm({
    appRoot: APP_ROOT,
    runtimeEnv: backendManager.getRuntimeEnv(),
  });
  return browserRuntimePrewarm;
}

function synchronizeBackendEndpoint() {
  backendApiUrl = backendManager.getBackendApiUrl();
  visibleAuthBroker?.setBackendApiUrl(backendApiUrl);
  visibleAuthBroker?.setRuntimeEnv(backendManager.getRuntimeEnv());
}

async function prepareBackend() {
  if (lifecycleCoordinator.isShuttingDown()) {
    return { ok: false, message: `${APP_BRAND_NAME} is shutting down.` };
  }
  try {
    await backendManager.start();
    synchronizeBackendEndpoint();
    if (lifecycleCoordinator.isShuttingDown()) {
      await backendManager.shutdown();
      return { ok: false, message: `${APP_BRAND_NAME} is shutting down.` };
    }
    visibleAuthBroker.setRuntimeEnv(backendManager.getRuntimeEnv());
    ensureBrowserRuntimePrewarm();
  } catch (error) {
    return {
      ok: false,
      message: error.message,
    };
  }
  const health = await waitForBackend({ source: 'startup' });
  if (health.ok || BACKEND_MODE !== 'embedded') return health;
  return recoverEmbeddedBackend({ trigger: 'startup_backend_not_ready', failedHealth: health });
}

async function waitForFrontend() {
  const deadline = Date.now() + FRONTEND_READY_TIMEOUT_MS;
  let lastHealth = null;

  while (!lifecycleCoordinator.isShuttingDown() && Date.now() < deadline) {
    lastHealth = await getFrontendHealth();
    if (lastHealth.ok) {
      return lastHealth;
    }
    await sleep(FRONTEND_READY_POLL_MS);
  }

  return lastHealth || {
    ok: false,
    message: `Timed out waiting for the ${APP_BRAND_NAME} frontend.`,
  };
}

async function loadBreakTwentyApp(window) {
  if (lifecycleCoordinator.isShuttingDown() || window.isDestroyed()) return;
  await window.loadURL(loadingDataUrl());
  if (lifecycleCoordinator.isShuttingDown() || window.isDestroyed()) return;
  let frontendHealth = null;
  if (USE_PACKAGED_DYNAMIC_FRONTEND_PORT) {
    frontendHealth = await prepareFrontend();
    if (lifecycleCoordinator.isShuttingDown() || window.isDestroyed()) return;
    if (!frontendHealth.ok) {
      await window.loadURL(loadingDataUrl(
        `${APP_BRAND_NAME} frontend build is not ready`,
        frontendHealth.message || `Last frontend status: ${frontendHealth.status || 'unavailable'}`,
      ));
      return;
    }
    backendManager.setFrontendOrigin(frontendUrl);
  }
  const backendHealth = await prepareBackend();
  if (lifecycleCoordinator.isShuttingDown() || window.isDestroyed()) return;
  if (!backendHealth.ok) {
    await window.loadURL(loadingDataUrl(
      `${APP_BRAND_NAME} backend is not ready`,
      backendHealth.message || `Last backend status: ${backendHealth.status || 'unavailable'}`,
    ));
    return;
  }
  frontendHealth = frontendHealth || await prepareFrontend();
  if (lifecycleCoordinator.isShuttingDown() || window.isDestroyed()) return;
  if (frontendHealth.ok) {
    await window.loadURL(frontendUrl);
    startBackendHealthSupervisor();
    return;
  }
  await window.loadURL(loadingDataUrl(
    FRONTEND_MODE === 'build'
      ? `${APP_BRAND_NAME} frontend build is not ready`
      : `${APP_BRAND_NAME} frontend is not ready`,
    frontendHealth.message || `Last frontend status: ${frontendHealth.status || 'unavailable'}`,
  ));
}

function createMainWindow() {
  if (lifecycleCoordinator.isShuttingDown()) {
    return;
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    focusExistingWindow(mainWindow);
    return;
  }
  const windowBounds = getMainWindowBounds();
  const zoomFactor = getMainWindowZoomFactor(windowBounds);
  mainWindowZoomPreferenceWasStored = Boolean(getStoredMainWindowZoomFactor());
  mainWindowZoomUserChanged = false;
  mainWindowZoomResetPreviewActive = false;

  mainWindow = new BrowserWindow({
    ...windowBounds,
    ...desktopIconWindowOptions(),
    backgroundColor: '#111827',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      zoomFactor,
      additionalArguments: [
        `--breaktwenty-platform=${process.platform}`,
        `--breaktwenty-packaged=${app.isPackaged ? '1' : '0'}`,
      ],
    },
  });
  applyDesktopWindowIcon(mainWindow);
  if (MAIN_WINDOW_SIZE.startMaximized) {
    mainWindow.maximize();
  }
  const createdMainWindow = mainWindow;
  const mainWindowWebContents = createdMainWindow.webContents;
  let updateInstallVisibilitySignaled = false;
  const signalUpdateInstallVisibility = () => {
    if (updateInstallVisibilitySignaled) return;
    try {
      updateInstallVisibilitySignaled = markUpdateInstallWindowVisible(app);
    } catch (error) {
      log.warn(`Could not acknowledge the visible main window to the update helper: ${error.message}`);
    }
  };
  mainWindowWebContents.on('console-message', (eventDetails, legacyLevel, legacyMessage, legacyLineNumber, legacySourceId) => {
    const details = normalizeConsoleMessageDetails(
      eventDetails,
      legacyLevel,
      legacyMessage,
      legacyLineNumber,
      legacySourceId,
    );
    const level = String(details.level || '').toLowerCase();
    const message = formatConsoleMessage(details);
    if (level === 'error') {
      log.error(`[renderer] ${message}`);
      return;
    }
    if (level === 'warning') {
      log.warn(`[renderer] ${message}`);
      return;
    }
    log.info(`[renderer] ${message}`);
  });

  mainWindowWebContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    log.error(
      `[renderer] did-fail-load mainFrame=${Boolean(isMainFrame)} code=${errorCode} `
      + `description=${cleanLogValue(errorDescription)} url=${cleanLogValue(validatedURL, 500)}`,
    );
    if (isMainFrame) {
      captureApplicationIncident({
        trigger: 'renderer_main_frame_load_failed',
        summary: 'The application renderer could not load its main page.',
        backend: backendManager?.describe?.() || null,
        probes: backendProbeHistory,
        details: { errorCode, errorDescription, validatedURL },
      });
    }
  });

  mainWindowWebContents.on('render-process-gone', (_event, details) => {
    log.error(`[renderer] render-process-gone reason=${details?.reason || 'unknown'} exitCode=${details?.exitCode ?? 'unknown'}`);
    captureApplicationIncident({
      trigger: 'renderer_process_gone',
      summary: 'The application renderer process exited unexpectedly.',
      backend: backendManager?.describe?.() || null,
      probes: backendProbeHistory,
      details,
    });
  });

  mainWindowWebContents.on('unresponsive', () => {
    log.warn('[renderer] main window became unresponsive');
    captureApplicationIncident({
      trigger: 'renderer_unresponsive',
      summary: 'The application renderer stopped responding.',
      backend: backendManager?.describe?.() || null,
      probes: backendProbeHistory,
    });
  });

  mainWindowWebContents.on('responsive', () => {
    log.info('[renderer] main window became responsive again');
  });

  mainWindowWebContents.on('did-finish-load', () => {
    if (!mainWindowWebContents.isDestroyed()) {
      log.info(`[renderer] did-finish-load url=${cleanLogValue(mainWindowWebContents.getURL(), 500)}`);
      mainWindowWebContents.setZoomFactor(getMainWindowZoomFactor(createdMainWindow.getBounds()));
      if (createdMainWindow.isVisible()) {
        signalUpdateInstallVisibility();
      }
    }
  });

  mainWindowWebContents.on('did-start-navigation', (details, _url, isInPlace, isMainFrame) => {
    const isMainFrameNavigation = details && typeof details.isMainFrame === 'boolean'
      ? details.isMainFrame
      : isMainFrame;
    const isSameDocumentNavigation = details && typeof details.isSameDocument === 'boolean'
      ? details.isSameDocument
      : isInPlace;
    if (isMainFrameNavigation && !isSameDocumentNavigation) {
      saveCurrentMainWindowZoomFactor(mainWindowWebContents, {
        requireFrontendUrl: true,
        requireUserZoomPreference: true,
      });
    }
  });

  mainWindowWebContents.on('did-start-loading', () => {
    saveCurrentMainWindowZoomFactor(mainWindowWebContents, {
      requireFrontendUrl: true,
      requireUserZoomPreference: true,
    });
  });

  mainWindowWebContents.on('zoom-changed', (event, zoomDirection) => {
    const shouldPersistUserZoom = !suppressNextMainWindowZoomSave;
    if (shouldPersistUserZoom) {
      mainWindowZoomResetPreviewActive = false;
      mainWindowZoomUserChanged = true;
    }
    const minimumZoomFactor = getMinimumMainWindowZoomFactor(createdMainWindow.getBounds());
    if (
      zoomDirection === 'out'
      && mainWindowWebContents.getZoomFactor() <= minimumZoomFactor + 0.001
    ) {
      event.preventDefault();
      if (Math.abs(mainWindowWebContents.getZoomFactor() - minimumZoomFactor) >= 0.001) {
        suppressNextMainWindowZoomSave = true;
        mainWindowWebContents.setZoomFactor(minimumZoomFactor);
        setImmediate(() => {
          suppressNextMainWindowZoomSave = false;
        });
      }
      notifyMainWindowZoomChanged();
      if (shouldPersistUserZoom) {
        saveMainWindowZoomFactor(minimumZoomFactor);
      }
      return;
    }
    setImmediate(() => {
      if (mainWindowWebContents.isDestroyed()) {
        return;
      }
      const displaySafeZoomFactor = clampMainWindowZoomFactorToDisplay(
        mainWindowWebContents.getZoomFactor(),
        createdMainWindow.getBounds(),
      );
      let zoomWasClamped = false;
      if (Math.abs(mainWindowWebContents.getZoomFactor() - displaySafeZoomFactor) >= 0.001) {
        zoomWasClamped = true;
        suppressNextMainWindowZoomSave = true;
        mainWindowWebContents.setZoomFactor(displaySafeZoomFactor);
        setImmediate(() => {
          suppressNextMainWindowZoomSave = false;
        });
      }
      notifyMainWindowZoomChanged();
      if (!shouldPersistUserZoom) {
        return;
      }
      if (zoomWasClamped) {
        saveMainWindowZoomFactor(displaySafeZoomFactor);
      } else {
        saveCurrentMainWindowZoomFactor(mainWindowWebContents, { requireFrontendUrl: true });
      }
    });
  });

  createdMainWindow.on('close', () => {
    saveCurrentMainWindowZoomFactor(mainWindowWebContents, {
      requireFrontendUrl: true,
      requireUserZoomPreference: true,
    });
  });
  createdMainWindow.on('move', scheduleAdaptiveMainWindowZoomForCurrentDisplay);
  createdMainWindow.once('closed', () => {
    if (mainWindowDisplayZoomTimer) {
      clearTimeout(mainWindowDisplayZoomTimer);
      mainWindowDisplayZoomTimer = null;
    }
    startupUpdateOfferedVersion = '';
    if (mainWindow === createdMainWindow) {
      mainWindow = null;
    }
  });

  mainWindowWebContents.setWindowOpenHandler(({ url }) => {
    openApprovedExternalUrl(url, 'main-window popup', { allowMailto: true });
    return { action: 'deny' };
  });

  mainWindowWebContents.on('will-navigate', (event, url) => {
    if (isFrontendUrl(url)) {
      return;
    }
    event.preventDefault();
    openApprovedExternalUrl(url, 'main-window navigation', { allowMailto: true });
  });

  void loadBreakTwentyApp(createdMainWindow);
  appUpdater?.scheduleStartupCheck();
  if (secondInstanceFocusPending) {
    secondInstanceFocusPending = false;
    focusExistingWindow(createdMainWindow);
  }
}

async function revealSupportArchive(request = {}) {
  const runtimePaths = backendManager?.describe?.()?.runtimePaths || {};
  const logDir = String(runtimePaths.logDir || '').trim();
  const targetPath = materializeOwnedSupportArchive({
    logDir,
    archiveId: request.archiveId,
    userId: LOCAL_DESKTOP_USER_ID,
  });
  if (!targetPath) {
    return { status: 'error', message: 'Support archive was not found.' };
  }
  try {
    shell.showItemInFolder(targetPath);
  } catch (error) {
    return { status: 'error', message: error.message || 'Could not reveal support archive.' };
  }
  return { status: 'ok' };
}

function listApplicationDiagnostics() {
  if (!appDiagnostics) {
    return {
      status: 'unavailable',
      message: 'Application diagnostics are not initialized.',
      incidents: [],
    };
  }
  try {
    return {
      status: 'ok',
      incidents: appDiagnostics.list(),
      policy: appDiagnostics.policy(),
      lastRecovery: lastBackendRecovery,
    };
  } catch (error) {
    return {
      status: 'error',
      message: error.message || 'Application diagnostics could not be listed.',
      incidents: [],
    };
  }
}

async function exportApplicationDiagnostic(request = {}) {
  if (!appDiagnostics) {
    return { status: 'error', message: 'Application diagnostics are not initialized.' };
  }
  let incidentId = String(request.incidentId || '');
  let incident = appDiagnostics.list().find((item) => item.incidentId === incidentId);
  if (!incidentId) {
    incident = captureApplicationIncident({
      trigger: 'user_requested_app_logs',
      summary: 'The user requested a current application diagnostic bundle.',
      backend: backendManager?.describe?.() || null,
      probes: backendProbeHistory,
      details: { currentHealth: await getBackendHealth() },
    });
    incidentId = String(incident?.incidentId || '');
  }
  if (!incident) {
    return { status: 'error', message: 'Application diagnostic incident was not found.' };
  }
  const timestamp = String(incident.createdAt || '')
    .replace(/[:]/g, '-')
    .replace(/\.\d{3}Z$/, 'Z')
    .replace('T', '_');
  const defaultPath = `${APP_BRAND_NAME}_application_diagnostics_${timestamp || incidentId}.zip`;
  const result = await dialog.showSaveDialog(mainWindow || undefined, {
    title: 'Export Application Diagnostics',
    defaultPath,
    filters: [{ name: 'ZIP archive', extensions: ['zip'] }],
  });
  if (result.canceled || !result.filePath) return { status: 'cancelled' };
  try {
    const exported = await appDiagnostics.exportIncident(incidentId, result.filePath);
    shell.showItemInFolder(result.filePath);
    return {
      status: 'ok',
      filename: path.basename(result.filePath),
      bytes: exported.bytes,
    };
  } catch (error) {
    return { status: 'error', message: error.message || 'Application diagnostics could not be exported.' };
  }
}

function quiesceDesktopLifecycle() {
  stopBackendHealthSupervisor();
  appUpdater?.quiesce();
  backendManager?.quiesce();
  visibleAuthBroker?.quiesce();
  if (mainWindowDisplayZoomTimer) {
    clearTimeout(mainWindowDisplayZoomTimer);
    mainWindowDisplayZoomTimer = null;
  }
  saveCurrentMainWindowZoomFactor(undefined, {
    requireFrontendUrl: true,
    requireUserZoomPreference: true,
  });
  if (app.isReady()) {
    screen.removeListener('display-metrics-changed', scheduleAdaptiveMainWindowZoomForCurrentDisplay);
  }
  return true;
}

async function stopFrontendBuildServer() {
  const server = frontendBuildServer;
  frontendBuildServer = null;
  return closeHttpServer(server);
}

async function stopBrowserRuntimePrewarm() {
  if (typeof browserRuntimePrewarm?.shutdown !== 'function') {
    return true;
  }
  return browserRuntimePrewarm.shutdown();
}

const lifecycleCoordinator = new DesktopLifecycleCoordinator({
  logger: log,
  phases: [
    {
      name: 'quiesce',
      state: 'quiescing',
      timeoutMs: 2000,
      tasks: [
        { name: 'desktop-entrypoints', run: quiesceDesktopLifecycle },
      ],
    },
    {
      name: 'runtime-stop',
      state: 'stopping-runtimes',
      timeoutMs: 10000,
      tasks: [
        { name: 'visible-auth', run: () => visibleAuthBroker?.shutdown() ?? true },
        { name: 'browser-prewarm', run: stopBrowserRuntimePrewarm },
        { name: 'frontend-build-server', run: stopFrontendBuildServer },
      ],
    },
    {
      name: 'launch-auth-revoke',
      state: 'revoking-launch-auth',
      timeoutMs: 6000,
      tasks: [
        { name: 'backend-launch-generation', run: () => backendManager?.revokeLaunchAuthentication() ?? true },
      ],
    },
    {
      name: 'backend-stop',
      state: 'stopping-backend',
      timeoutMs: 9000,
      tasks: [
        { name: 'embedded-backend', run: () => backendManager?.shutdown() ?? true },
      ],
    },
  ],
});

function requestApplicationShutdown(reason) {
  return lifecycleCoordinator.shutdown(reason);
}

async function prepareForUpdateInstall() {
  const report = await requestApplicationShutdown('update-install');
  const failedTasks = report.tasks.filter((task) => task.status !== 'completed');
  if (failedTasks.length > 0) {
    throw new Error(`shutdown did not complete for ${failedTasks.map((task) => task.name).join(', ')}`);
  }
  return report;
}

function focusRunningAppWindow() {
  if (lifecycleCoordinator.isShuttingDown()) return false;
  let focused = focusExistingWindow(mainWindow);
  if (!focused && app.isReady() && desktopRuntimeInitialized) {
    createMainWindow();
    focused = focusExistingWindow(mainWindow);
  }
  secondInstanceFocusPending = !focused;
  return focused;
}

const authorizePrivilegedIpc = createMainWindowIpcAuthorizer({
  getMainWindow: () => mainWindow,
  getFrontendUrl: () => frontendUrl,
});

function registerIpcHandlers() {
  const handle = (channel, handler) => (
    registerPrivilegedIpcHandler(ipcMain, authorizePrivilegedIpc, channel, handler)
  );
  handle('breaktwenty:main-window-zoom', () => getMainWindowZoomStatus());

  handle('breaktwenty:launch-auth', () => ({
    backendApiUrl,
    accessToken: backendManager.getRendererLaunchAuthToken(),
    tokenType: 'Bearer',
  }));

  handle('breaktwenty:desktop-status', async () => ({
    isDesktop: true,
    appVersion: app.getVersion(),
    backendMode: BACKEND_MODE,
    frontendMode: FRONTEND_MODE,
    frontendUrl,
    backendApiUrl,
    frontendHealth: await getFrontendHealth(),
    backendHealth: await getBackendHealth(),
    backendProcess: backendManager.describe(),
    mainWindowZoom: getMainWindowZoomStatus(),
    visibleAuth: visibleAuthBroker.describe(),
    browserRuntimePrewarm,
    updates: appUpdater ? appUpdater.getStatus() : null,
    lastBackendRecovery,
  }));

  handle('breaktwenty:backend-recovery-acknowledge', (request) => (
    backendRecoveryHandoff.acknowledge(request || {})
  ));

  handle('breaktwenty:visible-auth-launch', (request) => (
    visibleAuthBroker.launch(request || {})
  ));

  handle('breaktwenty:visible-auth-status', (request) => (
    visibleAuthBroker.status(request || {})
  ));

  handle('breaktwenty:visible-auth-cancel', (request) => (
    visibleAuthBroker.cancel(request || {})
  ));

  handle('breaktwenty:reveal-support-archive', (request) => (
    revealSupportArchive(request || {})
  ));

  handle('breaktwenty:app-diagnostics-list', () => listApplicationDiagnostics());

  handle('breaktwenty:app-diagnostics-export', (request) => (
    exportApplicationDiagnostic(request || {})
  ));
}

if (ownsSingleInstanceLock) {
  app.on('second-instance', () => {
    focusRunningAppWindow();
  });
}

app.whenReady().then(() => {
  if (!ownsSingleInstanceLock) {
    return;
  }
  if (desktopStartupOwnershipError) {
    dialog.showErrorBox(
      `${APP_BRAND_NAME} secure launcher required`,
      desktopStartupOwnershipError.message,
    );
    app.quit();
    return;
  }
  if (app.isPackaged && app.commandLine.hasSwitch('no-sandbox')) {
    log.error('Packaged startup rejected because Chromium sandboxing is unavailable.');
    dialog.showErrorBox(
      `${APP_BRAND_NAME} security requirement`,
      `${APP_BRAND_NAME} cannot start because Chromium process sandboxing was disabled. On Linux, enable unprivileged user namespaces or install the ${APP_BRAND_NAME} DEB package, then try again.`,
    );
    app.quit();
    return;
  }
  pruneElectronProfileCaches(app.getPath('userData'));
  backendManager = new BackendManager({
    app,
    appRoot: APP_ROOT,
    backendMode: BACKEND_MODE,
    backendApiUrl,
    frontendOrigin: FRONTEND_MODE === 'build' ? buildFrontendUrl() : DEV_FRONTEND_URL,
    dynamicBackendPort: USE_PACKAGED_DYNAMIC_BACKEND_PORT,
  });
  try {
    backendManager.prepareRuntimeEnv();
  } catch (_error) {
  }
  try {
    const runtimePaths = backendManager.describe().runtimePaths || {};
    const runtimeEnv = backendManager.getRuntimeEnv();
    const resourceRoot = runtimeEnv.BREAKTWENTY_RESOURCE_ROOT || APP_ROOT;
    const electronLogPath = log.transports.file.getFile().path;
    appDiagnostics = new AppDiagnostics({
      logDir: runtimePaths.logDir || path.join(app.getPath('userData'), 'data', 'logs'),
      appVersion: app.getVersion(),
      channel: process.env.BREAKTWENTY_UPDATE_CHANNEL || '',
      electronLogPath,
      backendLogPath: backendManager.describe().logPath
        || path.join(runtimePaths.logDir || '', 'embedded-backend-process.log'),
      runtimeFingerprintPath: path.join(resourceRoot, 'python-runtime-fingerprint.json'),
      redactionRoots: [
        app.getPath('home'),
        app.getPath('userData'),
        app.getPath('temp'),
        path.dirname(app.getPath('exe')),
        APP_ROOT,
        ...Object.values(runtimePaths),
      ],
    });
  } catch (error) {
    log.error(`Application diagnostics initialization failed: ${error.message}`);
  }
  visibleAuthBroker = new VisibleAuthBroker({
    appRoot: APP_ROOT,
    backendApiUrl,
    backendMode: BACKEND_MODE,
    runtimeEnv: backendManager.getRuntimeEnv(),
    getLaunchAuthToken: () => backendManager.getDesktopLaunchAuthToken(),
    restoreBackendReadiness: restoreBackendReadinessForRunnerGrant,
  });
  appUpdater = new BreakTwentyAppUpdater({
    app,
    ipcMain,
    getMainWindow: () => mainWindow,
    authorizeIpcEvent: authorizePrivilegedIpc,
    prepareForInstall: prepareForUpdateInstall,
    onStatusChange: handleDesktopUpdaterStatusChange,
  });
  ensureBrowserRuntimePrewarm();
  registerIpcHandlers();
  installApplicationMenu();
  desktopRuntimeInitialized = true;
  createMainWindow();
  screen.on('display-metrics-changed', scheduleAdaptiveMainWindowZoomForCurrentDisplay);

  app.on('activate', () => {
    if (!lifecycleCoordinator.isShuttingDown() && BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    }
  });
});

app.on('before-quit', (event) => {
  if (!ownsSingleInstanceLock || lifecycleCoordinator.state === 'stopped') {
    return;
  }
  event.preventDefault();
  if (quitResumeScheduled) {
    return;
  }
  quitResumeScheduled = true;
  void requestApplicationShutdown('app-quit').then((report) => {
    const failedTasks = report.tasks.filter((task) => task.status !== 'completed');
    if (failedTasks.length > 0) {
      log.warn(`Desktop shutdown completed with ${failedTasks.length} failed task(s).`);
    }
    app.quit();
  });
});

app.on('window-all-closed', () => {
  if (ownsSingleInstanceLock && process.platform !== 'darwin') {
    app.quit();
  }
});
