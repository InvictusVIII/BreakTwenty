#!/usr/bin/env node

const { spawn, spawnSync } = require('node:child_process');
const { createHmac, randomBytes, randomUUID } = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const {
  BACKEND_OWNERSHIP_CONTEXT,
} = require('../desktop/src/backendHealthProbe');
const {
  ISOLATED_ELECTRON_JOURNEY_FLAG,
  ISOLATED_ELECTRON_RECEIPT_PREFIX,
} = require('../desktop/src/isolatedElectronJourneyProtocol');
const {
  ISOLATED_TEST_MARKER_PREFIX,
  ISOLATED_TEST_ROOT_PREFIX,
  assertIsolatedTestEnvironment,
} = require('../desktop/src/isolatedTestContract');

const REPOSITORY_ROOT = path.resolve(__dirname, '..');
const DESKTOP_ROOT = path.join(REPOSITORY_ROOT, 'desktop');
const FRONTEND_ROOT = path.join(REPOSITORY_ROOT, 'frontend');
const JOURNEY_TIMEOUT_MS = 45000;
const FRONTEND_TIMEOUT_MS = 15000;
const MAX_PROCESS_OUTPUT_BYTES = 1024 * 1024;
const UPDATE_VISUAL_BASELINES = process.argv.slice(2).includes('--update-baselines');
const VISUAL_FAILURE_ROOT = path.join(REPOSITORY_ROOT, '.breaktwenty-validation', 'visual-diffs');

for (const argument of process.argv.slice(2)) {
  if (argument !== '--update-baselines') throw new Error(`Unknown argument: ${argument}`);
}

function listen(server, port = 0) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => {
      server.removeListener('error', reject);
      resolve(server.address().port);
    });
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

async function reservePort() {
  const server = net.createServer();
  const port = await listen(server);
  await closeServer(server);
  return port;
}

function createBackendStub(ownershipKey, requests) {
  return http.createServer((request, response) => {
    const requestUrl = new URL(request.url || '/', `http://${request.headers.host}`);
    requests.push({
      method: request.method,
      pathname: requestUrl.pathname,
      authorization: Boolean(request.headers.authorization),
    });
    response.setHeader('Access-Control-Allow-Origin', '*');
    response.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type');
    response.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
    response.setHeader('Cache-Control', 'no-store');
    if (request.method === 'OPTIONS') {
      response.writeHead(204);
      response.end();
      return;
    }
    if (requestUrl.pathname === '/api/health/ownership') {
      const challenge = requestUrl.searchParams.get('challenge') || '';
      const endpoint = `http://${request.headers.host}`;
      const proof = createHmac('sha256', ownershipKey)
        .update(`${BACKEND_OWNERSHIP_CONTEXT}${endpoint}:${challenge}`, 'ascii')
        .digest('base64url');
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ proof }));
      return;
    }
    if (requestUrl.pathname === '/api/health') {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({
        app: 'BreakTwenty',
        status: 'ok',
        startup: { ready: true, source: 'isolated-electron-stub' },
      }));
      return;
    }
    if (requestUrl.pathname === '/api/institutions/all') {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end('[]');
      return;
    }
    response.writeHead(501, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ detail: 'Live API access is disabled in the isolated Electron journey.' }));
  });
}

function isolatedEnvironment({ root, backendOrigin, frontendOrigin, ownershipKey, marker, appId }) {
  const environment = {};
  for (const key of [
    'PATH', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'LC_ALL', 'TZ',
    'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS',
  ]) {
    if (process.env[key]) environment[key] = process.env[key];
  }
  const home = path.join(root, 'home');
  const temp = path.join(root, 'tmp');
  const userData = path.join(root, 'user-data');
  const desktopAuth = path.join(root, 'desktop-auth');
  for (const directory of [home, temp, userData, desktopAuth]) {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  }
  return {
    ...environment,
    APPDATA: path.join(root, 'app-data'),
    BREAKTWENTY_APP_ROOT: REPOSITORY_ROOT,
    BREAKTWENTY_BACKEND_API_URL: `${backendOrigin}/api`,
    BREAKTWENTY_BACKEND_HEALTH_PROBE_TIMEOUT_MS: '1000',
    BREAKTWENTY_BACKEND_MODE: 'embedded',
    BREAKTWENTY_BACKEND_READY_POLL_MS: '20',
    BREAKTWENTY_BACKEND_READY_TIMEOUT_MS: '3000',
    BREAKTWENTY_DESKTOP_AUTH_DIR: desktopAuth,
    BREAKTWENTY_DESKTOP_FRONTEND_MODE: 'dev',
    BREAKTWENTY_DESKTOP_PREWARM_BROWSER_RUNTIME: '0',
    BREAKTWENTY_DEVELOPMENT_USER_DATA_DIR: userData,
    BREAKTWENTY_FRONTEND_READY_POLL_MS: '20',
    BREAKTWENTY_FRONTEND_READY_TIMEOUT_MS: '5000',
    BREAKTWENTY_FRONTEND_URL: frontendOrigin,
    BREAKTWENTY_ISOLATED_HOST_TEMP_ROOT: os.tmpdir(),
    BREAKTWENTY_ISOLATED_NETWORK_POLICY: 'loopback-only',
    BREAKTWENTY_ISOLATED_OWNERSHIP_PROOF_KEY: ownershipKey.toString('base64'),
    BREAKTWENTY_ISOLATED_TEST_APP_ID: appId,
    BREAKTWENTY_ISOLATED_TEST_MARKER: marker,
    BREAKTWENTY_ISOLATED_TEST_ROOT: root,
    BREAKTWENTY_VISUAL_BASELINE_MODE: process.platform === 'linux'
      ? (UPDATE_VISUAL_BASELINES ? 'update' : 'verify')
      : 'disabled',
    BREAKTWENTY_VISUAL_FAILURE_DIR: VISUAL_FAILURE_ROOT,
    CI: '1',
    HOME: home,
    LOCALAPPDATA: path.join(root, 'local-app-data'),
    NODE_ENV: 'development',
    NO_PROXY: '127.0.0.1,localhost,::1',
    TEMP: temp,
    TMP: temp,
    TMPDIR: temp,
    USERPROFILE: home,
    XDG_CACHE_HOME: path.join(root, 'xdg-cache'),
    XDG_CONFIG_HOME: path.join(root, 'xdg-config'),
    XDG_DATA_HOME: path.join(root, 'xdg-data'),
  };
}

function runReadOnlyFrontendChecks() {
  const scripts = [
    ['scripts/syncProviderCatalog.js', '--check'],
    ['scripts/syncLoadingScreen.js', '--check'],
    ['scripts/generate-theme-css.js', '--check'],
  ];
  for (const args of scripts) {
    const result = spawnSync(process.execPath, args, {
      cwd: FRONTEND_ROOT,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    if (result.status !== 0) {
      throw new Error(
        `Frontend prerequisite failed: node ${args.join(' ')}\n${result.stderr || result.stdout}`,
      );
    }
  }
}

function captureProcessOutput(child, label) {
  const output = { stdout: '', stderr: '' };
  let exceeded = false;
  const append = (stream, chunk) => {
    output[stream] += chunk.toString('utf8');
    if (!exceeded && Buffer.byteLength(output.stdout) + Buffer.byteLength(output.stderr) > MAX_PROCESS_OUTPUT_BYTES) {
      exceeded = true;
      child.kill('SIGKILL');
    }
  };
  child.stdout.on('data', (chunk) => append('stdout', chunk));
  child.stderr.on('data', (chunk) => append('stderr', chunk));
  return {
    output,
    assertWithinLimit() {
      if (exceeded) throw new Error(`${label} exceeded its output limit.`);
    },
  };
}

async function terminateChild(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  await Promise.race([
    new Promise((resolve) => child.once('close', resolve)),
    new Promise((resolve) => setTimeout(resolve, 2000)),
  ]);
  if (child.exitCode === null && child.signalCode === null) {
    child.kill('SIGKILL');
    await Promise.race([
      new Promise((resolve) => child.once('close', resolve)),
      new Promise((resolve) => setTimeout(resolve, 2000)),
    ]);
  }
}

async function waitForFrontend(frontendOrigin, child, captured) {
  const deadline = Date.now() + FRONTEND_TIMEOUT_MS;
  while (Date.now() < deadline) {
    captured.assertWithinLimit();
    if (child.exitCode !== null) {
      throw new Error(`Vite exited before becoming ready.\n${captured.output.stderr || captured.output.stdout}`);
    }
    try {
      const response = await fetch(frontendOrigin, { signal: AbortSignal.timeout(1000) });
      if (response.ok) return;
    } catch (_error) {
      // The bounded readiness loop owns retries.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Vite did not become ready within ${FRONTEND_TIMEOUT_MS} ms.\n${captured.output.stderr}`);
}

function runElectron(executable, environment) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [DESKTOP_ROOT, ISOLATED_ELECTRON_JOURNEY_FLAG], {
      cwd: DESKTOP_ROOT,
      env: environment,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const captured = captureProcessOutput(child, 'Electron journey');
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`The isolated Electron journey exceeded ${JOURNEY_TIMEOUT_MS} ms.`));
    }, JOURNEY_TIMEOUT_MS);
    child.once('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once('close', (code, signal) => {
      clearTimeout(timer);
      try {
        captured.assertWithinLimit();
        const receiptLine = captured.output.stdout
          .split(/\r?\n/)
          .find((line) => line.startsWith(ISOLATED_ELECTRON_RECEIPT_PREFIX));
        if (!receiptLine) {
          throw new Error(
            `Electron returned no journey receipt (code=${code}, signal=${signal || 'none'}).\n`
            + (captured.output.stderr || captured.output.stdout),
          );
        }
        const receipt = JSON.parse(receiptLine.slice(ISOLATED_ELECTRON_RECEIPT_PREFIX.length));
        if (code !== 0 || receipt.error) {
          throw new Error(
            `${receipt.error || `Electron journey failed with code ${code}.`}\n`
            + `Journey diagnostics: ${JSON.stringify({
              rendererErrors: receipt.rendererErrors,
              blockedRequests: receipt.blockedRequests,
              rendererSnapshot: receipt.rendererSnapshot,
            }, null, 2)}`,
          );
        }
        resolve(receipt);
      } catch (error) {
        reject(error);
      }
    });
  });
}

async function main() {
  runReadOnlyFrontendChecks();
  fs.rmSync(VISUAL_FAILURE_ROOT, { recursive: true, force: true });
  const root = fs.mkdtempSync(path.join(os.tmpdir(), ISOLATED_TEST_ROOT_PREFIX));
  const marker = `${ISOLATED_TEST_MARKER_PREFIX}${randomUUID()}`;
  const appId = `app.breaktwenty.test.${randomUUID()}`;
  const ownershipKey = randomBytes(32);
  const backendRequests = [];
  const backendServer = createBackendStub(ownershipKey, backendRequests);
  let backendListening = false;
  let viteChild = null;
  let summary = null;
  try {
    const backendPort = await listen(backendServer);
    backendListening = true;
    const frontendPort = await reservePort();
    const backendOrigin = `http://127.0.0.1:${backendPort}`;
    const frontendOrigin = `http://127.0.0.1:${frontendPort}`;
    const environment = isolatedEnvironment({
      root,
      backendOrigin,
      frontendOrigin,
      ownershipKey,
      marker,
      appId,
    });
    assertIsolatedTestEnvironment(environment);

    const vitePath = path.join(FRONTEND_ROOT, 'node_modules', 'vite', 'bin', 'vite.js');
    if (!fs.existsSync(vitePath)) throw new Error('The installed frontend Vite runtime is missing.');
    viteChild = spawn(process.execPath, [vitePath, '--host', '127.0.0.1', '--port', String(frontendPort), '--strictPort'], {
      cwd: FRONTEND_ROOT,
      env: environment,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const viteCaptured = captureProcessOutput(viteChild, 'Vite');
    await waitForFrontend(frontendOrigin, viteChild, viteCaptured);

    const electronExecutable = require('../desktop/node_modules/electron');
    const receipt = await runElectron(electronExecutable, environment);
    const unexpectedBackendRequests = backendRequests.filter(
      (request) => !['/api/health/ownership', '/api/health', '/api/institutions/all'].includes(request.pathname),
    );
    if (unexpectedBackendRequests.length > 0) {
      throw new Error(`The renderer escaped Promo fixtures: ${JSON.stringify(unexpectedBackendRequests)}`);
    }
    if (!backendRequests.some((request) => request.pathname === '/api/health/ownership')) {
      throw new Error('Electron never proved ownership of the isolated backend stub.');
    }
    if (!backendRequests.some((request) => request.pathname === '/api/health' && request.authorization)) {
      throw new Error('Electron never reached authenticated backend readiness.');
    }
    summary = {
      status: 'passed',
      scenario: 'real-isolated-electron-routes-geometry-and-visuals',
      routes: receipt.routes,
      visuals: receipt.visuals,
      assertions: receipt.assertions,
      backendRequests: backendRequests.length,
      isolation: {
        temporaryRoot: true,
        testAppIdentity: true,
        nonpersistentRendererPartition: true,
        syntheticPromoData: true,
        separateLoopbackPorts: true,
        sanitizedEnvironment: true,
        externalNetworkBlocked: true,
        providerAuthenticationDisabled: true,
        ordinaryApplicationProfileUntouched: true,
        cleanedUp: true,
      },
    };
  } finally {
    ownershipKey.fill(0);
    await terminateChild(viteChild);
    if (backendListening) await closeServer(backendServer);
    fs.rmSync(root, { recursive: true, force: true });
  }
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
