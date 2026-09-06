const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { spawnSync } = require('node:child_process');

const MACOS_X64_TARGET = 'x86_64-apple-darwin';
const MACOS_DEPLOYMENT_TARGET = '13.0';
const MINIMUM_RUST_VERSION = [1, 83, 0];
const OPENSSL_VERSION = '4.0.1';
const OPENSSL_SHA256 = '2db3f3a0d6ea4b59e1f094ace2c8cd536dffb87cdc39084c5afa1e6f7f37dd09';
const OPENSSL_URL = `https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz`;

function runProbe(command, args, spawn = spawnSync) {
  const result = spawn(command, args, {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  if (result.error || result.status !== 0) return '';
  return String(result.stdout || '').trim();
}

function runChecked(command, args, options = {}, spawn = spawnSync) {
  const result = spawn(command, args, {
    cwd: options.cwd || process.cwd(),
    env: options.env || process.env,
    encoding: 'utf8',
    stdio: options.stdio || 'inherit',
    windowsHide: true,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} exited with code ${result.status}`);
  }
}

function packageList(value) {
  return String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function addPackage(value, packageName) {
  const packages = packageList(value);
  if (!packages.some((item) => item.toLowerCase() === packageName.toLowerCase())) {
    packages.push(packageName);
  }
  return packages.join(',');
}

function runtimeIsMacX64(pythonBin, spawn = spawnSync) {
  const output = runProbe(
    pythonBin,
    [
      '-s',
      '-c',
      'import json, platform; print(json.dumps({"system": platform.system(), "machine": platform.machine()}))',
    ],
    spawn,
  );
  if (!output) {
    throw new Error(`Could not identify the packaged Python target from ${pythonBin}.`);
  }
  let runtime;
  try {
    runtime = JSON.parse(output);
  } catch (_error) {
    throw new Error(`Packaged Python returned an invalid target description: ${output}`);
  }
  return runtime.system === 'Darwin' && ['x86_64', 'amd64'].includes(String(runtime.machine).toLowerCase());
}

function versionAtLeast(actual, minimum) {
  for (let index = 0; index < minimum.length; index += 1) {
    const actualPart = Number(actual[index] || 0);
    if (actualPart > minimum[index]) return true;
    if (actualPart < minimum[index]) return false;
  }
  return true;
}

function validateRust(spawn = spawnSync) {
  const rustcVersion = runProbe('rustc', ['--version'], spawn);
  const match = /^rustc\s+(\d+)\.(\d+)\.(\d+)/.exec(rustcVersion);
  if (!match || !versionAtLeast(match.slice(1).map(Number), MINIMUM_RUST_VERSION)) {
    throw new Error(
      'Intel macOS packaging requires Rust 1.83.0 or newer. Install the current Rust toolchain, then retry.',
    );
  }
  if (!runProbe('cargo', ['--version'], spawn)) {
    throw new Error('Intel macOS packaging requires Cargo. Install the current Rust toolchain, then retry.');
  }
}

function sha256File(filePath) {
  return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex');
}

function validateOpenSslRoot(candidate, spawn = spawnSync) {
  if (!candidate) return '';
  const root = path.resolve(candidate);
  const required = [
    path.join(root, 'include', 'openssl', 'ssl.h'),
    path.join(root, 'lib', 'libssl.a'),
    path.join(root, 'lib', 'libcrypto.a'),
  ];
  if (!required.every((filePath) => fs.existsSync(filePath) && fs.statSync(filePath).isFile())) {
    return '';
  }
  const opensslBin = path.join(root, 'bin', 'openssl');
  const version = runProbe(opensslBin, ['version'], spawn);
  return version.startsWith(`OpenSSL ${OPENSSL_VERSION} `) ? root : '';
}

async function downloadFile(url, destination, fetchImpl = fetch) {
  const response = await fetchImpl(url, {
    redirect: 'follow',
    headers: { 'User-Agent': 'BreakTwenty Intel macOS OpenSSL builder' },
  });
  if (!response.ok || !response.body) {
    throw new Error(`OpenSSL download failed with HTTP ${response.status}: ${url}`);
  }
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  await pipeline(Readable.fromWeb(response.body), fs.createWriteStream(destination));
}

async function ensurePinnedMacX64OpenSsl({
  cacheRoot,
  baseEnv = process.env,
  spawn = spawnSync,
  fetchImpl = fetch,
}) {
  const root = path.resolve(cacheRoot, `openssl-${OPENSSL_VERSION}-macos-x64`);
  const existing = validateOpenSslRoot(root, spawn);
  if (existing) return existing;

  const archive = path.resolve(cacheRoot, 'downloads', `openssl-${OPENSSL_VERSION}.tar.gz`);
  if (!fs.existsSync(archive)) {
    const temporary = `${archive}.tmp.${process.pid}`;
    await downloadFile(OPENSSL_URL, temporary, fetchImpl);
    if (sha256File(temporary) !== OPENSSL_SHA256) {
      fs.rmSync(temporary, { force: true });
      throw new Error(`OpenSSL ${OPENSSL_VERSION} SHA-256 verification failed.`);
    }
    fs.renameSync(temporary, archive);
  } else if (sha256File(archive) !== OPENSSL_SHA256) {
    throw new Error(`Cached OpenSSL ${OPENSSL_VERSION} archive failed SHA-256 verification.`);
  }

  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-openssl-build-'));
  const stagedRoot = `${root}.tmp.${process.pid}`;
  fs.rmSync(stagedRoot, { recursive: true, force: true });
  try {
    runChecked('tar', ['-xzf', archive, '-C', temporaryRoot], {}, spawn);
    const sourceRoot = path.join(temporaryRoot, `openssl-${OPENSSL_VERSION}`);
    if (!fs.existsSync(path.join(sourceRoot, 'Configure'))) {
      throw new Error(`OpenSSL ${OPENSSL_VERSION} source archive has an unexpected layout.`);
    }
    const buildEnv = {
      ...baseEnv,
      MACOSX_DEPLOYMENT_TARGET: MACOS_DEPLOYMENT_TARGET,
    };
    runChecked('./Configure', [
      'darwin64-x86_64-cc',
      'no-shared',
      'no-tests',
      `--prefix=${stagedRoot}`,
      `--openssldir=${path.join(stagedRoot, 'ssl')}`,
    ], { cwd: sourceRoot, env: buildEnv }, spawn);
    runChecked('make', [`-j${Math.max(1, os.availableParallelism())}`], { cwd: sourceRoot, env: buildEnv }, spawn);
    runChecked('make', ['install_sw'], { cwd: sourceRoot, env: buildEnv }, spawn);
    if (!validateOpenSslRoot(stagedRoot, spawn)) {
      throw new Error(`Built OpenSSL ${OPENSSL_VERSION} failed artifact validation.`);
    }
    fs.mkdirSync(path.dirname(root), { recursive: true });
    fs.rmSync(root, { recursive: true, force: true });
    fs.renameSync(stagedRoot, root);
  } finally {
    fs.rmSync(stagedRoot, { recursive: true, force: true });
    fs.rmSync(temporaryRoot, { recursive: true, force: true });
  }
  return root;
}

async function resolvePythonRuntimeInstallPolicy({
  pythonBin,
  targetTriple = '',
  pipOnlyBinary = '',
  pipNoBinary = '',
  opensslCacheRoot = '',
  baseEnv = process.env,
  hostPlatform = process.platform,
  hostArch = process.arch,
  spawn = spawnSync,
  fetchImpl = fetch,
}) {
  const runtimePath = [path.dirname(pythonBin), String(baseEnv.PATH || '')]
    .filter(Boolean)
    .join(path.delimiter);
  const macX64 = targetTriple
    ? targetTriple === MACOS_X64_TARGET
    : runtimeIsMacX64(pythonBin, spawn);
  if (!macX64) {
    return {
      env: { ...baseEnv, PATH: runtimePath, PYTHONNOUSERSITE: '1' },
      pipOnlyBinary,
      pipNoBinary,
      requiresMacX64BuildPrerequisites: false,
    };
  }

  if (hostPlatform !== 'darwin' || hostArch !== 'x64') {
    throw new Error(
      'The Intel macOS runtime must be assembled on Intel macOS; cross-building its native Python extensions is not supported.',
    );
  }
  if (!runProbe('xcrun', ['--find', 'clang'], spawn)) {
    throw new Error(
      'Intel macOS packaging requires the Xcode command-line tools. Run xcode-select --install, then retry.',
    );
  }
  if (!runProbe('make', ['--version'], spawn)) {
    throw new Error('Intel macOS packaging requires make from the Xcode command-line tools.');
  }
  if (!runProbe('perl', ['-v'], spawn)) {
    throw new Error('Intel macOS packaging requires Perl to configure the pinned OpenSSL source.');
  }
  validateRust(spawn);
  const opensslRoot = await ensurePinnedMacX64OpenSsl({
    cacheRoot: opensslCacheRoot || path.join(os.homedir(), 'Library', 'Caches', 'BreakTwenty', 'build'),
    baseEnv,
    spawn,
    fetchImpl,
  });

  return {
    env: {
      ...baseEnv,
      ARCHFLAGS: '-arch x86_64',
      MACOSX_DEPLOYMENT_TARGET: MACOS_DEPLOYMENT_TARGET,
      OPENSSL_DIR: opensslRoot,
      OPENSSL_STATIC: '1',
      PATH: runtimePath,
      PYTHONNOUSERSITE: '1',
    },
    pipOnlyBinary,
    pipNoBinary: addPackage(pipNoBinary, 'cryptography'),
    requiresMacX64BuildPrerequisites: true,
  };
}

module.exports = {
  MACOS_DEPLOYMENT_TARGET,
  MACOS_X64_TARGET,
  MINIMUM_RUST_VERSION,
  OPENSSL_SHA256,
  OPENSSL_URL,
  OPENSSL_VERSION,
  addPackage,
  ensurePinnedMacX64OpenSsl,
  resolvePythonRuntimeInstallPolicy,
  runtimeIsMacX64,
  validateOpenSslRoot,
  versionAtLeast,
};
