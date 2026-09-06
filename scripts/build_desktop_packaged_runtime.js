#!/usr/bin/env node

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { resolvePythonRuntimeInstallPolicy } = require('./python_runtime_install_policy');

const REPO_ROOT = path.resolve(__dirname, '..');
const VISIBLE_AUTH_FILES = [
  'amex_visible_auth.py',
  'bmo_visible_auth.py',
  'browser_timezone.py',
  'cibc_visible_auth.py',
  'desktop_browser_runtime.py',
  'desktop_visible_auth_requirements.lock',
  'desktop_visible_auth_requirements.txt',
  'eqbank_visible_auth.py',
  'ibkr_visible_auth.py',
  'national_visible_auth.py',
  'moomoo_oauth_visible_auth.py',
  'rbc_visible_auth.py',
  'scotiabank_visible_auth.py',
  'tangerine_visible_auth.py',
  'td_visible_auth.py',
  'visible_auth_common.py',
];
let outputRoot = process.env.BREAKTWENTY_DESKTOP_PACKAGE_RUNTIME_DIR
  || path.join(REPO_ROOT, 'desktop', 'dist', 'packaged-runtime', 'resources');
let frontendBuildDir = path.join(REPO_ROOT, 'frontend', 'build');
let buildFrontend = envFlag(process.env.BREAKTWENTY_PACKAGE_BUILD_FRONTEND);
let packageBackendMode = normalizeBackendMode(process.env.BREAKTWENTY_PACKAGE_BACKEND_MODE || 'embedded');
let installPython = envFlag(process.env.BREAKTWENTY_PACKAGE_INSTALL_PYTHON);
let pythonSource = process.env.BREAKTWENTY_PACKAGE_PYTHON_SOURCE || '';
let pipOnlyBinary = process.env.BREAKTWENTY_PACKAGE_PIP_ONLY_BINARY || '';
let pipNoBinary = process.env.BREAKTWENTY_PACKAGE_PIP_NO_BINARY || '';
let updateHelperOnly = false;
let tmpOutput = '';

function log(message) {
  console.log(`[BreakTwenty] ${message}`);
}

function usage() {
  console.log(`Usage: ${path.basename(process.argv[1])} [--output PATH] [--update-helper-only] [--build-frontend] [--backend-mode docker|embedded] [--install-python] [--python-source PATH] [--pip-only-binary VALUE] [--pip-no-binary VALUE]`);
}

function envFlag(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value || '').trim().toLowerCase());
}

function normalizeBackendMode(value) {
  return String(value || '').trim().toLowerCase() === 'docker' ? 'docker' : 'embedded';
}

function npmCommand() {
  return process.platform === 'win32' ? 'npm.cmd' : 'npm';
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

function runFrontendBuild() {
  const frontendDir = path.join(REPO_ROOT, 'frontend');
  if (process.platform === 'win32') {
    run(process.env.ComSpec || 'cmd.exe', ['/d', '/c', 'npm.cmd', '--prefix', frontendDir, 'run', 'build']);
    return;
  }
  run(npmCommand(), ['--prefix', frontendDir, 'run', 'build']);
}

function parseArgs(argv) {
  for (let index = 0; index < argv.length;) {
    const arg = argv[index];
    switch (arg) {
      case '--output':
        outputRoot = requireValue(argv, index, arg);
        index += 2;
        break;
      case '--build-frontend':
        buildFrontend = true;
        index += 1;
        break;
      case '--update-helper-only':
        updateHelperOnly = true;
        index += 1;
        break;
      case '--backend-mode':
        packageBackendMode = normalizeBackendMode(requireValue(argv, index, arg));
        index += 2;
        break;
      case '--docker-backend':
        packageBackendMode = 'docker';
        index += 1;
        break;
      case '--embedded-backend':
        packageBackendMode = 'embedded';
        index += 1;
        break;
      case '--install-python':
        installPython = true;
        index += 1;
        break;
      case '--python-source':
        pythonSource = requireValue(argv, index, arg);
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

  packageBackendMode = normalizeBackendMode(packageBackendMode);
  if (!updateHelperOnly && packageBackendMode === 'docker' && pythonSource) {
    throw new Error('--python-source is only valid with --embedded-backend.');
  }
  if (!updateHelperOnly && packageBackendMode === 'docker' && installPython) {
    throw new Error('--install-python is only valid with --embedded-backend.');
  }
}

function requireValue(argv, index, flag) {
  if (index + 1 >= argv.length) {
    usage();
    throw new Error(`${flag} requires a value.`);
  }
  return argv[index + 1];
}

function runFrontendBuildIfRequested() {
  if (buildFrontend) {
    log('Building frontend...');
    runFrontendBuild();
  }

  if (!fileExists(path.join(frontendBuildDir, 'index.html'))) {
    throw new Error(
      `Frontend build is missing at ${frontendBuildDir}. Run npm --prefix ${path.join(REPO_ROOT, 'frontend')} run build or pass --build-frontend.`,
    );
  }
}

function copyBackend() {
  const backendDir = path.join(tmpOutput, 'backend');
  ensureDir(backendDir);
  fs.copyFileSync(path.join(REPO_ROOT, 'backend', 'alembic.ini'), path.join(backendDir, 'alembic.ini'));
  fs.copyFileSync(path.join(REPO_ROOT, 'backend', 'requirements.txt'), path.join(backendDir, 'requirements.txt'));
  fs.copyFileSync(path.join(REPO_ROOT, 'backend', 'requirements-bootstrap.lock'), path.join(backendDir, 'requirements-bootstrap.lock'));
  fs.copyFileSync(path.join(REPO_ROOT, 'backend', 'requirements-macos-x64-build.lock'), path.join(backendDir, 'requirements-macos-x64-build.lock'));
  fs.copyFileSync(path.join(REPO_ROOT, 'backend', 'requirements.lock'), path.join(backendDir, 'requirements.lock'));
  fs.copyFileSync(path.join(REPO_ROOT, 'backend', 'runtime-policy.json'), path.join(backendDir, 'runtime-policy.json'));
  fs.cpSync(path.join(REPO_ROOT, 'backend', 'app'), path.join(backendDir, 'app'), { recursive: true });
  fs.cpSync(path.join(REPO_ROOT, 'backend', 'alembic'), path.join(backendDir, 'alembic'), { recursive: true });
}

function copyFrontend() {
  const frontendDir = path.join(tmpOutput, 'frontend');
  ensureDir(frontendDir);
  fs.cpSync(frontendBuildDir, path.join(frontendDir, 'build'), { recursive: true });
}

function copyConfig() {
  const configDir = path.join(tmpOutput, 'config');
  ensureDir(configDir);
  for (const fileName of ['provider_catalog.json', 'loading-screen.json', 'loading-screen.css']) {
    fs.copyFileSync(
      path.join(REPO_ROOT, 'config', fileName),
      path.join(configDir, fileName),
    );
  }
  fs.copyFileSync(
    path.join(REPO_ROOT, 'frontend', 'src', 'theme', 'tokens.generated.css'),
    path.join(configDir, 'loading-screen-theme.css'),
  );
}

function copyVisibleAuthScripts() {
  const scriptsDir = path.join(tmpOutput, 'scripts');
  ensureDir(scriptsDir);
  for (const file of VISIBLE_AUTH_FILES) {
    fs.copyFileSync(path.join(REPO_ROOT, 'scripts', file), path.join(scriptsDir, file));
  }
}

function findWindowsFrameworkCompiler() {
  const windowsRoot = process.env.SystemRoot || process.env.WINDIR || 'C:\\Windows';
  const candidates = [
    process.env.BREAKTWENTY_WINDOWS_CSC,
    path.join(windowsRoot, 'Microsoft.NET', 'Framework64', 'v4.0.30319', 'csc.exe'),
    path.join(windowsRoot, 'Microsoft.NET', 'Framework', 'v4.0.30319', 'csc.exe'),
  ].filter(Boolean);
  return candidates.find(fileExists) || '';
}

function buildWindowsUpdateHelper() {
  if (process.platform !== 'win32') {
    return;
  }
  const compiler = findWindowsFrameworkCompiler();
  if (!compiler) {
    throw new Error('The .NET Framework C# compiler required for the Windows update helper is missing.');
  }
  const sourcePath = path.join(
    REPO_ROOT,
    'desktop',
    'windows-update-helper',
    'Program.cs',
  );
  const helperDir = path.join(tmpOutput, 'desktop', 'update-helper');
  const outputPath = path.join(helperDir, 'BreakTwentyUpdateHelper.exe');
  const markPath = path.join(REPO_ROOT, 'frontend', 'public', 'assets', 'brand', 'breaktwenty-mark-dark.png');
  const wordmarkPath = path.join(REPO_ROOT, 'frontend', 'public', 'assets', 'brand', 'breaktwenty-wordmark-dark-320.png');
  ensureDir(helperDir);
  log('Building native Windows update progress helper...');
  run(compiler, [
    '/nologo',
    '/target:winexe',
    '/platform:x64',
    '/optimize+',
    '/debug-',
    `/out:${outputPath}`,
    `/win32icon:${path.join(REPO_ROOT, 'desktop', 'assets', 'icons', 'window-icon.ico')}`,
    `/resource:${markPath},BreakTwenty.UpdateHelper.Mark.png`,
    `/resource:${wordmarkPath},BreakTwenty.UpdateHelper.Wordmark.png`,
    '/reference:System.dll',
    '/reference:System.Drawing.dll',
    '/reference:System.Windows.Forms.dll',
    sourcePath,
  ]);
  if (!fileExists(outputPath)) {
    throw new Error('The native Windows update helper was not produced.');
  }
}

function buildLinuxUpdateHelper() {
  if (process.platform !== 'linux') {
    return;
  }
  const helperDir = path.join(tmpOutput, 'desktop', 'update-helper');
  const outputPath = path.join(helperDir, 'BreakTwentyUpdateHelper');
  const gtkFlags = run('pkg-config', ['--cflags', '--libs', 'gtk+-3.0'], { stdio: 'pipe' })
    .stdout.trim().split(/\s+/).filter(Boolean);
  ensureDir(helperDir);
  log('Building native Linux update progress helper...');
  run('gcc', [
    '-std=c11',
    '-D_POSIX_C_SOURCE=200809L',
    '-O2',
    '-Wall',
    '-Wextra',
    '-Werror',
    '-o',
    outputPath,
    path.join(REPO_ROOT, 'desktop', 'linux-update-helper', 'main.c'),
    ...gtkFlags,
  ]);
  fs.chmodSync(outputPath, 0o700);
  if (!fileExists(outputPath)) {
    throw new Error('The native Linux update helper was not produced.');
  }
}

function buildMacosUpdateHelper() {
  if (process.platform !== 'darwin') {
    return;
  }
  const helperDir = path.join(tmpOutput, 'desktop', 'update-helper');
  const outputPath = path.join(helperDir, 'BreakTwentyUpdateHelper');
  ensureDir(helperDir);
  log('Building native macOS update progress helper...');
  run('xcrun', [
    'swiftc',
    '-O',
    '-framework',
    'AppKit',
    '-framework',
    'Foundation',
    '-o',
    outputPath,
    path.join(REPO_ROOT, 'desktop', 'macos-update-helper', 'main.swift'),
  ]);
  fs.chmodSync(outputPath, 0o700);
  if (!fileExists(outputPath)) {
    throw new Error('The native macOS update helper was not produced.');
  }
}

function pythonBinForRuntime(pythonDir) {
  const candidates = [
    path.join(pythonDir, 'bin', 'python3'),
    path.join(pythonDir, 'bin', 'python'),
    path.join(pythonDir, 'python.exe'),
  ];
  try {
    const binDir = path.join(pythonDir, 'bin');
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
    throw new Error(`No Python executable found under ${pythonDir}.`);
  }
  return pythonBin;
}

function runPython(pythonBin, args, baseEnv = process.env) {
  run(pythonBin, args, {
    env: {
      ...baseEnv,
      PYTHONNOUSERSITE: '1',
    },
  });
}

async function installBackendPythonDependencies(pythonDir) {
  const pythonBin = pythonBinForRuntime(pythonDir);
  const policy = await resolvePythonRuntimeInstallPolicy({
    pythonBin,
    pipOnlyBinary,
    pipNoBinary,
    opensslCacheRoot: path.join(REPO_ROOT, 'desktop', 'dist', 'python-build-cache'),
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

  runPython(pythonBin, ['-s', '-m', 'ensurepip', '--upgrade'], policy.env);
  runPython(pythonBin, [
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
  ], policy.env);
  if (policy.requiresMacX64BuildPrerequisites) {
    log('Installing hash-verified Intel macOS cryptography build prerequisites...');
    runPython(pythonBin, [
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
    ], policy.env);
  }
  runPython(pythonBin, [
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
  ], policy.env);
  if (policy.requiresMacX64BuildPrerequisites) {
    runPython(pythonBin, ['-s', '-m', 'pip', 'uninstall', '--yes', 'maturin'], policy.env);
  }
  const noticeArgs = [
    path.join(REPO_ROOT, 'backend', 'scripts', 'generate_python_runtime_notices.py'),
    '--output-dir',
    path.join(tmpOutput, 'third-party-licenses', 'python-runtime'),
    '--fail-on-missing-license',
  ];
  if (policy.requiresMacX64BuildPrerequisites) {
    noticeArgs.push('--include-macos-x64-static-openssl');
  }
  runPython(pythonBin, noticeArgs, policy.env);
}

function findHostPython() {
  for (const candidate of (process.platform === 'win32' ? ['python.exe', 'python'] : ['python3', 'python'])) {
    const status = spawnSync(candidate, ['--version'], {
      encoding: 'utf8',
      stdio: 'ignore',
      windowsHide: true,
    });
    if (!status.error && status.status === 0) {
      return candidate;
    }
  }
  return '';
}

async function installPythonRuntime() {
  const pythonDir = path.join(tmpOutput, 'python');
  if (pythonSource) {
    const source = path.resolve(pythonSource);
    if (!dirExists(source)) {
      throw new Error(`Python source directory is missing: ${source}`);
    }
    log(`Copying Python runtime from ${source}...`);
    fs.cpSync(source, pythonDir, {
      recursive: true,
      verbatimSymlinks: true,
    });
    await installBackendPythonDependencies(pythonDir);
    return;
  }

  if (!installPython) {
    ensureDir(pythonDir);
    fs.writeFileSync(
      path.join(pythonDir, 'README.txt'),
      [
        'Package a Python runtime here for a fully self-contained no-Docker build.',
        'Use scripts/build_desktop_packaged_runtime.js --install-python for a local managed venv.',
        'Use --python-source PATH to copy a prebuilt OS-specific Python runtime.',
        '',
      ].join('\n'),
      'utf8',
    );
    return;
  }

  const hostPython = findHostPython();
  if (!hostPython) {
    throw new Error('python3 or python is required to build the managed packaged backend runtime.');
  }

  log('Creating managed packaged backend Python runtime...');
  run(hostPython, ['-m', 'venv', '--copies', pythonDir]);
  await installBackendPythonDependencies(pythonDir);
}

function verifySqlcipherRuntime() {
  if (packageBackendMode !== 'embedded') {
    return;
  }
  const pythonDir = path.join(tmpOutput, 'python');
  const readmePlaceholder = path.join(pythonDir, 'README.txt');
  if (fileExists(readmePlaceholder)) {
    return;
  }
  const pythonBin = pythonBinForRuntime(pythonDir);
  log('Verifying packaged SQLCipher runtime...');
  runPython(
    pythonBin,
    [path.join(REPO_ROOT, 'scripts', 'verify_sqlcipher_runtime.py')],
  );
}

function verifyPackagedEncryptedBackendStartup() {
  if (packageBackendMode !== 'embedded') {
    return;
  }
  const pythonDir = path.join(tmpOutput, 'python');
  if (fileExists(path.join(pythonDir, 'README.txt'))) {
    return;
  }

  const runtimeRoot = fs.mkdtempSync(
    path.join(os.tmpdir(), 'breaktwenty-packaged-sqlcipher-'),
  );
  const dataDir = path.join(runtimeRoot, 'data');
  const databasePath = path.join(dataDir, 'breaktwenty.db');
  const backendRoot = path.join(tmpOutput, 'backend');
  const pythonBin = pythonBinForRuntime(pythonDir);
  const databaseKey = crypto.randomBytes(32);
  const appEncryptionKey = crypto.randomBytes(32);
  const desktopLaunchToken = crypto.randomBytes(32);
  const rendererLaunchToken = crypto.randomBytes(32);
  const secretMarkers = [
    databaseKey.toString('base64'),
    appEncryptionKey.toString('base64'),
    desktopLaunchToken.toString('base64'),
    rendererLaunchToken.toString('base64'),
  ];
  const env = {
    ...process.env,
    BREAKTWENTY_DATA_DIR: dataDir,
    BREAKTWENTY_DB_PATH: databasePath,
    BREAKTWENTY_ENABLE_NIGHTLY_SYNC: '0',
    BREAKTWENTY_KEY_BOOTSTRAP: 'stdin-v2',
    BREAKTWENTY_RESOURCE_ROOT: tmpOutput,
    DATABASE_URL: `sqlite+sqlcipher_aiosqlite:///${databasePath.split(path.sep).join('/')}`,
    PROVIDER_CATALOG_PATH: path.join(tmpOutput, 'config', 'provider_catalog.json'),
    PYTHONNOUSERSITE: '1',
    PYTHONPATH: backendRoot,
  };
  delete env.BREAKTWENTY_DATABASE_ENCRYPTION_KEY;
  delete env.BREAKTWENTY_APP_ENCRYPTION_KEY;

  function runKeyed(args, label) {
    const payload = Buffer.from(`${JSON.stringify({
      version: 2,
      databaseKey: secretMarkers[0],
      appEncryptionKey: secretMarkers[1],
      desktopLaunchToken: secretMarkers[2],
      rendererLaunchToken: secretMarkers[3],
    })}\n`, 'utf8');
    try {
      const status = spawnSync(pythonBin, args, {
        cwd: backendRoot,
        env,
        input: payload,
        encoding: 'utf8',
        timeout: 120000,
        maxBuffer: 4 * 1024 * 1024,
        windowsHide: true,
      });
      const output = `${status.stdout || ''}\n${status.stderr || ''}`;
      if (secretMarkers.some((marker) => output.includes(marker))) {
        throw new Error(`Packaged ${label} exposed key material in process output.`);
      }
      if (status.error || status.status !== 0) {
        throw new Error(`Packaged ${label} failed.`);
      }
      return String(status.stdout || '');
    } finally {
      payload.fill(0);
    }
  }

  const startupCode = [
    'import asyncio',
    'from app.main import app',
    'async def verify():',
    '    async with app.router.lifespan_context(app):',
    '        pass',
    'asyncio.run(verify())',
  ].join('\n');
  const plainSqliteProbe = [
    'import sqlite3, sys',
    'connection = sqlite3.connect(sys.argv[1])',
    'try:',
    '    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()',
    'except sqlite3.DatabaseError:',
    '    raise SystemExit(0)',
    'raise SystemExit(2)',
  ].join('\n');

  try {
    fs.mkdirSync(dataDir, { recursive: true });
    log('Verifying packaged encrypted backend create, restart, and reopen...');
    runKeyed(
      ['-m', 'alembic', '-c', path.join(backendRoot, 'alembic.ini'), 'upgrade', 'head'],
      'Alembic upgrade',
    );
    runKeyed(['-c', startupCode], 'backend startup');
    const currentRevision = runKeyed(
      ['-m', 'alembic', '-c', path.join(backendRoot, 'alembic.ini'), 'current'],
      'Alembic reopen',
    );
    if (!currentRevision.trim()) {
      throw new Error('Packaged Alembic reopen did not report a revision.');
    }
    runKeyed(['-c', startupCode], 'backend restart');
    const header = fs.readFileSync(databasePath).subarray(0, 16);
    if (header.equals(Buffer.from('SQLite format 3\0', 'binary'))) {
      throw new Error('Packaged backend created a plaintext SQLite database.');
    }
    const plainProbe = spawnSync(
      pythonBin,
      ['-c', plainSqliteProbe, databasePath],
      {
        cwd: backendRoot,
        env: {
          ...env,
          BREAKTWENTY_KEY_BOOTSTRAP: '',
        },
        encoding: 'utf8',
        timeout: 30000,
        windowsHide: true,
      },
    );
    if (plainProbe.error || plainProbe.status !== 0) {
      throw new Error('Ordinary SQLite did not reject the packaged encrypted database.');
    }
  } finally {
    databaseKey.fill(0);
    appEncryptionKey.fill(0);
    desktopLaunchToken.fill(0);
    rendererLaunchToken.fill(0);
    removePath(runtimeRoot);
  }
}

function verifyPackagedProviderRuntime() {
  if (packageBackendMode !== 'embedded') {
    return;
  }
  const pythonDir = path.join(tmpOutput, 'python');
  if (fileExists(path.join(pythonDir, 'README.txt'))) {
    return;
  }
  const backendRoot = path.join(tmpOutput, 'backend');
  const pythonBin = pythonBinForRuntime(pythonDir);
  const pythonPath = [backendRoot, path.join(REPO_ROOT, 'backend')].join(path.delimiter);
  log('Verifying provider contracts with the packaged Python runtime...');
  runPython(
    pythonBin,
    [
      path.join(REPO_ROOT, 'scripts', 'verify_packaged_provider_runtime.py'),
      '--resource-root',
      tmpOutput,
      '--source-backend',
      path.join(REPO_ROOT, 'backend'),
    ],
    {
      ...process.env,
      PROVIDER_CATALOG_PATH: path.join(tmpOutput, 'config', 'provider_catalog.json'),
      PYTHONPATH: pythonPath,
    },
  );
}

function writePythonRuntimeFingerprint() {
  if (packageBackendMode !== 'embedded') {
    return;
  }
  const pythonDir = path.join(tmpOutput, 'python');
  if (fileExists(path.join(pythonDir, 'README.txt'))) {
    return;
  }
  const pythonBin = pythonBinForRuntime(pythonDir);
  runPython(
    pythonBin,
    [
      path.join(REPO_ROOT, 'scripts', 'generate_python_runtime_fingerprint.py'),
      '--resource-root',
      tmpOutput,
      '--output',
      path.join(tmpOutput, 'python-runtime-fingerprint.json'),
    ],
    {
      ...process.env,
      PYTHONPATH: '',
    },
  );
}

function writeManifest() {
  const manifest = {
    resourceLayoutVersion: 1,
    createdAt: new Date().toISOString(),
    backendMode: packageBackendMode,
    backendDistribution: packageBackendMode === 'embedded'
      ? (pythonSource || installPython ? 'packaged-python' : 'python-placeholder')
      : 'external-docker',
    frontendMode: 'build',
    frontendBuildRoot: 'frontend/build',
    providerCatalog: 'config/provider_catalog.json',
    loadingScreenConfig: 'config/loading-screen.json',
    loadingScreenStyles: 'config/loading-screen.css',
    loadingScreenThemeStyles: 'config/loading-screen-theme.css',
    visibleAuthScripts: 'scripts',
    visibleAuthRequirements: 'scripts/desktop_visible_auth_requirements.lock',
    databaseEncryption: {
      binding: 'sqlcipher3',
      bindingVersion: '0.6.2',
      sqlcipherVersion: '4.12.0 community',
      keyTransport: 'electron-child-stdin-v2',
    },
  };
  if (packageBackendMode === 'embedded') {
    manifest.backendRoot = 'backend';
    manifest.pythonRuntime = 'python';
    manifest.pythonRuntimeFingerprint = 'python-runtime-fingerprint.json';
    manifest.pythonRuntimeSbom = 'third-party-licenses/python-runtime/python-runtime-sbom.cdx.json';
    manifest.pythonRuntimeNotices = 'third-party-licenses/python-runtime/PYTHON_RUNTIME_NOTICES.md';
  } else {
    manifest.defaultBackendApiUrl = 'http://localhost:8000/api';
  }
  fs.writeFileSync(
    path.join(tmpOutput, 'packaged-runtime.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
    'utf8',
  );
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

function publishOutput() {
  ensureDir(path.dirname(outputRoot));
  removePath(outputRoot);
  fs.renameSync(tmpOutput, outputRoot);
  tmpOutput = '';
}

async function main() {
  parseArgs(process.argv.slice(2));
  outputRoot = path.resolve(outputRoot);
  frontendBuildDir = path.resolve(frontendBuildDir);
  tmpOutput = `${outputRoot}.tmp.${process.pid}`;
  removePath(tmpOutput);
  ensureDir(tmpOutput);

  try {
    if (updateHelperOnly) {
      buildWindowsUpdateHelper();
      buildLinuxUpdateHelper();
      buildMacosUpdateHelper();
    } else {
      runFrontendBuildIfRequested();
      if (packageBackendMode === 'embedded') {
        copyBackend();
      }
      copyFrontend();
      copyConfig();
      copyVisibleAuthScripts();
      buildWindowsUpdateHelper();
      buildLinuxUpdateHelper();
      buildMacosUpdateHelper();
      if (packageBackendMode === 'embedded') {
        await installPythonRuntime();
        verifySqlcipherRuntime();
        verifyPackagedEncryptedBackendStartup();
        verifyPackagedProviderRuntime();
        removePythonCache(tmpOutput);
        writePythonRuntimeFingerprint();
      }
      writeManifest();
      removePythonCache(tmpOutput);
    }
    publishOutput();
    log(updateHelperOnly
      ? `Native update helper is ready at ${outputRoot}`
      : `Packaged runtime resources are ready at ${outputRoot}`);
  } finally {
    removePath(tmpOutput);
  }
}

main().catch((error) => {
  console.error(`[BreakTwenty] ${error.message}`);
  process.exitCode = 1;
});
