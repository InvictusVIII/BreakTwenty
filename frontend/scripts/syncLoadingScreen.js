const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_SOURCE_DIR = path.resolve(__dirname, '..', '..', 'config');
const TARGET_CONFIG_PATH = path.resolve(__dirname, '..', 'src', 'constants', 'loadingScreen.generated.json');
const TARGET_STYLES_PATH = path.resolve(__dirname, '..', 'src', 'loadingScreen.generated.css');
const WATCH_INTERVAL_MS = 1000;

function getSourceDir() {
  if (process.env.BREAKTWENTY_SHARED_CONFIG_DIR) {
    return path.resolve(process.env.BREAKTWENTY_SHARED_CONFIG_DIR);
  }
  if (process.env.BREAKTWENTY_PROVIDER_CATALOG_SOURCE) {
    return path.dirname(path.resolve(process.env.BREAKTWENTY_PROVIDER_CATALOG_SOURCE));
  }
  return DEFAULT_SOURCE_DIR;
}

function sourcePaths(sourceDir = getSourceDir()) {
  return {
    config: path.join(sourceDir, 'loading-screen.json'),
    styles: path.join(sourceDir, 'loading-screen.css'),
  };
}

function readSource(sourcePath) {
  return fs.readFileSync(sourcePath, 'utf8');
}

function validateConfig(content, sourcePath) {
  const parsed = JSON.parse(content);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`Loading-screen config at ${sourcePath} must be a JSON object.`);
  }
  if (typeof parsed.catchphrase !== 'string' || !parsed.catchphrase.trim()) {
    throw new Error(`Loading-screen config at ${sourcePath} must define a non-empty catchphrase.`);
  }
}

function writeAtomically(targetPath, content) {
  const tempPath = `${targetPath}.tmp`;
  fs.writeFileSync(tempPath, content, 'utf8');
  fs.renameSync(tempPath, targetPath);
}

function syncFile(sourcePath, targetPath, { check, silent }) {
  const nextContent = readSource(sourcePath);
  const currentContent = fs.existsSync(targetPath) ? fs.readFileSync(targetPath, 'utf8') : null;
  if (currentContent === nextContent) return false;
  if (check) {
    throw new Error(
      `Loading-screen mirror ${targetPath} is out of sync with ${sourcePath}. ` +
      'Run "node scripts/syncLoadingScreen.js" from the frontend directory.',
    );
  }
  writeAtomically(targetPath, nextContent);
  if (!silent) console.log(`Synced loading-screen mirror: ${targetPath}`);
  return true;
}

function syncLoadingScreen({ sourceDir = getSourceDir(), check = false, silent = false } = {}) {
  const sources = sourcePaths(sourceDir);
  const configContent = readSource(sources.config);
  validateConfig(configContent, sources.config);
  const changedConfig = syncFile(sources.config, TARGET_CONFIG_PATH, { check, silent });
  const changedStyles = syncFile(sources.styles, TARGET_STYLES_PATH, { check, silent });
  if (!silent && !changedConfig && !changedStyles) {
    console.log('Loading-screen mirrors are up to date.');
  }
  return changedConfig || changedStyles;
}

function watchLoadingScreen({ sourceDir = getSourceDir(), intervalMs = WATCH_INTERVAL_MS } = {}) {
  const sources = sourcePaths(sourceDir);
  let lastSignature = '';

  const timer = setInterval(() => {
    try {
      const signature = Object.values(sources)
        .map((sourcePath) => fs.statSync(sourcePath).mtimeMs)
        .join(':');
      if (signature === lastSignature && fs.existsSync(TARGET_CONFIG_PATH) && fs.existsSync(TARGET_STYLES_PATH)) {
        return;
      }
      syncLoadingScreen({ sourceDir, silent: true });
      lastSignature = signature;
    } catch (error) {
      console.error(`[loading-screen] ${error.message}`);
    }
  }, intervalMs);

  return () => clearInterval(timer);
}

function main() {
  const args = new Set(process.argv.slice(2));
  const check = args.has('--check');
  syncLoadingScreen({ check });
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[loading-screen] ${error.message}`);
    process.exit(1);
  }
}

module.exports = {
  getSourceDir,
  syncLoadingScreen,
  watchLoadingScreen,
};
