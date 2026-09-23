#!/usr/bin/env node

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { PACKAGED_SAFE_STORAGE_SMOKE_FLAG } = require(
  '../desktop/src/packagedSafeStorageSmokeProtocol',
);

function requireArgument(argv, name) {
  const index = argv.indexOf(name);
  if (index < 0 || index + 1 >= argv.length) {
    throw new Error(`${name} is required.`);
  }
  return argv[index + 1];
}

function resolvePackagedExecutable({ distDir, appName, platform = process.platform }) {
  if (!/^[A-Za-z0-9._-]+$/.test(appName)) {
    throw new Error('The packaged application name is unsafe.');
  }
  if (platform === 'win32') {
    return path.join(distDir, 'win-unpacked', `${appName}.exe`);
  }
  if (platform === 'darwin') {
    const candidates = fs.readdirSync(distDir, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && entry.name.startsWith('mac'))
      .map((entry) => path.join(
        distDir,
        entry.name,
        `${appName}.app`,
        'Contents',
        'MacOS',
        appName,
      ))
      .filter((candidate) => fs.existsSync(candidate));
    if (candidates.length !== 1) {
      throw new Error(`Expected one packaged macOS executable, found ${candidates.length}.`);
    }
    return candidates[0];
  }
  if (platform === 'linux') {
    return path.join(distDir, 'linux-unpacked', appName);
  }
  throw new Error(`Packaged Safe Storage smoke is unsupported on ${platform}.`);
}

function runPhase(executablePath, stateDir, mode, expectedStorageIdentity) {
  const env = {
    ...process.env,
    BREAKTWENTY_SAFE_STORAGE_SMOKE_DIR: stateDir,
    BREAKTWENTY_SAFE_STORAGE_SMOKE_MODE: mode,
  };
  delete env.ELECTRON_RUN_AS_NODE;
  const result = spawnSync(executablePath, [PACKAGED_SAFE_STORAGE_SMOKE_FLAG], {
    encoding: 'utf8',
    env,
    timeout: 120000,
    windowsHide: true,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `${mode} failed with code ${result.status}: ${String(result.stderr || result.stdout || '').trim()}`,
    );
  }
  const resultPath = path.join(stateDir, `${mode}.json`);
  if (!fs.existsSync(resultPath)) {
    throw new Error(`${mode} returned no Safe Storage success record.`);
  }
  const payload = JSON.parse(fs.readFileSync(resultPath, 'utf8'));
  if (
    payload.ok !== true
    || payload.mode !== mode
    || payload.storageIdentity !== expectedStorageIdentity
  ) {
    throw new Error(`${mode} returned an invalid Safe Storage result.`);
  }
}

function main(argv = process.argv.slice(2)) {
  const distDir = path.resolve(requireArgument(argv, '--dist'));
  const appName = requireArgument(argv, '--app-name');
  const executablePath = resolvePackagedExecutable({ distDir, appName });
  if (!fs.statSync(executablePath).isFile()) {
    throw new Error(`Packaged application executable is missing: ${executablePath}`);
  }
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-safe-storage-smoke-'));
  const expectedStorageIdentity = process.platform === 'darwin' ? appName : 'BreakTwenty';
  try {
    runPhase(executablePath, stateDir, 'smoke-encrypt', expectedStorageIdentity);
    runPhase(executablePath, stateDir, 'smoke-decrypt', expectedStorageIdentity);
    process.stdout.write(`Packaged ${appName} Safe Storage round trip passed.\n`);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  resolvePackagedExecutable,
};
