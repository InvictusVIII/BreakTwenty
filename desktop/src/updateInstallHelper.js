const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const log = require('electron-log');

const HELPER_TIMEOUT_SECONDS = 10 * 60;
const HELPER_DISPLAY_DELAY_SECONDS = 3;
const WINDOWS_HELPER_FILE_NAME = 'BreakTwentyUpdateHelper.exe';
const LINUX_HELPER_FILE_NAME = 'BreakTwentyUpdateHelper';
const MACOS_HELPER_FILE_NAME = 'BreakTwentyUpdateHelper';
const UPDATE_READY_FILE_NAME = 'app-window-visible';
const DARK_MARK_FILE_NAME = 'breaktwenty-mark-dark.png';
const DARK_WORDMARK_FILE_NAME = 'breaktwenty-wordmark-dark-320.png';

function cleanVersion(value) {
  return String(value || '').replace(/[^\w .+-]/g, '').trim();
}

function ensureHelperDir(app) {
  const helperDir = path.join(app.getPath('userData'), 'update-helper');
  fs.mkdirSync(helperDir, { recursive: true, mode: 0o700 });
  return helperDir;
}

function nativeHelperSourcePath(fileName, resourcesPath = process.resourcesPath) {
  return path.join(
    String(resourcesPath || ''),
    'desktop',
    'update-helper',
    fileName,
  );
}

function windowsHelperSourcePath(resourcesPath = process.resourcesPath) {
  return nativeHelperSourcePath(WINDOWS_HELPER_FILE_NAME, resourcesPath);
}

function linuxHelperSourcePath(resourcesPath = process.resourcesPath) {
  return nativeHelperSourcePath(LINUX_HELPER_FILE_NAME, resourcesPath);
}

function macosHelperSourcePath(resourcesPath = process.resourcesPath) {
  return nativeHelperSourcePath(MACOS_HELPER_FILE_NAME, resourcesPath);
}

function helperBrandAssetSourcePaths(resourcesPath = process.resourcesPath) {
  const brandRoot = path.join(String(resourcesPath || ''), 'frontend', 'build', 'assets', 'brand');
  return {
    markPath: path.join(brandRoot, DARK_MARK_FILE_NAME),
    wordmarkPath: path.join(brandRoot, DARK_WORDMARK_FILE_NAME),
  };
}

function cleanStaleHelperFiles(helperDir, keepNames) {
  const keep = new Set(keepNames);
  const stalePattern = /^(?:BreakTwentyUpdateHelper-\d+(?:\.exe)?|\d+-(?:breaktwenty-mark-dark|breaktwenty-wordmark-(?:original-dark|dark-320))\.png)$/i;
  for (const entry of fs.readdirSync(helperDir, { withFileTypes: true })) {
    if (entry.isFile() && stalePattern.test(entry.name) && !keep.has(entry.name)) {
      try {
        fs.rmSync(path.join(helperDir, entry.name), { force: true });
      } catch (_error) {
      }
    }
  }
}

function prepareHelperBrandAssets(app) {
  const helperDir = ensureHelperDir(app);
  const sources = helperBrandAssetSourcePaths();
  for (const sourcePath of Object.values(sources)) {
    if (!fs.existsSync(sourcePath)) {
      throw new Error(`the packaged update-helper brand asset ${path.basename(sourcePath)} is missing`);
    }
  }
  const markName = `${process.pid}-${DARK_MARK_FILE_NAME}`;
  const wordmarkName = `${process.pid}-${DARK_WORDMARK_FILE_NAME}`;
  cleanStaleHelperFiles(helperDir, [markName, wordmarkName]);
  const markPath = path.join(helperDir, markName);
  const wordmarkPath = path.join(helperDir, wordmarkName);
  fs.copyFileSync(sources.markPath, markPath);
  fs.copyFileSync(sources.wordmarkPath, wordmarkPath);
  return { markPath, wordmarkPath };
}

function updateReadyPath(app) {
  return path.join(ensureHelperDir(app), UPDATE_READY_FILE_NAME);
}

function resetUpdateReadySignal(app) {
  const readyPath = updateReadyPath(app);
  fs.rmSync(readyPath, { force: true });
  return readyPath;
}

function markUpdateInstallWindowVisible(app) {
  if (!app) return false;
  const readyPath = updateReadyPath(app);
  fs.writeFileSync(readyPath, '', { encoding: 'utf8', mode: 0o600 });
  return true;
}

function windowsHelperArguments({ readyPath, version }) {
  const args = [
    '--ready-path',
    String(readyPath || ''),
    '--delay-ms',
    String(HELPER_DISPLAY_DELAY_SECONDS * 1000),
    '--timeout-seconds',
    String(HELPER_TIMEOUT_SECONDS),
  ];
  if (version) {
    args.push('--version', cleanVersion(version));
  }
  return args;
}

function prepareWindowsHelperExecutable(app) {
  const sourcePath = windowsHelperSourcePath();
  if (!fs.existsSync(sourcePath)) {
    throw new Error('the packaged Windows update helper is missing');
  }
  const helperDir = ensureHelperDir(app);
  const targetName = `BreakTwentyUpdateHelper-${process.pid}.exe`;
  const targetPath = path.join(helperDir, targetName);
  cleanStaleHelperFiles(helperDir, [targetName]);
  fs.copyFileSync(sourcePath, targetPath);
  return targetPath;
}

function prepareUnixHelperExecutable(app, sourcePath) {
  if (!fs.existsSync(sourcePath)) {
    throw new Error('the packaged update helper is missing');
  }
  const helperDir = ensureHelperDir(app);
  const targetName = `BreakTwentyUpdateHelper-${process.pid}`;
  const targetPath = path.join(helperDir, targetName);
  cleanStaleHelperFiles(helperDir, [
    targetName,
    `${process.pid}-${DARK_MARK_FILE_NAME}`,
    `${process.pid}-${DARK_WORDMARK_FILE_NAME}`,
  ]);
  fs.copyFileSync(sourcePath, targetPath);
  fs.chmodSync(targetPath, 0o700);
  return targetPath;
}

function brandedHelperArguments({ markPath, readyPath, version, wordmarkPath }) {
  const args = [
    '--ready-path', String(readyPath || ''),
    '--mark-path', String(markPath || ''),
    '--wordmark-path', String(wordmarkPath || ''),
    '--delay-ms', String(HELPER_DISPLAY_DELAY_SECONDS * 1000),
    '--timeout-seconds', String(HELPER_TIMEOUT_SECONDS),
  ];
  if (version) {
    args.push('--version', cleanVersion(version));
  }
  return args;
}

function spawnDetached(command, args, { windowsHide = true } = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      detached: true,
      stdio: 'ignore',
      windowsHide,
    });
    child.once('error', (error) => {
      const reason = error?.code ? ` (${error.code})` : '';
      log.warn(`Persistent update helper process could not start${reason}; update installation is continuing without it.`);
      resolve(false);
    });
    child.once('spawn', () => {
      child.unref();
      resolve(true);
    });
  });
}

async function startPersistentInstallHelper({ app, version = null } = {}) {
  if (!app || process.env.BREAKTWENTY_UPDATE_PERSISTENT_HELPER === '0') {
    return false;
  }
  if (process.platform !== 'win32' && process.platform !== 'linux' && process.platform !== 'darwin') {
    return false;
  }

  const clean = cleanVersion(version);
  const readyPath = resetUpdateReadySignal(app);
  if (process.platform === 'win32') {
    const helperPath = prepareWindowsHelperExecutable(app);
    return spawnDetached(helperPath, windowsHelperArguments({
      readyPath,
      version: clean,
    }), { windowsHide: false });
  }

  if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY) {
    return false;
  }
  const brandAssets = prepareHelperBrandAssets(app);
  const helperPath = prepareUnixHelperExecutable(
    app,
    process.platform === 'darwin' ? macosHelperSourcePath() : linuxHelperSourcePath(),
  );
  return spawnDetached(helperPath, brandedHelperArguments({
    ...brandAssets,
    readyPath,
    version: clean,
  }), { windowsHide: false });
}

async function tryStartPersistentInstallHelper(request) {
  try {
    return await startPersistentInstallHelper(request);
  } catch (error) {
    log.warn(`Could not start persistent update helper: ${error.message}. Update installation is continuing without it.`);
    return false;
  }
}

module.exports = {
  markUpdateInstallWindowVisible,
  tryStartPersistentInstallHelper,
  _test: {
    brandedHelperArguments,
    helperBrandAssetSourcePaths,
    linuxHelperSourcePath,
    macosHelperSourcePath,
    updateReadyPath,
    windowsHelperArguments,
    windowsHelperSourcePath,
  },
};
