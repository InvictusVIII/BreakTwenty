#!/usr/bin/env node

const { spawn, spawnSync } = require('node:child_process');
const { randomBytes, randomUUID } = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const {
  ISOLATED_ELECTRON_JOURNEY_FLAG,
  ISOLATED_ELECTRON_RECEIPT_PREFIX,
} = require('../desktop/src/isolatedElectronJourneyProtocol');
const {
  ISOLATED_TEST_MARKER_PREFIX,
  ISOLATED_TEST_ROOT_PREFIX,
  assertPackagedNativeSmokeEnvironment,
} = require('../desktop/src/isolatedTestContract');
const { terminateChildProcess, waitForChildExit } = require('../desktop/src/appLifecycle');
const { resolvePackagedExecutable } = require('./run_packaged_safe_storage_smoke');

const REPOSITORY_ROOT = path.resolve(__dirname, '..');
const DIST_ROOT = path.join(REPOSITORY_ROOT, 'desktop', 'dist', 'electron');
const JOURNEY_TIMEOUT_MS = 180000;
const MAX_PROCESS_OUTPUT_BYTES = 2 * 1024 * 1024;

function requireArgument(argv, name, fallback = '') {
  const index = argv.indexOf(name);
  if (index < 0) return fallback;
  if (index + 1 >= argv.length) throw new Error(`${name} requires a value.`);
  return argv[index + 1];
}

function parseArgs(argv) {
  const build = argv.includes('--build');
  const known = new Set(['--build', '--dist', '--app-name']);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!known.has(argument)) throw new Error(`Unknown argument: ${argument}`);
    if (argument !== '--build') index += 1;
  }
  return {
    build,
    distDir: path.resolve(requireArgument(argv, '--dist', DIST_ROOT)),
    appName: requireArgument(argv, '--app-name', 'BreakTwenty'),
  };
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd || REPOSITORY_ROOT,
    env: options.env || process.env,
    encoding: 'utf8',
    stdio: options.stdio || 'inherit',
    windowsHide: true,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command} ${args.join(' ')} exited with ${result.status}.`);
}

function buildCurrentPlatformPackage() {
  const scripts = {
    linux: 'package:linux:dir',
    win32: 'package:win:dir',
    darwin: 'package:mac:dir',
  };
  const script = scripts[process.platform];
  if (!script) throw new Error(`Packaged native smoke is unsupported on ${process.platform}.`);
  const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  run(npm, ['--prefix', 'desktop', 'run', script]);
}

function reservePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close((error) => (error ? reject(error) : resolve(port)));
    });
  });
}

function isolatedEnvironment({ root, backendPort, frontendPort, marker, appId }) {
  const isolatedRoot = fs.realpathSync(root);
  const environment = {};
  for (const key of [
    'PATH', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'LC_ALL', 'TZ',
    'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS',
  ]) {
    if (process.env[key]) environment[key] = process.env[key];
  }
  const home = path.join(isolatedRoot, 'home');
  const temp = path.join(isolatedRoot, 'tmp');
  const userData = path.join(isolatedRoot, 'user-data');
  const desktopAuth = path.join(isolatedRoot, 'desktop-auth');
  const data = path.join(isolatedRoot, 'data');
  const logs = path.join(data, 'logs');
  const appData = path.join(isolatedRoot, 'app-data');
  const localAppData = path.join(isolatedRoot, 'local-app-data');
  const xdgCache = path.join(isolatedRoot, 'xdg-cache');
  const xdgConfig = path.join(isolatedRoot, 'xdg-config');
  const xdgData = path.join(isolatedRoot, 'xdg-data');
  for (const directory of [
    home,
    temp,
    userData,
    desktopAuth,
    data,
    logs,
    appData,
    localAppData,
    xdgCache,
    xdgConfig,
    xdgData,
  ]) {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  }
  return {
    ...environment,
    APPDATA: appData,
    BREAKTWENTY_BACKEND_API_URL: `http://127.0.0.1:${backendPort}/api`,
    BREAKTWENTY_BACKEND_HEALTH_PROBE_TIMEOUT_MS: '1500',
    BREAKTWENTY_BACKEND_MODE: 'embedded',
    BREAKTWENTY_BACKEND_READY_POLL_MS: '50',
    BREAKTWENTY_BACKEND_READY_TIMEOUT_MS: '90000',
    BREAKTWENTY_BROWSER_RUNTIME_DIR: path.join(desktopAuth, 'browser-runtimes'),
    BREAKTWENTY_DATA_DIR: data,
    BREAKTWENTY_DB_PATH: path.join(data, 'breaktwenty.db'),
    BREAKTWENTY_DESKTOP_AUTH_DIR: desktopAuth,
    BREAKTWENTY_DESKTOP_BUILD_PORT: String(frontendPort),
    BREAKTWENTY_DESKTOP_FRONTEND_MODE: 'build',
    BREAKTWENTY_DESKTOP_PREWARM_BROWSER_RUNTIME: '0',
    BREAKTWENTY_DEVELOPMENT_USER_DATA_DIR: userData,
    BREAKTWENTY_DIAGNOSTIC_DIR: path.join(logs, 'providers'),
    BREAKTWENTY_DISABLE_AUTO_UPDATE: '1',
    BREAKTWENTY_EMBEDDED_BACKEND_PORT: String(backendPort),
    BREAKTWENTY_FRONTEND_READY_POLL_MS: '20',
    BREAKTWENTY_FRONTEND_READY_TIMEOUT_MS: '30000',
    BREAKTWENTY_FRONTEND_URL: `http://127.0.0.1:${frontendPort}`,
    BREAKTWENTY_ISOLATED_HOST_TEMP_ROOT: fs.realpathSync(os.tmpdir()),
    BREAKTWENTY_ISOLATED_JOURNEY_MODE: 'packaged-native',
    BREAKTWENTY_ISOLATED_NETWORK_POLICY: 'loopback-only',
    BREAKTWENTY_ISOLATED_OWNERSHIP_PROOF_KEY: randomBytes(32).toString('base64'),
    BREAKTWENTY_ISOLATED_TEST_APP_ID: appId,
    BREAKTWENTY_ISOLATED_TEST_MARKER: marker,
    BREAKTWENTY_ISOLATED_TEST_ROOT: isolatedRoot,
    BREAKTWENTY_LOG_DIR: logs,
    CI: '1',
    HOME: home,
    LOCALAPPDATA: localAppData,
    NODE_ENV: 'production',
    NO_PROXY: '127.0.0.1,localhost,::1',
    TEMP: temp,
    TMP: temp,
    TMPDIR: temp,
    USERPROFILE: home,
    XDG_CACHE_HOME: xdgCache,
    XDG_CONFIG_HOME: xdgConfig,
    XDG_DATA_HOME: xdgData,
  };
}

function launchCommand(
  executablePath,
  { platform = process.platform, hostEnvironment = process.env } = {},
) {
  const executableArgs = [];
  if (
    platform === 'linux'
    && String(hostEnvironment.BREAKTWENTY_PACKAGED_SMOKE_NO_SANDBOX || '') === '1'
  ) {
    // GitHub's hosted Linux runner cannot use Electron's SUID or namespace sandbox.
    // This switch is restricted to the isolated, loopback-only smoke harness.
    executableArgs.push('--no-sandbox');
  }
  executableArgs.push(ISOLATED_ELECTRON_JOURNEY_FLAG);
  if (platform === 'linux' && !hostEnvironment.DISPLAY) {
    return { command: 'xvfb-run', args: ['-a', executablePath, ...executableArgs] };
  }
  return { command: executablePath, args: executableArgs };
}

function runPackagedExecutable(
  executablePath,
  environment,
  {
    launch = launchCommand(executablePath),
    spawnProcess = spawn,
    terminateProcess = terminateChildProcess,
    waitForExit = waitForChildExit,
  } = {},
) {
  return new Promise((resolve, reject) => {
    const child = spawnProcess(launch.command, launch.args, {
      cwd: path.dirname(executablePath),
      env: environment,
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: process.platform !== 'win32',
      windowsHide: true,
    });
    let stdout = '';
    let stderr = '';
    let exceeded = false;
    let settled = false;
    let exitCode = null;
    let exitSignal = null;
    const finish = async (error, receipt = null, { allowGracefulExit = false } = {}) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (allowGracefulExit) {
        await waitForExit(child, 20000);
      }
      await terminateProcess(child, {
        forceWaitMs: 3000,
        graceMs: 1000,
        processTree: true,
      });
      if (error) reject(error);
      else resolve(receipt);
    };
    const inspectReceipt = () => {
      const receiptLine = stdout.split(/\r?\n/)
        .find((line) => line.startsWith(ISOLATED_ELECTRON_RECEIPT_PREFIX));
      if (!receiptLine) return false;
      try {
        const receipt = JSON.parse(receiptLine.slice(ISOLATED_ELECTRON_RECEIPT_PREFIX.length));
        if (receipt.error) {
          void finish(new Error(`${receipt.error}\n${stderr}`));
        } else {
          void finish(null, receipt, { allowGracefulExit: true });
        }
      } catch (error) {
        void finish(error);
      }
      return true;
    };
    const append = (stream, chunk) => {
      if (stream === 'stdout') stdout += chunk.toString('utf8');
      else stderr += chunk.toString('utf8');
      if (!exceeded && Buffer.byteLength(stdout) + Buffer.byteLength(stderr) > MAX_PROCESS_OUTPUT_BYTES) {
        exceeded = true;
        void finish(new Error('The packaged native smoke exceeded its output limit.'));
        return;
      }
      if (stream === 'stdout') inspectReceipt();
    };
    child.stdout.on('data', (chunk) => append('stdout', chunk));
    child.stderr.on('data', (chunk) => append('stderr', chunk));
    const timer = setTimeout(() => {
      void finish(new Error(
        `The packaged native smoke exceeded ${JOURNEY_TIMEOUT_MS} ms.`
        + `\nstdout:\n${stdout || '(empty)'}`
        + `\nstderr:\n${stderr || '(empty)'}`,
      ));
    }, JOURNEY_TIMEOUT_MS);
    child.once('error', (error) => {
      void finish(error);
    });
    child.once('exit', (code, signal) => {
      exitCode = code;
      exitSignal = signal;
      setTimeout(() => {
        if (settled || inspectReceipt()) return;
        void finish(new Error(
          `Packaged executable returned no smoke receipt (code=${exitCode}, signal=${exitSignal || 'none'}).`
          + `\nstdout:\n${stdout || '(empty)'}`
          + `\nstderr:\n${stderr || '(empty)'}`,
        ));
      }, 250);
    });
  });
}

async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  if (args.build) buildCurrentPlatformPackage();
  const executablePath = resolvePackagedExecutable({
    distDir: args.distDir,
    appName: args.appName,
  });
  if (!fs.existsSync(executablePath) || !fs.statSync(executablePath).isFile()) {
    throw new Error(`Packaged application executable is missing: ${executablePath}`);
  }
  const root = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), ISOLATED_TEST_ROOT_PREFIX)),
  );
  const marker = `${ISOLATED_TEST_MARKER_PREFIX}${randomUUID()}`;
  const appId = `app.breaktwenty.test.${randomUUID()}`;
  try {
    const backendPort = await reservePort();
    const frontendPort = await reservePort();
    const environment = isolatedEnvironment({ root, backendPort, frontendPort, marker, appId });
    assertPackagedNativeSmokeEnvironment(environment);
    const receipt = await runPackagedExecutable(executablePath, environment);
    const dbPath = path.join(root, 'data', 'breaktwenty.db');
    if (!fs.existsSync(dbPath) || fs.statSync(dbPath).size <= 0) {
      throw new Error('The packaged embedded backend did not create its isolated database.');
    }
    process.stdout.write(`${JSON.stringify({
      status: 'passed',
      scenario: 'packaged-native-executable-smoke',
      executable: path.relative(REPOSITORY_ROOT, executablePath),
      routes: receipt.routes,
      assertions: receipt.assertions,
      isolation: {
        temporaryProfile: true,
        temporaryDatabase: true,
        testOnlyIdentity: true,
        updaterDisabled: true,
        providerAuthenticationDisabled: true,
        browserPrewarmDisabled: true,
        externalRendererNetworkBlocked: true,
        ordinaryApplicationProfileUntouched: true,
      },
    }, null, 2)}\n`);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`${error.stack || error.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  buildCurrentPlatformPackage,
  isolatedEnvironment,
  launchCommand,
  parseArgs,
  runPackagedExecutable,
};
