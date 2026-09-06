#!/usr/bin/env node

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { spawnSync } = require('node:child_process');
const { resolvePythonRuntimeInstallPolicy } = require('./python_runtime_install_policy');

const REPO_ROOT = path.resolve(__dirname, '..');
const DEFAULT_RUNTIME_ROOT = path.join(REPO_ROOT, 'desktop', 'dist', 'portable-python', 'current', 'python');
const DEFAULT_CACHE_ROOT = path.join(REPO_ROOT, 'desktop', 'dist', 'portable-python', 'cache');
const RUNTIME_POLICY_PATH = path.join(REPO_ROOT, 'backend', 'runtime-policy.json');
const runtimePolicyBytes = fs.readFileSync(RUNTIME_POLICY_PATH);
const runtimePolicy = JSON.parse(runtimePolicyBytes.toString('utf8'));

let runtimeRoot = process.env.BREAKTWENTY_PORTABLE_PYTHON_DIR || DEFAULT_RUNTIME_ROOT;
let cacheRoot = process.env.BREAKTWENTY_PORTABLE_PYTHON_CACHE_DIR || DEFAULT_CACHE_ROOT;
let targetTriple = process.env.BREAKTWENTY_PORTABLE_PYTHON_TARGET || '';
let pythonUrl = '';
let pythonSha256 = '';
let installDeps = !envDisables(process.env.BREAKTWENTY_PORTABLE_PYTHON_INSTALL_DEPS);
let pipOnlyBinary = process.env.BREAKTWENTY_PORTABLE_PYTHON_PIP_ONLY_BINARY || ':all:';
let pipNoBinary = process.env.BREAKTWENTY_PORTABLE_PYTHON_PIP_NO_BINARY || '';
let tmpRoot = '';

function log(message) {
  console.error(`[BreakTwenty] ${message}`);
}

function usage() {
  console.log(`Usage: ${path.basename(process.argv[1])} [--output PATH] [--cache PATH] [--target TRIPLE] [--pip-only-binary VALUE] [--pip-no-binary VALUE] [--skip-deps]`);
}

function envDisables(value) {
  return ['0', 'false', 'no', 'off'].includes(String(value || '').trim().toLowerCase());
}

function removePath(filePath) {
  if (!filePath) return;
  fs.rmSync(filePath, { recursive: true, force: true });
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function fileExists(filePath) {
  try {
    return fs.existsSync(filePath) && fs.statSync(filePath).isFile();
  } catch (_error) {
    return false;
  }
}

function dirExists(dirPath) {
  try {
    return fs.existsSync(dirPath) && fs.statSync(dirPath).isDirectory();
  } catch (_error) {
    return false;
  }
}

function run(command, args, options = {}) {
  const status = spawnSync(command, args, {
    cwd: options.cwd || process.cwd(),
    env: options.env || process.env,
    encoding: 'utf8',
    stdio: options.stdio || 'inherit',
    windowsHide: true,
  });
  if (status.error) {
    throw status.error;
  }
  if (status.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} exited with code ${status.status}`);
  }
  return status;
}

function parseArgs(argv) {
  for (let index = 0; index < argv.length;) {
    const arg = argv[index];
    switch (arg) {
      case '--output':
        runtimeRoot = requireValue(argv, index, arg);
        index += 2;
        break;
      case '--cache':
        cacheRoot = requireValue(argv, index, arg);
        index += 2;
        break;
      case '--target':
        targetTriple = requireValue(argv, index, arg);
        index += 2;
        break;
      case '--pip-only-binary':
        pipOnlyBinary = requireValue(argv, index, arg);
        index += 2;
        break;
      case '--pip-no-binary':
        pipNoBinary = requireValue(argv, index, arg);
        index += 2;
        break;
      case '--skip-deps':
        installDeps = false;
        index += 1;
        break;
      case '-h':
      case '--help':
        usage();
        process.exit(0);
        break;
      default:
        usage();
        throw new Error(`Unknown argument: ${arg}`);
    }
  }
}

function requireValue(argv, index, flag) {
  if (index + 1 >= argv.length) {
    usage();
    throw new Error(`${flag} requires a value.`);
  }
  return argv[index + 1];
}

function defaultTargetTriple() {
  const arch = process.arch;
  if (process.platform === 'linux') {
    if (arch === 'x64') return 'x86_64-unknown-linux-gnu';
    if (arch === 'arm64') return 'aarch64-unknown-linux-gnu';
  }
  if (process.platform === 'win32') {
    if (arch === 'x64') return 'x86_64-pc-windows-msvc';
    if (arch === 'arm64') return 'aarch64-pc-windows-msvc';
  }
  if (process.platform === 'darwin') {
    if (arch === 'x64') return 'x86_64-apple-darwin';
    if (arch === 'arm64') return 'aarch64-apple-darwin';
  }
  throw new Error(`Unsupported platform for portable Python: ${process.platform} ${arch}. Set BREAKTWENTY_PORTABLE_PYTHON_TARGET explicitly.`);
}

async function downloadToFile(url, destination) {
  ensureDir(path.dirname(destination));
  const response = await fetch(url, {
    redirect: 'follow',
    headers: {
      'User-Agent': 'BreakTwenty Desktop Portable Python Runtime',
    },
  });
  if (!response.ok || !response.body) {
    throw new Error(`Download failed with HTTP ${response.status}: ${url}`);
  }
  await pipeline(Readable.fromWeb(response.body), fs.createWriteStream(destination));
}

async function downloadArchive(archiveName) {
  const archivePath = path.join(cacheRoot, 'archives', archiveName);
  if (!fileExists(archivePath)) {
    log(`Downloading ${archiveName}...`);
    const tempPath = `${archivePath}.tmp`;
    await downloadToFile(pythonUrl, tempPath);
    fs.renameSync(tempPath, archivePath);
  } else {
    log(`Using cached ${archiveName}.`);
  }
  return archivePath;
}

function verifyArchive(archivePath) {
  if (!pythonSha256) {
    return;
  }
  log(`Verifying ${path.basename(archivePath)}...`);
  const actual = crypto.createHash('sha256').update(fs.readFileSync(archivePath)).digest('hex');
  if (actual.toLowerCase() !== pythonSha256.toLowerCase()) {
    throw new Error(`SHA256 mismatch for ${archivePath}. Expected ${pythonSha256}, got ${actual}.`);
  }
}

function pythonBinForRuntime(runtimeDir) {
  const candidates = [
    path.join(runtimeDir, 'bin', 'python3'),
    path.join(runtimeDir, 'bin', 'python'),
    path.join(runtimeDir, 'python.exe'),
  ];
  try {
    const binDir = path.join(runtimeDir, 'bin');
    const versionedPythonBins = fs.readdirSync(binDir)
      .filter((name) => /^python3(?:\.\d+)?$/.test(name))
      .sort((left, right) => right.localeCompare(left))
      .map((name) => path.join(binDir, name));
    candidates.push(...versionedPythonBins);
  } catch (_error) {
    // The standard candidate list above is enough for Windows and venv layouts.
  }
  const pythonBin = candidates.find(fileExists);
  if (!pythonBin) {
    return '';
  }
  return pythonBin;
}

function isPathInside(child, parent) {
  const relative = path.relative(parent, child);
  return Boolean(relative) && !relative.startsWith('..') && !path.isAbsolute(relative);
}

function rewriteCopiedSymlinks(runtimeDir, sourceRuntimeDir) {
  const pending = [runtimeDir];
  while (pending.length > 0) {
    const current = pending.shift();
    let entries = [];
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (_error) {
      continue;
    }
    for (const entry of entries) {
      const entryPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        pending.push(entryPath);
        continue;
      }
      if (!entry.isSymbolicLink()) {
        continue;
      }
      const target = fs.readlinkSync(entryPath);
      if (!path.isAbsolute(target) || !isPathInside(target, sourceRuntimeDir)) {
        continue;
      }
      const relativeToSource = path.relative(sourceRuntimeDir, target);
      const targetInRuntime = path.join(runtimeDir, relativeToSource);
      const relativeTarget = path.relative(path.dirname(entryPath), targetInRuntime) || '.';
      fs.unlinkSync(entryPath);
      fs.symlinkSync(relativeTarget, entryPath);
    }
  }
}

function findExtractedRuntime(extractedRoot) {
  const directCandidates = [
    path.join(extractedRoot, 'python', 'install'),
    path.join(extractedRoot, 'install'),
    path.join(extractedRoot, 'python'),
  ];
  for (const candidate of directCandidates) {
    if (pythonBinForRuntime(candidate)) {
      return candidate;
    }
  }

  const pending = [extractedRoot];
  while (pending.length > 0) {
    const current = pending.shift();
    if (pythonBinForRuntime(current)) {
      return current;
    }
    let entries = [];
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (_error) {
      continue;
    }
    for (const entry of entries) {
      if (entry.isDirectory()) {
        pending.push(path.join(current, entry.name));
      }
    }
  }
  return '';
}

async function installBackendDependencies(pythonBin) {
  if (!installDeps) {
    return;
  }
  const policy = await resolvePythonRuntimeInstallPolicy({
    pythonBin,
    targetTriple,
    pipOnlyBinary,
    pipNoBinary,
    opensslCacheRoot: path.join(cacheRoot, 'build-dependencies'),
  });
  pipOnlyBinary = policy.pipOnlyBinary;
  pipNoBinary = policy.pipNoBinary;
  const pipArgs = [];
  if (pipOnlyBinary) {
    pipArgs.push('--only-binary', pipOnlyBinary);
  }
  if (pipNoBinary) {
    pipArgs.push('--no-binary', pipNoBinary);
  }
  const env = policy.env;
  log('Installing backend dependencies into portable Python...');
  run(pythonBin, ['-s', '-m', 'ensurepip', '--upgrade'], { env });
  run(pythonBin, [
    '-s',
    '-m',
    'pip',
    'install',
    '--upgrade',
    '--require-hashes',
    '--no-deps',
    '--only-binary=:all:',
    '-r',
    path.join(REPO_ROOT, 'backend', 'requirements-bootstrap.lock'),
  ], { env });
  if (policy.requiresMacX64BuildPrerequisites) {
    log('Installing hash-verified Intel macOS cryptography build prerequisites...');
    run(pythonBin, [
      '-s',
      '-m',
      'pip',
      'install',
      '--upgrade',
      '--require-hashes',
      '--no-deps',
      '--only-binary=:all:',
      '-r',
      path.join(REPO_ROOT, 'backend', 'requirements-macos-x64-build.lock'),
    ], { env });
  }
  run(pythonBin, [
    '-s',
    '-m',
    'pip',
    'install',
    '--ignore-installed',
    '--require-hashes',
    '--no-build-isolation',
    ...pipArgs,
    '-r',
    path.join(REPO_ROOT, 'backend', 'requirements.lock'),
  ], { env });
  if (policy.requiresMacX64BuildPrerequisites) {
    run(pythonBin, ['-s', '-m', 'pip', 'uninstall', '--yes', 'maturin'], { env });
  }
}

function removePythonCache(rootDir) {
  if (!dirExists(rootDir)) return;
  for (const entry of fs.readdirSync(rootDir, { withFileTypes: true })) {
    const entryPath = path.join(rootDir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === '__pycache__') {
        removePath(entryPath);
      } else {
        removePythonCache(entryPath);
      }
      continue;
    }
    if (entry.isFile() && (entry.name.endsWith('.pyc') || entry.name.endsWith('.pyo'))) {
      removePath(entryPath);
    }
  }
}

function writeManifest(runtimeDir, archiveName, pythonBin) {
  const status = spawnSync(pythonBin, ['-s', '-c', 'import json, platform, ssl; print(json.dumps({"pythonVersion": platform.python_version(), "opensslVersion": ssl.OPENSSL_VERSION}))'], {
    env: {
      ...process.env,
      PYTHONNOUSERSITE: '1',
    },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  if (status.error || status.status !== 0) {
    throw new Error(`Could not read portable Python identity. ${status.error?.message || status.stderr || ''}`.trim());
  }
  const identity = JSON.parse(status.stdout);
  const expectedIdentity = {
    pythonVersion: runtimePolicy.python.version,
    opensslVersion: runtimePolicy.python.opensslVersion,
  };
  if (identity.pythonVersion !== expectedIdentity.pythonVersion
      || identity.opensslVersion !== expectedIdentity.opensslVersion) {
    throw new Error(`Portable Python identity does not match runtime policy. Expected ${JSON.stringify(expectedIdentity)}, got ${JSON.stringify(identity)}.`);
  }

  fs.writeFileSync(
    path.join(runtimeDir, 'BREAKTWENTY_PORTABLE_PYTHON.json'),
    `${JSON.stringify({
      runtimeKind: runtimePolicy.python.distribution,
      release: runtimePolicy.python.release,
      target: targetTriple,
      asset: archiveName,
      archiveSha256: pythonSha256,
      policySha256: crypto.createHash('sha256').update(runtimePolicyBytes).digest('hex'),
      pythonVersion: identity.pythonVersion,
      opensslVersion: identity.opensslVersion,
      pipOnlyBinary,
      pipNoBinary,
    }, null, 2)}\n`,
    'utf8',
  );
}

function tarPath(filePath) {
  return process.platform === 'win32' ? String(filePath).replace(/\\/g, '/') : filePath;
}

function extractArchive(archivePath, destination) {
  ensureDir(destination);
  const options = {};
  const args = ['-xzf', archivePath, '-C', destination];
  if (process.platform === 'win32') {
    options.cwd = path.dirname(archivePath);
    args[1] = path.basename(archivePath);
    args[3] = tarPath(destination);
  }
  run('tar', args, options);
}

async function prepareRuntime() {
  runtimeRoot = path.resolve(runtimeRoot);
  cacheRoot = path.resolve(cacheRoot);

  if (!targetTriple) {
    targetTriple = defaultTargetTriple();
  }

  const asset = runtimePolicy.python.assets[targetTriple];
  if (!asset) {
    throw new Error(`Runtime policy has no pinned asset for ${targetTriple}.`);
  }
  const archiveName = asset.name;
  pythonUrl = asset.url;
  pythonSha256 = asset.sha256;

  const archivePath = await downloadArchive(archiveName);
  verifyArchive(archivePath);

  const runtimeParent = path.dirname(runtimeRoot);
  tmpRoot = path.join(runtimeParent, `.portable-python.tmp.${process.pid}`);
  const extractRoot = path.join(tmpRoot, 'extract');
  const stagedRuntime = path.join(tmpRoot, 'python');
  removePath(tmpRoot);
  ensureDir(extractRoot);
  ensureDir(stagedRuntime);
  ensureDir(runtimeParent);

  log(`Extracting ${archiveName}...`);
  extractArchive(archivePath, extractRoot);
  const extractedRuntime = findExtractedRuntime(extractRoot);
  if (!extractedRuntime) {
    throw new Error('Could not find a Python runtime inside the extracted archive.');
  }

  fs.cpSync(extractedRuntime, stagedRuntime, { recursive: true });
  rewriteCopiedSymlinks(stagedRuntime, extractedRuntime);
  const pythonBin = pythonBinForRuntime(stagedRuntime);
  if (!pythonBin) {
    throw new Error(`No Python executable found under ${stagedRuntime}.`);
  }

  run(pythonBin, ['-s', '-V'], {
    env: {
      ...process.env,
      PYTHONNOUSERSITE: '1',
    },
  });
  await installBackendDependencies(pythonBin);
  run(pythonBin, [
    '-s',
    path.join(REPO_ROOT, 'scripts', 'verify_sqlcipher_runtime.py'),
  ], {
    env: {
      ...process.env,
      PYTHONNOUSERSITE: '1',
    },
  });
  removePythonCache(stagedRuntime);
  writeManifest(stagedRuntime, archiveName, pythonBin);

  removePath(runtimeRoot);
  fs.renameSync(stagedRuntime, runtimeRoot);
  removePath(tmpRoot);
  tmpRoot = '';
  log(`Portable Python runtime is ready at ${runtimeRoot}`);
}

async function main() {
  parseArgs(process.argv.slice(2));
  await prepareRuntime();
}

process.on('exit', () => {
  removePath(tmpRoot);
});

main().catch((error) => {
  console.error(`[BreakTwenty] ${error.message}`);
  process.exit(1);
});
