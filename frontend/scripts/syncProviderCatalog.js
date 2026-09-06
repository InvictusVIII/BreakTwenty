const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_SOURCE_PATH = path.resolve(__dirname, '..', '..', 'config', 'provider_catalog.json');
const TARGET_PATH = path.resolve(__dirname, '..', 'src', 'constants', 'providerCatalog.json');
const WATCH_INTERVAL_MS = 1000;

function getSourcePath() {
  return process.env.BREAKTWENTY_PROVIDER_CATALOG_SOURCE || DEFAULT_SOURCE_PATH;
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function readCatalog(sourcePath) {
  const raw = fs.readFileSync(sourcePath, 'utf8');
  const parsed = JSON.parse(raw);
  if (!isPlainObject(parsed)) {
    throw new Error(`Provider catalog at ${sourcePath} must be a JSON object.`);
  }
  const catalog = {};
  for (const [provider, metadata] of Object.entries(parsed)) {
    if (typeof provider === 'string' && provider.startsWith('$')) {
      continue;
    }
    if (!isPlainObject(metadata)) {
      throw new Error(`Provider catalog entry "${provider}" must be a JSON object.`);
    }
    catalog[provider] = metadata;
  }
  return catalog;
}

function formatCatalog(catalog) {
  return `${JSON.stringify(catalog, null, 2)}\n`;
}

function writeAtomically(targetPath, content) {
  const targetDir = path.dirname(targetPath);
  fs.mkdirSync(targetDir, { recursive: true });
  const tempPath = `${targetPath}.tmp`;
  fs.writeFileSync(tempPath, content, 'utf8');
  fs.renameSync(tempPath, targetPath);
}

function syncProviderCatalog({ sourcePath = getSourcePath(), targetPath = TARGET_PATH, check = false, silent = false } = {}) {
  const nextContent = formatCatalog(readCatalog(sourcePath));
  const currentContent = fs.existsSync(targetPath) ? fs.readFileSync(targetPath, 'utf8') : null;

  if (currentContent === nextContent) {
    if (!silent) {
      console.log(`Provider catalog mirror is up to date: ${targetPath}`);
    }
    return false;
  }

  if (check) {
    throw new Error(
      'Provider catalog mirror is out of sync. Run "node scripts/syncProviderCatalog.js" from the frontend directory.',
    );
  }

  writeAtomically(targetPath, nextContent);
  if (!silent) {
    console.log(`Synced provider catalog mirror: ${targetPath}`);
  }
  return true;
}

function watchProviderCatalog({ sourcePath = getSourcePath(), targetPath = TARGET_PATH, intervalMs = WATCH_INTERVAL_MS } = {}) {
  let lastKnownMtimeMs = fs.statSync(sourcePath).mtimeMs;

  const timer = setInterval(() => {
    try {
      const nextMtimeMs = fs.statSync(sourcePath).mtimeMs;
      if (nextMtimeMs === lastKnownMtimeMs && fs.existsSync(targetPath)) {
        return;
      }
      syncProviderCatalog({ sourcePath, targetPath, silent: true });
      lastKnownMtimeMs = nextMtimeMs;
    } catch (error) {
      console.error(`[provider-catalog] ${error.message}`);
    }
  }, intervalMs);

  return () => clearInterval(timer);
}

function main() {
  const args = new Set(process.argv.slice(2));
  const check = args.has('--check');
  const watch = args.has('--watch');

  syncProviderCatalog({ check });

  if (!watch) {
    return;
  }

  watchProviderCatalog();
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(`[provider-catalog] ${error.message}`);
    process.exit(1);
  }
}

module.exports = {
  TARGET_PATH,
  getSourcePath,
  syncProviderCatalog,
  watchProviderCatalog,
};
