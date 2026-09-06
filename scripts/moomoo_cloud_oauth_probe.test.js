const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  ProbeError,
  assertNodeVersion,
  buildAuthorizationUrl,
  createCallbackListener,
  createPkce,
  loadCachedClient,
  parseMoomooJson,
  parseArgs,
  probeAccounts,
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
  validateGrantedScope,
} = require('./moomoo_cloud_oauth_probe');

test('cloud probe arguments expose no credential or token input surface', () => {
  assert.equal(parseArgs([]).currency, 'USD');
  assert.deepEqual(parseArgs(['--currency', 'usd', '--port', '60356', '--timeout', '600', '--no-open']), {
    currency: 'USD',
    freshClient: false,
    help: false,
    noOpen: true,
    port: 60356,
    stateDir: '',
    timeoutSeconds: 600,
  });
  assert.throws(() => parseArgs(['--password', 'secret']), /Unknown option/);
  assert.throws(() => parseArgs(['--access-token', 'secret']), /Unknown option/);
  assert.throws(() => parseArgs(['--port', '80']), /1024 through 65535/);
  assert.throws(() => parseArgs(['--currency', 'dollars']), /three-letter ISO/);
});

test('cloud probe enforces the desktop minimum Node version', () => {
  assert.doesNotThrow(() => assertNodeVersion('22.12.0'));
  assert.doesNotThrow(() => assertNodeVersion('24.0.0'));
  assert.throws(() => assertNodeVersion('22.11.0'), /22\.12 or newer/);
});

test('PKCE and authorization URL use the documented OAuth public-client contract', () => {
  const pkce = createPkce((size) => Buffer.alloc(size, 7));
  assert.match(pkce.verifier, /^[A-Za-z0-9_-]{43,128}$/);
  assert.match(pkce.challenge, /^[A-Za-z0-9_-]{43}$/);
  const url = new URL(buildAuthorizationUrl({
    clientId: 'client-id',
    challenge: pkce.challenge,
    redirectUri: 'http://localhost:60355/callback',
    state: 'state-value',
  }));
  assert.equal(url.origin, 'https://webapi.moomoo.com');
  assert.equal(url.pathname, '/oauth2/authorize/confirm');
  assert.equal(url.searchParams.get('client_id'), 'client-id');
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256');
  assert.equal(url.searchParams.get('redirect_uri'), 'http://localhost:60355/callback');
  assert.equal(url.searchParams.get('response_type'), 'code');
  assert.equal(url.searchParams.get('state'), 'state-value');
});

test('callback listener accepts one matching callback without logging or persisting its code', async () => {
  const callback = createCallbackListener({ expectedState: 'expected', port: 0, timeoutMs: 5000 });
  const redirectUri = await callback.ready;
  const response = await fetch(`${redirectUri}?code=one-time-secret&state=expected`);
  assert.equal(response.status, 200);
  assert.equal(await callback.result, 'one-time-secret');
});

test('callback listener rejects mismatched state', async () => {
  const callback = createCallbackListener({ expectedState: 'expected', port: 0, timeoutMs: 5000 });
  const redirectUri = await callback.ready;
  const response = await fetch(`${redirectUri}?code=one-time-secret&state=wrong`);
  assert.equal(response.status, 400);
  await assert.rejects(callback.result, (error) => error instanceof ProbeError && error.code === 'invalid_state');
});

test('cached OAuth registry persists only the public client ID', () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-moomoo-cloud-'));
  const redirectUri = 'http://localhost:60355/callback';
  try {
    saveCachedClient(stateDir, redirectUri, 'public-client-id');
    assert.equal(loadCachedClient(stateDir, redirectUri), 'public-client-id');
    const stored = fs.readFileSync(path.join(stateDir, 'oauth-clients.json'), 'utf8');
    assert.match(stored, /public-client-id/);
    assert.doesNotMatch(stored, /access_token|refresh_token|registration_access_token|password/);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});

test('account summaries omit identifiers, card numbers, balances, quantities, codes, and names', () => {
  const account = summarizeAccount({
    account_id: '123456789',
    account_card_number: '1111222233334444',
    univs_account_card_number: '5555666677778888',
    security_firm: 'FUTUINC',
    acc_type: 'margin',
    enable_market: [2, 11],
  });
  const funds = summarizeFunds({ currency: 'CAD', total_assets: '1000', cash: '500', market_val: '500' });
  const positions = summarizePositions([
    { code: 'US.AAPL', stock_name: 'Apple', qty: '10', currency: 'USD' },
    { code: 'CA.XEQT', stock_name: 'XEQT', qty: '20', currency: 'CAD' },
  ]);
  const serialized = JSON.stringify({ account, funds, positions });
  assert.match(account.account_ref, /^[a-f0-9]{12}$/);
  assert.deepEqual(account.enabled_markets, [2, 11]);
  assert.deepEqual(funds.populated_fields, ['total_assets', 'cash', 'market_val']);
  assert.deepEqual(positions, { count: 2, currencies: ['CAD', 'USD'], market_prefixes: ['CA', 'US'] });
  assert.doesNotMatch(serialized, /123456789|1111222233334444|5555666677778888|1000|500|AAPL|XEQT|Apple|qty/);
});

test('recent-history probes derive only documented trading markets and retain counts, not records', () => {
  assert.deepEqual(tradingMarketsForAccount({ enable_market: [12, 2, 7, 12] }), ['CA', 'US']);
  assert.deepEqual(summarizeHistoryPage({
    order_fills: [{ deal_id: 'secret-deal', code: 'CA.XEQT', qty: '100' }],
    page_flag: 'secret-next-page',
    completed: false,
  }, 'order_fills'), {
    returned_count: 1,
    completed: false,
    has_more: true,
  });
});

test('authorized account probe reads funds and positions for every account', async () => {
  const calls = [];
  const discovered = [];
  const fakeRequest = async (url) => {
    const parsed = new URL(url);
    calls.push(`${parsed.pathname}${parsed.search}`);
    if (parsed.pathname.endsWith('/authorized_trd_accs')) {
      return {
        status: 200,
        body: {
          s: 'ok',
          d: { accounts: [{ account_id: 'a/b', security_firm: 'FUTUCA', enable_market: [12] }] },
        },
      };
    }
    if (parsed.pathname.endsWith('/funds')) {
      return { status: 200, body: { s: 'ok', d: { currency: 'CAD', total_assets: '1' } } };
    }
    if (parsed.pathname.endsWith('/positions')) {
      return { status: 200, body: { s: 'ok', d: [{ code: 'CA.XEQT', currency: 'CAD' }] } };
    }
    if (parsed.pathname.endsWith('/fills_history')) {
      return { status: 200, body: { s: 'ok', d: { order_fills: [], page_flag: '', completed: true } } };
    }
    return { status: 200, body: { s: 'ok', d: { orders: [], page_flag: '', completed: true } } };
  };
  const accounts = await probeAccounts({
    accessToken: 'memory-only-token',
    currency: 'CAD',
    oauthScope: 'trade:read accid:a/b',
    onAccountDiscovered: (account) => discovered.push(account),
    request: fakeRequest,
  });
  assert.equal(accounts.length, 1);
  assert.equal(accounts[0].positions.count, 1);
  assert.equal(accounts[0].oauth_account_scope_match, true);
  assert.equal(discovered[0].oauth_account_scope_match, true);
  assert.deepEqual(calls, [
    '/api/v1.0/accounts/authorized_trd_accs',
    '/api/v1.0/accounts/a%2Fb/funds?currency=CAD',
    '/api/v1.0/accounts/a%2Fb/positions',
    '/api/v1.0/accounts/a%2Fb/fills_history?trd_market=CA&page_flag=&page_size=50',
    '/api/v1.0/accounts/a%2Fb/orders_history?trd_market=CA&page_flag=&page_size=50',
  ]);
});

test('authorized account probe stops before account data calls when OAuth account scope differs', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(new URL(url).pathname);
    return {
      status: 200,
      body: { s: 'ok', d: { accounts: [{ account_id: 'returned-account', enable_market: [12] }] } },
    };
  };
  await assert.rejects(
    probeAccounts({
      accessToken: 'memory-only-token',
      currency: 'CAD',
      oauthScope: 'trade:read accid:different-account',
      request,
    }),
    (error) => error instanceof ProbeError && error.code === 'account_scope_mismatch',
  );
  assert.deepEqual(calls, ['/api/v1.0/accounts/authorized_trd_accs']);
});

test('API response parser supports documented trading response and rejects provider errors', () => {
  assert.deepEqual(unwrapApiResponse({ status: 200, body: { s: 'ok', d: { accounts: [] } } }, 'accounts'), {
    accounts: [],
  });
  assert.throws(
    () => unwrapApiResponse({ status: 200, body: { s: 'error', errcode: -1200, errmsg: 'Denied' } }, 'accounts'),
    (error) => error instanceof ProbeError && error.stage === 'accounts' && error.code === -1200,
  );
});

test('Moomoo JSON parser preserves undocumented numeric account IDs without IEEE-754 rounding', () => {
  const parsed = parseMoomooJson('{"account_id":9876543210987655,"nested":{"acc_id":9876543210987657}}');
  assert.equal(parsed.account_id, '9876543210987655');
  assert.equal(parsed.nested.acc_id, '9876543210987657');
});

test('diagnostic scopes redact account-specific OAuth authorization', () => {
  assert.deepEqual(sanitizedScopes('quote:read trade:read accid:123456'), [
    'quote:read',
    'trade:read',
    'accid:[authorized]',
  ]);
});

test('granted OAuth scope must include read access and an account-specific grant', () => {
  assert.doesNotThrow(() => validateGrantedScope('quote:read trade:read accid:123456'));
  assert.throws(
    () => validateGrantedScope('quote:read accid:123456'),
    (error) => error instanceof ProbeError && error.code === 'missing_trade_read',
  );
  assert.throws(
    () => validateGrantedScope('quote:read trade:read'),
    (error) => error instanceof ProbeError && error.code === 'missing_account_scope',
  );
  assert.throws(
    () => validateGrantedScope('trade:read trade:write accid:123456'),
    (error) => error instanceof ProbeError && error.code === 'trading_write_scope',
  );
});

test('diagnostic messages redact common authentication and identity material', () => {
  const message = sanitizeMessage('password=hunter2 access_token=abc123 user@example.com account 123456789');
  assert.equal(
    message,
    'password=[redacted] access_token=[redacted] [redacted-email] account [redacted-number]',
  );
});

test('full cloud probe validates registration, OAuth, data reads, and token refresh without retaining secrets', async () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-moomoo-cloud-flow-'));
  const output = [];
  const calls = [];
  const secrets = ['one-time-code', 'initial-access', 'refresh-secret', 'refreshed-access', 'real-account-id'];
  const request = async (url, options = {}, metadata = {}) => {
    const parsed = new URL(url);
    calls.push({ pathname: parsed.pathname, body: String(options.body || ''), stage: metadata.stage });
    if (parsed.pathname === '/oauth2/register') {
      return {
        status: 201,
        body: { client_id: 'public-client-id', scope: 'quote:read trade:read accid:real-account-id' },
      };
    }
    if (parsed.pathname === '/oauth2/token' && String(options.body).includes('grant_type=authorization_code')) {
      return {
        status: 200,
        body: {
          access_token: 'initial-access',
          refresh_token: 'refresh-secret',
          expires_in: 7200,
          scope: 'quote:read trade:read accid:real-account-id',
        },
      };
    }
    if (parsed.pathname === '/oauth2/token') {
      return { status: 200, body: { access_token: 'refreshed-access', scope: 'trade:read accid:real-account-id' } };
    }
    if (parsed.pathname.endsWith('/authorized_trd_accs')) {
      return {
        status: 200,
        body: {
          s: 'ok',
          d: {
            accounts: [{
              account_id: 'real-account-id',
              security_firm: 'FUTUCA',
              acc_type: 'margin',
              enable_market: [12, 2],
            }],
          },
        },
      };
    }
    if (parsed.pathname.endsWith('/funds')) {
      return { status: 200, body: { s: 'ok', d: { currency: 'CAD', total_assets: '1234.56' } } };
    }
    if (parsed.pathname.endsWith('/positions')) {
      return {
        status: 200,
        body: { s: 'ok', d: [{ code: 'CA.XEQT', stock_name: 'XEQT', qty: '100', currency: 'CAD' }] },
      };
    }
    if (parsed.pathname.endsWith('/fills_history')) {
      return {
        status: 200,
        body: { s: 'ok', d: { order_fills: [{ deal_id: 'deal-secret' }], page_flag: '', completed: true } },
      };
    }
    if (parsed.pathname.endsWith('/orders_history')) {
      return {
        status: 200,
        body: { s: 'ok', d: { orders: [{ order_id: 'order-secret' }], page_flag: '', completed: true } },
      };
    }
    throw new Error(`Unexpected request: ${parsed.pathname}`);
  };
  try {
    const outcome = await runProbe({
      currency: 'CAD',
      freshClient: true,
      help: false,
      noOpen: true,
      port: 60355,
      stateDir,
      timeoutSeconds: 300,
    }, {
      callbackFactory: () => ({
        ready: Promise.resolve('http://localhost:60355/callback'),
        result: Promise.resolve('one-time-code'),
        close() {},
      }),
      request,
      write: (line) => output.push(line),
    });
    assert.equal(outcome.ok, true);
    assert.equal(outcome.diagnostic.result.token_refresh_verified, true);
    assert.equal(outcome.diagnostic.result.account_count, 1);
    assert.deepEqual(calls.map((call) => call.stage), [
      'client_registration',
      'token_exchange',
      'authorized_accounts',
      'account_funds',
      'account_positions',
      'recent_fills_CA',
      'recent_orders_CA',
      'recent_fills_US',
      'recent_orders_US',
      'token_refresh',
      'refreshed_authorized_accounts',
    ]);
    const retained = `${JSON.stringify(outcome.diagnostic)}\n${output.join('\n')}\n${fs.readdirSync(stateDir)
      .map((entry) => fs.readFileSync(path.join(stateDir, entry), 'utf8'))
      .join('\n')}`;
    for (const secret of [...secrets, 'deal-secret', 'order-secret']) {
      assert.doesNotMatch(retained, new RegExp(secret));
    }
    assert.match(retained, /accid:\[authorized\]/);
  } finally {
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});
