#!/usr/bin/env node

const { spawn } = require('node:child_process');
const { createHmac, randomBytes, randomUUID } = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {
  BACKEND_OWNERSHIP_CONTEXT,
} = require('../desktop/src/backendHealthProbe');
const {
  ISOLATED_TEST_MARKER_PREFIX,
  ISOLATED_TEST_ROOT_PREFIX,
  assertIsolatedTestEnvironment,
} = require('../desktop/src/isolatedTestContract');

const RECEIPT_PREFIX = 'BREAKTWENTY_ISOLATED_STARTUP_RECEIPT ';
const CHILD_TIMEOUT_MS = 15000;
const MAX_CHILD_OUTPUT_BYTES = 512 * 1024;

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.removeListener('error', reject);
      const address = server.address();
      resolve(`http://127.0.0.1:${address.port}`);
    });
  });
}

function close(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

function createBackendStub(ownershipKey, requests) {
  return http.createServer((request, response) => {
    const requestUrl = new URL(request.url || '/', `http://${request.headers.host}`);
    requests.push({
      method: request.method,
      pathname: requestUrl.pathname,
      authorization: Boolean(request.headers.authorization),
    });
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
        startup: { ready: true, source: 'isolated-startup-stub' },
      }));
      return;
    }
    response.writeHead(404, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ detail: 'not found' }));
  });
}

function createFrontendStub(requests) {
  return http.createServer((request, response) => {
    requests.push({ method: request.method, pathname: request.url || '/' });
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end('<!doctype html><title>BreakTwenty isolated startup</title>');
  });
}

function isolatedEnvironment({ root, backendUrl, frontendUrl, ownershipKey, marker, appId }) {
  const environment = {};
  for (const key of ['PATH', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'LC_ALL', 'TZ']) {
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
    BREAKTWENTY_APP_ROOT: path.resolve(__dirname, '..'),
    BREAKTWENTY_BACKEND_API_URL: `${backendUrl}/api`,
    BREAKTWENTY_BACKEND_HEALTH_PROBE_TIMEOUT_MS: '1000',
    BREAKTWENTY_BACKEND_MODE: 'embedded',
    BREAKTWENTY_BACKEND_READY_POLL_MS: '20',
    BREAKTWENTY_BACKEND_READY_TIMEOUT_MS: '2000',
    BREAKTWENTY_DESKTOP_AUTH_DIR: desktopAuth,
    BREAKTWENTY_DESKTOP_FRONTEND_MODE: 'dev',
    BREAKTWENTY_DESKTOP_PREWARM_BROWSER_RUNTIME: '0',
    BREAKTWENTY_DEVELOPMENT_USER_DATA_DIR: userData,
    BREAKTWENTY_FRONTEND_READY_POLL_MS: '20',
    BREAKTWENTY_FRONTEND_READY_TIMEOUT_MS: '2000',
    BREAKTWENTY_FRONTEND_URL: frontendUrl,
    BREAKTWENTY_ISOLATED_HOST_TEMP_ROOT: os.tmpdir(),
    BREAKTWENTY_ISOLATED_NETWORK_POLICY: 'loopback-only',
    BREAKTWENTY_ISOLATED_OWNERSHIP_PROOF_KEY: ownershipKey.toString('base64'),
    BREAKTWENTY_ISOLATED_TEST_APP_ID: appId,
    BREAKTWENTY_ISOLATED_TEST_MARKER: marker,
    BREAKTWENTY_ISOLATED_TEST_ROOT: root,
    CI: '1',
    HOME: home,
    LOCALAPPDATA: path.join(root, 'local-app-data'),
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

function runChild(childPath, environment) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [childPath], {
      cwd: path.resolve(__dirname, '..', 'desktop'),
      env: environment,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    let stdout = '';
    let stderr = '';
    const append = (current, chunk) => {
      const next = current + chunk.toString('utf8');
      if (Buffer.byteLength(next) > MAX_CHILD_OUTPUT_BYTES) {
        child.kill('SIGKILL');
        reject(new Error('The isolated startup child exceeded its output limit.'));
      }
      return next;
    };
    child.stdout.on('data', (chunk) => { stdout = append(stdout, chunk); });
    child.stderr.on('data', (chunk) => { stderr = append(stderr, chunk); });
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`The isolated startup child exceeded ${CHILD_TIMEOUT_MS} ms.`));
    }, CHILD_TIMEOUT_MS);
    child.once('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once('close', (code, signal) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(new Error(
          `The isolated startup child failed (code=${code}, signal=${signal || 'none'}).\n${stderr || stdout}`,
        ));
        return;
      }
      const receiptLine = stdout.split(/\r?\n/).find((line) => line.startsWith(RECEIPT_PREFIX));
      if (!receiptLine) {
        reject(new Error(`The isolated startup child returned no receipt.\n${stderr || stdout}`));
        return;
      }
      resolve(JSON.parse(receiptLine.slice(RECEIPT_PREFIX.length)));
    });
  });
}

async function main() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), ISOLATED_TEST_ROOT_PREFIX));
  const marker = `${ISOLATED_TEST_MARKER_PREFIX}${randomUUID()}`;
  const appId = `app.breaktwenty.test.${randomUUID()}`;
  const ownershipKey = randomBytes(32);
  const backendRequests = [];
  const frontendRequests = [];
  const backendServer = createBackendStub(ownershipKey, backendRequests);
  const frontendServer = createFrontendStub(frontendRequests);
  let backendListening = false;
  let frontendListening = false;
  let summary = null;
  try {
    const backendUrl = await listen(backendServer);
    backendListening = true;
    const frontendUrl = await listen(frontendServer);
    frontendListening = true;
    const environment = isolatedEnvironment({
      root,
      backendUrl,
      frontendUrl,
      ownershipKey,
      marker,
      appId,
    });
    assertIsolatedTestEnvironment(environment);
    const childPath = path.resolve(__dirname, '..', 'desktop', 'src', 'isolatedStartupChild.js');
    const receipt = await runChild(childPath, environment);
    if (!backendRequests.some((request) => request.pathname === '/api/health/ownership')) {
      throw new Error('The isolated startup never proved ownership of its backend stub.');
    }
    if (!backendRequests.some((request) => request.pathname === '/api/health' && request.authorization)) {
      throw new Error('The isolated startup never reached authenticated backend readiness.');
    }
    if (!frontendRequests.some((request) => request.pathname === '/')) {
      throw new Error('The isolated startup never probed its frontend stub.');
    }
    summary = {
      status: 'passed',
      scenario: 'normal-bootstrap-fast-startup',
      marker: receipt.marker,
      assertions: receipt.assertions,
      backendRequests: backendRequests.length,
      frontendRequests: frontendRequests.length,
      isolation: {
        temporaryRoot: true,
        testAppIdentity: true,
        separateLoopbackPorts: true,
        sanitizedEnvironment: true,
        externalNetworkBlocked: true,
        cleanedUp: true,
      },
    };
  } finally {
    ownershipKey.fill(0);
    if (frontendListening) await close(frontendServer);
    if (backendListening) await close(backendServer);
    fs.rmSync(root, { recursive: true, force: true });
  }
  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
