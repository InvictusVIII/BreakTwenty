#!/usr/bin/env node

const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const API_ORIGIN = 'https://webapi.moomoo.com';
const DEFAULT_CALLBACK_PORT = 60355;
const DEFAULT_TIMEOUT_SECONDS = 300;

class ProbeError extends Error {
  constructor(message, { stage = 'unknown', status = null, code = null } = {}) {
    super(message);
    this.name = 'ProbeError';
    this.stage = stage;
    this.status = status;
    this.code = code;
  }
}

function usage() {
  return `BreakTwenty Moomoo cloud OAuth probe

Usage:
  npm --prefix desktop run smoke:moomoo-cloud -- [options]

Options:
  --currency CODE       Funds display currency (default: USD).
  --port NUMBER         Local OAuth callback port (default: 60355).
  --timeout SECONDS     Browser authorization timeout (default: 300).
  --state-dir PATH      Override the isolated probe state/diagnostic directory.
  --no-open             Print the authorization URL without opening a browser.
  --fresh-client        Register a new OAuth client instead of reusing the cached client ID.
  --help                Show this help.

The probe never asks for or receives your Moomoo password. Moomoo handles sign-in and verification in the browser.
It performs read-only calls for authorized accounts, funds, positions, recent fills, and recent orders, then verifies
refresh-token access. It never places, changes, or cancels an order. OAuth tokens and authorization codes remain in
memory and are not written to disk.`;
}

function parseArgs(argv) {
  const options = {
    currency: 'USD',
    freshClient: false,
    help: false,
    noOpen: false,
    port: DEFAULT_CALLBACK_PORT,
    stateDir: '',
    timeoutSeconds: DEFAULT_TIMEOUT_SECONDS,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--help' || argument === '-h') {
      options.help = true;
    } else if (argument === '--no-open') {
      options.noOpen = true;
    } else if (argument === '--fresh-client') {
      options.freshClient = true;
    } else if (['--currency', '--port', '--state-dir', '--timeout'].includes(argument)) {
      const value = argv[index + 1];
      if (!value || value.startsWith('--')) throw new Error(`${argument} requires a value`);
      index += 1;
      if (argument === '--currency') options.currency = String(value).trim().toUpperCase();
      if (argument === '--port') options.port = Number(value);
      if (argument === '--state-dir') options.stateDir = value;
      if (argument === '--timeout') options.timeoutSeconds = Number(value);
    } else {
      throw new Error(`Unknown option: ${argument}`);
    }
  }
  if (!/^[A-Z]{3}$/.test(options.currency)) throw new Error('--currency must be a three-letter ISO currency code');
  if (!Number.isInteger(options.port) || options.port < 1024 || options.port > 65535) {
    throw new Error('--port must be an integer from 1024 through 65535');
  }
  if (!Number.isInteger(options.timeoutSeconds) || options.timeoutSeconds < 30 || options.timeoutSeconds > 1800) {
    throw new Error('--timeout must be an integer from 30 through 1800 seconds');
  }
  return options;
}

function assertNodeVersion(version = process.versions.node) {
  const [major, minor] = String(version || '').split('.').map((value) => Number(value));
  if (!Number.isInteger(major) || major < 22 || (major === 22 && minor < 12)) {
    throw new Error(`Node.js 22.12 or newer is required; found ${version || 'unknown'}.`);
  }
}

function defaultStateDir(platform = process.platform) {
  if (platform === 'win32') {
    return path.join(process.env.LOCALAPPDATA || os.tmpdir(), 'BreakTwenty', 'MoomooCloudOAuthProbe');
  }
  if (platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Caches', 'BreakTwenty', 'MoomooCloudOAuthProbe');
  }
  return path.join(
    process.env.XDG_CACHE_HOME || path.join(os.homedir(), '.cache'),
    'breaktwenty',
    'moomoo-cloud-oauth-probe',
  );
}

function base64Url(buffer) {
  return Buffer.from(buffer).toString('base64url');
}

function createPkce(randomBytes = crypto.randomBytes) {
  const verifier = base64Url(randomBytes(64));
  const challenge = base64Url(crypto.createHash('sha256').update(verifier, 'ascii').digest());
  return { verifier, challenge };
}

function buildAuthorizationUrl({ clientId, challenge, redirectUri, state }) {
  const url = new URL('/oauth2/authorize/confirm', API_ORIGIN);
  url.searchParams.set('client_id', clientId);
  url.searchParams.set('code_challenge', challenge);
  url.searchParams.set('code_challenge_method', 'S256');
  url.searchParams.set('redirect_uri', redirectUri);
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('state', state);
  return url.toString();
}

function safeEqual(left, right) {
  const leftBuffer = Buffer.from(String(left || ''), 'utf8');
  const rightBuffer = Buffer.from(String(right || ''), 'utf8');
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

function callbackPage(message) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>BreakTwenty Moomoo probe</title></head><body><main><h1>${message}</h1><p>You can close this tab and return to the terminal.</p></main></body></html>`;
}

function createCallbackListener({ expectedState, port, timeoutMs }) {
  let settled = false;
  let resolveResult;
  let rejectResult;
  const result = new Promise((resolve, reject) => {
    resolveResult = resolve;
    rejectResult = reject;
  });
  result.catch(() => {});
  const server = http.createServer((request, response) => {
    const requestUrl = new URL(request.url || '/', 'http://127.0.0.1');
    if (request.method !== 'GET' || requestUrl.pathname !== '/callback') {
      response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' });
      response.end('Not found');
      return;
    }
    const receivedState = requestUrl.searchParams.get('state') || '';
    const authorizationCode = requestUrl.searchParams.get('code') || '';
    const oauthError = requestUrl.searchParams.get('error') || '';
    const oauthDescription = requestUrl.searchParams.get('error_description') || '';
    if (!safeEqual(receivedState, expectedState)) {
      response.writeHead(400, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      response.end(callbackPage('Authorization state did not match.'), () => {
        finish(new ProbeError('Moomoo returned an OAuth callback with an invalid state.', {
          stage: 'authorization_callback',
          code: 'invalid_state',
        }));
      });
      return;
    }
    if (oauthError) {
      response.writeHead(400, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      response.end(callbackPage('Moomoo authorization was not completed.'), () => {
        finish(new ProbeError(oauthDescription || oauthError, {
          stage: 'authorization_callback',
          code: oauthError,
        }));
      });
      return;
    }
    if (!authorizationCode) {
      response.writeHead(400, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      response.end(callbackPage('Moomoo did not return an authorization code.'), () => {
        finish(new ProbeError('Moomoo did not return an authorization code.', {
          stage: 'authorization_callback',
          code: 'missing_code',
        }));
      });
      return;
    }
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
    response.end(callbackPage('Moomoo authorization was received.'), () => finish(null, authorizationCode));
  });
  const finish = (error, value) => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    const settleResult = () => {
      if (error) rejectResult(error);
      else resolveResult(value);
    };
    if (!server.listening) {
      settleResult();
      return;
    }
    server.close(settleResult);
    server.closeAllConnections?.();
  };
  const timer = setTimeout(() => {
    finish(new ProbeError('Timed out waiting for Moomoo browser authorization.', {
      stage: 'authorization_callback',
      code: 'timeout',
    }));
  }, timeoutMs);
  const ready = new Promise((resolve, reject) => {
    server.once('error', (error) => {
      finish(new ProbeError(`Could not start the local OAuth callback listener: ${error.message}`, {
        stage: 'callback_listener',
        code: error.code || 'listener_error',
      }));
      reject(error);
    });
    server.listen(port, '127.0.0.1', () => {
      const address = server.address();
      resolve(`http://localhost:${address.port}/callback`);
    });
  });
  return {
    ready,
    result,
    close() {
      finish(new ProbeError('Probe cancelled.', { stage: 'authorization_callback', code: 'cancelled' }));
    },
  };
}

function openAuthorizationUrl(url, platform = process.platform, spawnImpl = spawn) {
  const command = platform === 'win32' ? 'rundll32.exe' : platform === 'darwin' ? '/usr/bin/open' : 'xdg-open';
  const args = platform === 'win32' ? ['url.dll,FileProtocolHandler', url] : [url];
  return new Promise((resolve, reject) => {
    const child = spawnImpl(command, args, { detached: true, stdio: 'ignore', windowsHide: true });
    child.once('error', reject);
    child.once('spawn', () => {
      child.unref();
      resolve();
    });
  });
}

function providerError(body, fallback = 'Moomoo returned an unsuccessful response.') {
  if (!body || typeof body !== 'object') return { code: null, message: fallback };
  const errorObject = body.error && typeof body.error === 'object' ? body.error : null;
  return {
    code: body.errcode ?? errorObject?.code ?? (typeof body.error === 'string' ? body.error : null),
    message: String(body.errmsg || body.error_description || errorObject?.message || fallback).slice(0, 500),
  };
}

function sanitizeMessage(message) {
  const home = os.homedir();
  let sanitized = String(message || 'Unknown probe error');
  if (home) sanitized = sanitized.split(home).join('[home]');
  return sanitized
    .replace(/Bearer\s+[^\s"']+/gi, 'Bearer [redacted]')
    .replace(/\b(access_token|refresh_token|authorization_code|code_verifier|client_secret|password)\b\s*[:=]\s*[^\s,&"']+/gi, '$1=[redacted]')
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[redacted-email]')
    .replace(/\b\d{7,}\b/g, '[redacted-number]')
    .replace(/\b[a-f0-9]{32,}\b/gi, '[redacted-secret]')
    .slice(0, 500);
}

async function requestJson(url, options = {}, { stage = 'request', timeoutMs = 30000, fetchImpl = fetch } = {}) {
  const parsedUrl = new URL(url);
  if (parsedUrl.origin !== API_ORIGIN) throw new ProbeError('Refused a request outside the Moomoo API origin.', { stage });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(parsedUrl, { ...options, redirect: 'error', signal: controller.signal });
    const text = await response.text();
    let body = null;
    try {
      body = text ? parseMoomooJson(text) : null;
    } catch (_error) {
      throw new ProbeError(`Moomoo returned a non-JSON response (HTTP ${response.status}).`, {
        stage,
        status: response.status,
        code: 'invalid_json',
      });
    }
    if (!response.ok) {
      const details = providerError(body, `Moomoo returned HTTP ${response.status}.`);
      throw new ProbeError(details.message, { stage, status: response.status, code: details.code });
    }
    return { body, status: response.status };
  } catch (error) {
    if (error instanceof ProbeError) throw error;
    if (error.name === 'AbortError') {
      throw new ProbeError('Moomoo API request timed out.', { stage, code: 'timeout' });
    }
    throw new ProbeError(`Moomoo API request failed: ${error.message}`, {
      stage,
      code: error.code || 'network_error',
    });
  } finally {
    clearTimeout(timer);
  }
}

function parseMoomooJson(text) {
  return JSON.parse(text, (key, value, context) => {
    // Trading-account IDs are documented as strings, but preserving a numeric
    // response defensively avoids JavaScript rounding a 16-digit ID before it
    // is used in an account-scoped endpoint.
    if (
      (key === 'account_id' || key === 'acc_id')
      && typeof value === 'number'
      && /^\d+$/.test(String(context?.source || ''))
    ) {
      return context.source;
    }
    return value;
  });
}

function unwrapApiResponse(response, stage) {
  const body = response?.body;
  if (body?.s === 'ok') return body.d;
  if (body?.ret_code === 0) return body.data;
  const details = providerError(body);
  throw new ProbeError(details.message, { stage, status: response?.status ?? null, code: details.code });
}

async function registerOAuthClient(redirectUri, request = requestJson) {
  const response = await request(`${API_ORIGIN}/oauth2/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      redirect_uris: [redirectUri],
      token_endpoint_auth_method: 'none',
      grant_types: ['authorization_code', 'refresh_token'],
      response_types: ['code'],
      client_name: 'BreakTwenty Moomoo OAuth Probe',
    }),
  }, { stage: 'client_registration' });
  const clientId = response.body?.client_id;
  if (!clientId || typeof clientId !== 'string') {
    const details = providerError(response.body, 'Moomoo client registration did not return a client ID.');
    throw new ProbeError(details.message, {
      stage: 'client_registration',
      status: response.status,
      code: details.code || 'missing_client_id',
    });
  }
  return { clientId, scope: String(response.body.scope || '') };
}

async function exchangeAuthorizationCode({ clientId, code, redirectUri, verifier }, request = requestJson) {
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    client_id: clientId,
    redirect_uri: redirectUri,
    code_verifier: verifier,
  });
  const response = await request(`${API_ORIGIN}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  }, { stage: 'token_exchange' });
  if (!response.body?.access_token || !response.body?.refresh_token) {
    const details = providerError(response.body, 'Moomoo token exchange did not return access and refresh tokens.');
    throw new ProbeError(details.message, {
      stage: 'token_exchange',
      status: response.status,
      code: details.code || 'missing_tokens',
    });
  }
  return response.body;
}

async function refreshAccessToken({ clientId, refreshToken }, request = requestJson) {
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: refreshToken,
    client_id: clientId,
  });
  const response = await request(`${API_ORIGIN}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  }, { stage: 'token_refresh' });
  if (!response.body?.access_token) {
    const details = providerError(response.body, 'Moomoo refresh did not return an access token.');
    throw new ProbeError(details.message, {
      stage: 'token_refresh',
      status: response.status,
      code: details.code || 'missing_access_token',
    });
  }
  return response.body;
}

async function apiGet(pathname, accessToken, request = requestJson, stage = 'api_request') {
  const response = await request(`${API_ORIGIN}${pathname}`, {
    method: 'GET',
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
  }, { stage });
  return unwrapApiResponse(response, stage);
}

function fingerprint(value) {
  return crypto.createHash('sha256').update(String(value), 'utf8').digest('hex').slice(0, 12);
}

function sanitizedScopes(scope) {
  return String(scope || '')
    .split(/\s+/)
    .filter(Boolean)
    .map((entry) => (entry.startsWith('accid:') ? 'accid:[authorized]' : entry));
}

function grantedAccountIds(scope) {
  return String(scope || '')
    .split(/\s+/)
    .filter((entry) => entry.startsWith('accid:'))
    .map((entry) => entry.slice('accid:'.length))
    .filter(Boolean);
}

function validateGrantedScope(scope) {
  const entries = String(scope || '').split(/\s+/).filter(Boolean);
  if (!entries.includes('trade:read')) {
    throw new ProbeError('Moomoo authorization did not grant trade:read.', {
      stage: 'token_scope',
      code: 'missing_trade_read',
    });
  }
  if (grantedAccountIds(scope).length === 0) {
    throw new ProbeError('Moomoo authorization did not grant access to a trading account.', {
      stage: 'token_scope',
      code: 'missing_account_scope',
    });
  }
  if (entries.includes('trade:write')) {
    throw new ProbeError('Moomoo authorization included trade:write. Reconnect and select account/trading read access only.', {
      stage: 'token_scope',
      code: 'trading_write_scope',
    });
  }
}

function summarizeAccount(account) {
  return {
    account_ref: fingerprint(account.account_id),
    security_firm: String(account.security_firm || 'unknown'),
    account_type: String(account.acc_type || 'unknown'),
    enabled_markets: Array.isArray(account.enable_market) ? account.enable_market : [],
  };
}

function summarizeFunds(funds) {
  const expectedFields = ['total_assets', 'cash', 'market_val', 'available_funds', 'unrealized_pl', 'realized_pl'];
  return {
    currency: String(funds?.currency || 'unknown'),
    populated_fields: expectedFields.filter((field) => funds?.[field] !== undefined && funds?.[field] !== null),
  };
}

function summarizePositions(positions) {
  const rows = Array.isArray(positions) ? positions : Array.isArray(positions?.positions) ? positions.positions : [];
  return {
    count: rows.length,
    currencies: [...new Set(rows.map((row) => row?.currency).filter(Boolean).map(String))].sort(),
    market_prefixes: [...new Set(rows.map((row) => String(row?.code || '').split('.')[0]).filter(Boolean))].sort(),
  };
}

const TRADING_MARKETS_BY_ENABLE_CODE = new Map([
  [1, 'HK'],
  [2, 'US'],
  [4, 'HKCC'],
  [5, 'FUTURES'],
  [6, 'SG'],
  [12, 'CA'],
  [15, 'JP'],
  [18, 'KR'],
]);

function tradingMarketsForAccount(account) {
  return [...new Set((Array.isArray(account?.enable_market) ? account.enable_market : [])
    .map((value) => TRADING_MARKETS_BY_ENABLE_CODE.get(Number(value)))
    .filter(Boolean))];
}

function summarizeHistoryPage(page, rowsKey) {
  const rows = Array.isArray(page?.[rowsKey]) ? page[rowsKey] : [];
  return {
    returned_count: rows.length,
    completed: page?.completed === true,
    has_more: Boolean(page?.page_flag) || page?.completed === false,
  };
}

async function probeRecentHistory({ account, accountId, accessToken, request = requestJson }) {
  const markets = tradingMarketsForAccount(account);
  if (markets.length === 0) {
    throw new ProbeError('Moomoo returned no documented trading market for an authorized account.', {
      stage: 'recent_history',
      code: 'no_queryable_market',
    });
  }
  const history = [];
  for (const market of markets) {
    const query = new URLSearchParams({ trd_market: market, page_flag: '', page_size: '50' });
    const fills = await apiGet(
      `/api/v1.0/accounts/${accountId}/fills_history?${query}`,
      accessToken,
      request,
      `recent_fills_${market}`,
    );
    const orders = await apiGet(
      `/api/v1.0/accounts/${accountId}/orders_history?${query}`,
      accessToken,
      request,
      `recent_orders_${market}`,
    );
    history.push({
      market,
      fills: summarizeHistoryPage(fills, 'order_fills'),
      orders: summarizeHistoryPage(orders, 'orders'),
    });
  }
  return history;
}

async function probeAccounts({
  accessToken,
  currency,
  oauthScope = '',
  onAccountDiscovered = () => {},
  request = requestJson,
}) {
  const accountData = await apiGet(
    '/api/v1.0/accounts/authorized_trd_accs',
    accessToken,
    request,
    'authorized_accounts',
  );
  const accounts = Array.isArray(accountData?.accounts) ? accountData.accounts : [];
  if (accounts.length === 0) {
    throw new ProbeError('OAuth succeeded, but Moomoo returned no authorized trading accounts.', {
      stage: 'authorized_accounts',
      code: 'no_accounts',
    });
  }
  const authorizedAccountIds = grantedAccountIds(oauthScope);
  const summaries = [];
  for (const account of accounts) {
    const accountId = String(account.account_id || '');
    if (!accountId) {
      throw new ProbeError('Moomoo returned an authorized account without an account ID.', {
        stage: 'authorized_accounts',
        code: 'missing_account_id',
      });
    }
    const scopeMatchesAccount = authorizedAccountIds.length === 0
      ? null
      : authorizedAccountIds.includes('*') || authorizedAccountIds.includes(accountId);
    const accountSummary = {
      ...summarizeAccount(account),
      oauth_account_scope_match: scopeMatchesAccount,
    };
    onAccountDiscovered(accountSummary);
    if (scopeMatchesAccount === false) {
      throw new ProbeError(
        'Moomoo returned a trading account that does not match the account-specific OAuth grant.',
        { stage: 'authorized_accounts', code: 'account_scope_mismatch' },
      );
    }
    const encodedAccountId = encodeURIComponent(accountId);
    const funds = await apiGet(
      `/api/v1.0/accounts/${encodedAccountId}/funds?currency=${encodeURIComponent(currency)}`,
      accessToken,
      request,
      'account_funds',
    );
    const positions = await apiGet(
      `/api/v1.0/accounts/${encodedAccountId}/positions`,
      accessToken,
      request,
      'account_positions',
    );
    const recentHistory = await probeRecentHistory({
      account,
      accountId: encodedAccountId,
      accessToken,
      request,
    });
    summaries.push({
      ...accountSummary,
      funds: summarizeFunds(funds),
      positions: summarizePositions(positions),
      recent_history: recentHistory,
    });
  }
  return summaries;
}

function clientRegistryPath(stateDir) {
  return path.join(stateDir, 'oauth-clients.json');
}

function loadCachedClient(stateDir, redirectUri) {
  try {
    const parsed = JSON.parse(fs.readFileSync(clientRegistryPath(stateDir), 'utf8'));
    const clientId = parsed?.clients?.[redirectUri]?.client_id;
    return typeof clientId === 'string' && clientId ? clientId : '';
  } catch (_error) {
    return '';
  }
}

function saveCachedClient(stateDir, redirectUri, clientId) {
  fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  const target = clientRegistryPath(stateDir);
  let registry = { version: 1, clients: {} };
  try {
    const parsed = JSON.parse(fs.readFileSync(target, 'utf8'));
    if (parsed?.version === 1 && parsed.clients && typeof parsed.clients === 'object') registry = parsed;
  } catch (_error) {
    // A missing or invalid cache is replaced with a fresh non-secret client-ID registry.
  }
  registry.clients[redirectUri] = { client_id: clientId };
  const temporary = `${target}.tmp-${process.pid}-${crypto.randomBytes(4).toString('hex')}`;
  fs.writeFileSync(temporary, `${JSON.stringify(registry, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, target);
}

function diagnosticFilename(now = new Date()) {
  return `moomoo-cloud-oauth-probe-${now.toISOString().replace(/[:.]/g, '-')}.json`;
}

function writeDiagnostic(stateDir, diagnostic) {
  fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  const target = path.join(stateDir, diagnosticFilename());
  fs.writeFileSync(target, `${JSON.stringify(diagnostic, null, 2)}\n`, { mode: 0o600 });
  return target;
}

function errorDiagnostic(error) {
  return {
    stage: error?.stage || 'unknown',
    code: error?.code === null || error?.code === undefined ? null : String(error.code).slice(0, 100),
    http_status: Number.isInteger(error?.status) ? error.status : null,
    message: sanitizeMessage(error?.message),
  };
}

async function runProbe(options, {
  callbackFactory = createCallbackListener,
  openUrl = openAuthorizationUrl,
  request = requestJson,
  write = (line) => process.stdout.write(`${line}\n`),
} = {}) {
  const stateDir = path.resolve(options.stateDir || defaultStateDir());
  const startedAt = new Date();
  const diagnostic = {
    version: 1,
    started_at: startedAt.toISOString(),
    platform: process.platform,
    architecture: process.arch,
    node_version: process.versions.node,
    api_origin: API_ORIGIN,
    callback_port: options.port,
    requested_currency: options.currency,
    stages: [],
    result: null,
    error: null,
  };
  const stage = (name, details = {}) => diagnostic.stages.push({ name, at: new Date().toISOString(), ...details });
  const state = base64Url(crypto.randomBytes(32));
  const pkce = createPkce();
  const callback = callbackFactory({
    expectedState: state,
    port: options.port,
    timeoutMs: options.timeoutSeconds * 1000,
  });
  let diagnosticPath = '';
  try {
    const redirectUri = await callback.ready;
    stage('callback_listener_ready');
    write(`Local OAuth callback ready at ${redirectUri}`);

    let clientId = options.freshClient ? '' : loadCachedClient(stateDir, redirectUri);
    let registrationScope = '';
    if (clientId) {
      stage('client_registration', { reused: true });
      write('Reusing the cached non-secret OAuth client ID.');
    } else {
      write('Registering a Moomoo OAuth public client...');
      const registration = await registerOAuthClient(redirectUri, request);
      clientId = registration.clientId;
      registrationScope = registration.scope;
      saveCachedClient(stateDir, redirectUri, clientId);
      stage('client_registration', { reused: false, offered_scopes: sanitizedScopes(registrationScope) });
    }

    const authorizationUrl = buildAuthorizationUrl({
      clientId,
      challenge: pkce.challenge,
      redirectUri,
      state,
    });
    write('');
    write('Authorize BreakTwenty in the Moomoo page opened by your browser.');
    write('If Moomoo offers individual scope choices, select account/trading read access only.');
    write('If the browser does not open, copy this URL into it:');
    write(authorizationUrl);
    write('');
    if (!options.noOpen) {
      try {
        await openUrl(authorizationUrl);
        stage('authorization_browser_opened');
      } catch (error) {
        stage('authorization_browser_open_failed', { code: String(error.code || 'open_failed') });
        write(`Could not open the browser automatically (${error.message}). Use the URL above.`);
      }
    } else {
      stage('authorization_url_printed');
    }

    const code = await callback.result;
    stage('authorization_callback_received');
    write('Authorization callback received. Exchanging the one-time code...');
    const tokens = await exchangeAuthorizationCode({
      clientId,
      code,
      redirectUri,
      verifier: pkce.verifier,
    }, request);
    stage('token_exchange', {
      expires_in: Number(tokens.expires_in) || null,
      scopes: sanitizedScopes(tokens.scope),
      authorized_account_scope_count: grantedAccountIds(tokens.scope).length,
    });
    validateGrantedScope(tokens.scope);

    write('Testing authorized accounts, funds, positions, recent fills, and recent orders...');
    const accounts = await probeAccounts({
      accessToken: tokens.access_token,
      currency: options.currency,
      oauthScope: tokens.scope,
      onAccountDiscovered: (account) => stage('authorized_account_discovered', account),
      request,
    });
    stage('account_data', { account_count: accounts.length });

    write('Testing refresh-token access...');
    const refreshed = await refreshAccessToken({ clientId, refreshToken: tokens.refresh_token }, request);
    const refreshedAccountData = await apiGet(
      '/api/v1.0/accounts/authorized_trd_accs',
      refreshed.access_token,
      request,
      'refreshed_authorized_accounts',
    );
    const refreshedAccounts = Array.isArray(refreshedAccountData?.accounts) ? refreshedAccountData.accounts : [];
    if (refreshedAccounts.length !== accounts.length) {
      throw new ProbeError('The refreshed token returned a different authorized-account count.', {
        stage: 'refreshed_authorized_accounts',
        code: 'account_count_mismatch',
      });
    }
    stage('token_refresh', { account_count: refreshedAccounts.length });

    diagnostic.result = {
      ok: true,
      oauth_scope: sanitizedScopes(refreshed.scope || tokens.scope),
      token_refresh_verified: true,
      account_count: accounts.length,
      accounts,
    };
    write('');
    write(`PASS: OAuth, refresh, and read-only account access succeeded for ${accounts.length} account(s).`);
    for (const account of accounts) {
      const fillCount = account.recent_history.reduce((total, entry) => total + entry.fills.returned_count, 0);
      const orderCount = account.recent_history.reduce((total, entry) => total + entry.orders.returned_count, 0);
      const markets = account.recent_history.map((entry) => entry.market).join('/');
      write(`  ${account.account_ref}: ${account.security_firm}, ${account.account_type}, ${account.positions.count} position(s), ${fillCount} recent fill(s), ${orderCount} recent order(s), markets ${markets}, funds ${account.funds.currency}`);
    }
    return { ok: true, diagnostic, stateDir };
  } catch (error) {
    callback.close();
    diagnostic.error = errorDiagnostic(error);
    diagnostic.result = { ok: false };
    write('');
    write(`FAIL [${diagnostic.error.stage}]: ${diagnostic.error.message}`);
    return { ok: false, error, diagnostic, stateDir };
  } finally {
    diagnostic.finished_at = new Date().toISOString();
    diagnosticPath = writeDiagnostic(stateDir, diagnostic);
    write(`Sanitized diagnostic: ${diagnosticPath}`);
  }
}

async function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
    assertNodeVersion();
  } catch (error) {
    process.stderr.write(`ERROR: ${error.message}\n\n${usage()}\n`);
    process.exitCode = 1;
    return;
  }
  if (options.help) {
    process.stdout.write(`${usage()}\n`);
    return;
  }
  const outcome = await runProbe(options);
  if (!outcome.ok) process.exitCode = 1;
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`FAIL: ${error.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  API_ORIGIN,
  ProbeError,
  apiGet,
  assertNodeVersion,
  buildAuthorizationUrl,
  createCallbackListener,
  createPkce,
  defaultStateDir,
  exchangeAuthorizationCode,
  loadCachedClient,
  openAuthorizationUrl,
  parseMoomooJson,
  parseArgs,
  probeAccounts,
  probeRecentHistory,
  providerError,
  refreshAccessToken,
  registerOAuthClient,
  requestJson,
  runProbe,
  sanitizeMessage,
  sanitizedScopes,
  saveCachedClient,
  summarizeAccount,
  summarizeFunds,
  summarizeHistoryPage,
  summarizePositions,
  tradingMarketsForAccount,
  unwrapApiResponse,
  usage,
  validateGrantedScope,
  writeDiagnostic,
};
