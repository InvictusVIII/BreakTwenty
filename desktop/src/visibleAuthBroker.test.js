const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { VisibleAuthBroker } = require('./visibleAuthBroker');
const { MANAGED_VISIBLE_AUTH_PYTHON } = require('./visibleAuthPython');

test('runner grant crosses only the one-shot stdin bootstrap', async () => {
  const appRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-visible-auth-'));
  try {
    const runnerPath = path.join(appRoot, 'capture-runner.js');
    const capturePath = path.join(appRoot, 'capture.json');
    const escapedLogPath = path.join(appRoot, 'escaped-runner.log');
    const catalogPath = path.join(appRoot, 'provider-catalog.json');
    fs.writeFileSync(
      runnerPath,
      [
        "const fs = require('node:fs');",
        "let input = '';",
        "process.stdin.setEncoding('utf8');",
        "console.log('BREAKTWENTY_VISIBLE_AUTH_LOG_PATH ' + process.argv[3]);",
        "process.stdin.on('data', (chunk) => { input += chunk; });",
        "process.stdin.on('end', () => {",
        "  fs.writeFileSync(process.argv[2], JSON.stringify({ argv: process.argv, env: process.env, input }));",
        "  const payload = JSON.stringify({",
        "    provider: process.env.BREAKTWENTY_VISIBLE_AUTH_PROVIDER,",
        "    attemptId: process.env.BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID,",
        "    syncId: 'sync_test_1',",
        "    synced: false, authenticated: true, storageStateStaged: true,",
        "    sessionArtifactStaged: true, credentialsCaptured: true, credentialsStaged: true,",
        "    responseStatus: 'handoff_ready', password: 'renderer-must-not-see-this',",
        "  });",
        "  process.stdout.write('runner echoed plaintext renderer-must-');",
        "  process.stdout.write('not-see-this\\nBREAKTWENTY_VISIBLE_AUTH_RES');",
        "  process.stdout.write('ULT ' + payload.slice(0, 40));",
        "  process.stdout.write(payload.slice(40) + '\\n');",
        "});",
      ].join('\n'),
      'utf8',
    );
    fs.writeFileSync(
      catalogPath,
      JSON.stringify({
        rbc: {
          displayName: 'RBC',
          backend: { runtimeState: { artifactKinds: [] } },
          desktop: {
            visibleAuth: {
              enabled: true,
              addFlow: true,
              manualFlow: true,
              runnerCommand: [MANAGED_VISIBLE_AUTH_PYTHON, runnerPath, capturePath, escapedLogPath],
            },
          },
        },
      }),
      'utf8',
    );
    const broker = new VisibleAuthBroker({
      appRoot,
      backendApiUrl: 'http://127.0.0.1:8000/api',
      runtimeEnv: {
        PROVIDER_CATALOG_PATH: catalogPath,
        BREAKTWENTY_VISIBLE_AUTH_PYTHON: process.execPath,
        BREAKTWENTY_DATABASE_ENCRYPTION_KEY: 'db-secret-not-for-child',
        BREAKTWENTY_APP_ENCRYPTION_KEY: 'app-secret-not-for-child',
        BREAKTWENTY_LAUNCH_TOKEN: 'launch-secret-not-for-child',
        BREAKTWENTY_LAUNCH_TOKEN_FILE: '/private/token-file',
        BREAKTWENTY_RUNNER_GRANT: 'stale-grant-not-for-child',
      },
      getLaunchAuthToken: () => 'desktop-role-secret',
    });
    let registered = null;
    let revokedSession = null;
    broker.registerRunnerGrant = async (request) => { registered = request; };
    broker.revokeRunnerScope = async (session) => { revokedSession = session; };

    const launched = await broker.launch({
      provider: 'rbc',
      addFlow: true,
      attemptId: '../../renderer-controlled',
    });
    assert.equal(launched.requestStatus, 'launched');
    const child = broker.sessions.get('rbc').process;
    if (child.exitCode === null) {
      await new Promise((resolve, reject) => {
        child.once('close', resolve);
        child.once('error', reject);
      });
    }

    const captured = JSON.parse(fs.readFileSync(capturePath, 'utf8'));
    const bootstrap = JSON.parse(captured.input);
    assert.equal(bootstrap.version, 1);
    assert.equal(bootstrap.grant, registered.grant);
    assert.equal(captured.input.endsWith('\n'), true);
    assert.equal(captured.argv.some((value) => value.includes(registered.grant)), false);
    for (const key of [
      'BREAKTWENTY_DATABASE_ENCRYPTION_KEY',
      'BREAKTWENTY_APP_ENCRYPTION_KEY',
      'BREAKTWENTY_LAUNCH_TOKEN',
      'BREAKTWENTY_LAUNCH_TOKEN_FILE',
      'BREAKTWENTY_RUNNER_GRANT',
    ]) {
      assert.equal(Object.hasOwn(captured.env, key), false);
    }
    const session = broker.sessions.get('rbc');
    assert.match(session.attemptId, /^[0-9a-f-]{36}$/);
    assert.notEqual(session.attemptId, '../../renderer-controlled');
    assert.equal(registered.attemptId, session.attemptId);
    assert.equal(
      path.relative(path.join(appRoot, '.desktop-auth', 'logs', 'visible-auth'), session.logPath).startsWith('..'),
      false,
    );
    assert.notEqual(session.logPath, escapedLogPath);
    assert.equal(fs.existsSync(escapedLogPath), false);
    const brokerLog = fs.readFileSync(session.logPath, 'utf8');
    assert.equal(brokerLog.includes(registered.grant), false);
    assert.equal(brokerLog.includes('renderer-must-not-see-this'), false);
    const rendererSnapshot = broker.snapshot(session);
    assert.equal(JSON.stringify(rendererSnapshot).includes('renderer-must-not-see-this'), false);
    assert.equal(Object.hasOwn(rendererSnapshot, 'lastOutput'), false);
    assert.equal(Object.hasOwn(rendererSnapshot, 'logPath'), false);
    assert.deepEqual(Object.keys(rendererSnapshot.resultData).sort(), [
      'attemptId',
      'authenticated',
      'credentialsCaptured',
      'credentialsStaged',
      'provider',
      'responseStatus',
      'sessionArtifactStaged',
      'storageStateStaged',
      'syncId',
      'synced',
    ].sort());
    assert.equal(rendererSnapshot.resultData.attemptId, session.attemptId);
    assert.equal(revokedSession.provider, 'rbc');
    assert.equal(revokedSession.attemptId, registered.attemptId);
  } finally {
    fs.rmSync(appRoot, { recursive: true, force: true });
  }
});

test('runner authorization retries the same one-time grant after backend readiness is restored', async () => {
  const appRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-visible-auth-recovery-'));
  try {
    let registrationCount = 0;
    let readinessCount = 0;
    const requests = [];
    const broker = new VisibleAuthBroker({
      appRoot,
      backendApiUrl: 'http://127.0.0.1:8000/api',
      getLaunchAuthToken: () => 'desktop-role-secret',
      restoreBackendReadiness: async () => {
        readinessCount += 1;
        return { ok: true };
      },
    });
    broker.registerRunnerGrant = async (request) => {
      registrationCount += 1;
      requests.push({ ...request });
      if (registrationCount === 1) {
        const error = new Error('The operation was aborted due to timeout');
        error.runnerGrantRetryable = true;
        throw error;
      }
    };
    const session = {
      logPath: broker.createSessionLogPath('tangerine', 'attempt-recovery'),
    };
    const request = {
      grant: 'same-one-time-grant',
      provider: 'tangerine',
      attemptId: 'attempt-recovery',
      institutionId: '9',
      addFlow: false,
    };

    await broker.authorizeRunnerGrant(request, session);

    assert.equal(registrationCount, 2);
    assert.equal(readinessCount, 1);
    assert.deepEqual(requests, [request, request]);
    assert.match(fs.readFileSync(session.logPath, 'utf8'), /Backend readiness restored/);
  } finally {
    fs.rmSync(appRoot, { recursive: true, force: true });
  }
});

test('runner authorization does not retry a provider or ownership rejection', async () => {
  let readinessCount = 0;
  const broker = new VisibleAuthBroker({
    appRoot: os.tmpdir(),
    backendApiUrl: 'http://127.0.0.1:8000/api',
    getLaunchAuthToken: () => 'desktop-role-secret',
    restoreBackendReadiness: async () => {
      readinessCount += 1;
      return { ok: true };
    },
  });
  broker.registerRunnerGrant = async () => {
    const error = new Error('Institution not found');
    error.runnerGrantRetryable = false;
    throw error;
  };

  await assert.rejects(
    broker.authorizeRunnerGrant({ provider: 'tangerine' }, { logPath: '' }),
    /Institution not found/,
  );
  assert.equal(readinessCount, 0);
});

test('pending runner authorization deduplicates concurrent visible-auth launches', async () => {
  const appRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-visible-auth-pending-'));
  try {
    const runnerPath = path.join(appRoot, 'pending-runner.js');
    const launchCountPath = path.join(appRoot, 'launch-count.txt');
    const catalogPath = path.join(appRoot, 'provider-catalog.json');
    fs.writeFileSync(
      runnerPath,
      [
        "const fs = require('node:fs');",
        'process.stdin.resume();',
        "process.stdin.on('end', () => fs.appendFileSync(process.argv[2], '1'));",
      ].join('\n'),
      'utf8',
    );
    fs.writeFileSync(
      catalogPath,
      JSON.stringify({
        tangerine: {
          displayName: 'Tangerine',
          backend: { runtimeState: { artifactKinds: [] } },
          desktop: {
            visibleAuth: {
              enabled: true,
              addFlow: true,
              manualFlow: true,
              runnerCommand: [MANAGED_VISIBLE_AUTH_PYTHON, runnerPath, launchCountPath],
            },
          },
        },
      }),
      'utf8',
    );
    const broker = new VisibleAuthBroker({
      appRoot,
      backendApiUrl: 'http://127.0.0.1:8000/api',
      runtimeEnv: {
        PROVIDER_CATALOG_PATH: catalogPath,
        BREAKTWENTY_VISIBLE_AUTH_PYTHON: process.execPath,
      },
      getLaunchAuthToken: () => 'desktop-role-secret',
    });
    let releaseRegistration;
    let registrationCount = 0;
    broker.registerRunnerGrant = async () => {
      registrationCount += 1;
      await new Promise((resolve) => { releaseRegistration = resolve; });
    };
    broker.revokeRunnerScope = async () => {};

    const firstLaunch = broker.launch({ provider: 'tangerine', institutionId: 9 });
    const repeatedLaunch = await broker.launch({ provider: 'tangerine', institutionId: 9 });
    assert.equal(repeatedLaunch.requestStatus, 'already_running');
    assert.equal(registrationCount, 1);

    releaseRegistration();
    const launched = await firstLaunch;
    assert.equal(launched.requestStatus, 'launched');
    const child = broker.sessions.get('tangerine').process;
    await new Promise((resolve, reject) => {
      child.once('close', resolve);
      child.once('error', reject);
    });
    assert.equal(fs.readFileSync(launchCountPath, 'utf8'), '1');
  } finally {
    fs.rmSync(appRoot, { recursive: true, force: true });
  }
});

test('secure browser closed runner output becomes terminal browser-closed status', async () => {
  const appRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-visible-auth-closed-'));
  try {
    const runnerPath = path.join(appRoot, 'closed-runner.js');
    const cleanupPath = path.join(appRoot, 'runner-cleanup-finished');
    const catalogPath = path.join(appRoot, 'provider-catalog.json');
    const closedMessage = 'Login window was closed before secure login finished. Start sync again and keep the bank browser window open until the app says it is done.';
    fs.writeFileSync(
      runnerPath,
      [
        "const fs = require('node:fs');",
        `console.error(${JSON.stringify(closedMessage)});`,
        'setTimeout(() => {',
        `  fs.writeFileSync(${JSON.stringify(cleanupPath)}, 'complete');`,
        "  const payload = JSON.stringify({",
        "    provider: process.env.BREAKTWENTY_VISIBLE_AUTH_PROVIDER,",
        "    attempt_id: process.env.BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID,",
        "    sync_id: 'bmo-sync-final', add_flow: true, result: 'failed',",
        `    message: ${JSON.stringify(closedMessage)}, last_output: ${JSON.stringify(closedMessage)},`,
        '  });',
        "  console.log('BREAKTWENTY_VISIBLE_AUTH_DIAGNOSTICS_READY ' + payload);",
        '  process.exit(1);',
        '}, 75);',
      ].join('\n'),
      'utf8',
    );
    fs.writeFileSync(
      catalogPath,
      JSON.stringify({
        bmo: {
          displayName: 'BMO',
          backend: { runtimeState: { artifactKinds: [] } },
          desktop: {
            visibleAuth: {
              enabled: true,
              addFlow: true,
              manualFlow: true,
              runnerCommand: [MANAGED_VISIBLE_AUTH_PYTHON, runnerPath],
            },
          },
        },
      }),
      'utf8',
    );
    const broker = new VisibleAuthBroker({
      appRoot,
      backendApiUrl: 'http://127.0.0.1:8000/api',
      runtimeEnv: {
        PROVIDER_CATALOG_PATH: catalogPath,
        BREAKTWENTY_VISIBLE_AUTH_PYTHON: process.execPath,
      },
      getLaunchAuthToken: () => 'desktop-role-secret',
    });
    broker.registerRunnerGrant = async () => {};
    broker.revokeRunnerScope = async () => {};
    let published = null;
    let logAtPublication = '';
    broker.publishDiagnosticsReady = async (currentSession) => {
      published = currentSession.diagnosticsReady;
      logAtPublication = fs.readFileSync(currentSession.logPath, 'utf8');
    };

    const launched = await broker.launch({ provider: 'bmo', addFlow: true });
    assert.equal(launched.requestStatus, 'launched');
    const session = broker.sessions.get('bmo');
    await new Promise((resolve, reject) => {
      session.process.once('close', resolve);
      session.process.once('error', reject);
    });

    assert.equal(session.status, 'failed');
    assert.equal(session.message, closedMessage);
    assert.equal(broker.snapshot(session).message, closedMessage);
    assert.equal(fs.readFileSync(cleanupPath, 'utf8'), 'complete');
    assert.equal(published.syncId, 'bmo-sync-final');
    assert.equal(published.attemptId, session.attemptId);
    assert.match(logAtPublication, /Process exited with code 1/);
  } finally {
    fs.rmSync(appRoot, { recursive: true, force: true });
  }
});

test('Moomoo OAuth launches only a validated handoff and passes it to the managed runner', async () => {
  const appRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-visible-auth-moomoo-'));
  try {
    const runnerPath = path.join(appRoot, 'capture-moomoo-runner.js');
    const capturePath = path.join(appRoot, 'capture.json');
    const catalogPath = path.join(appRoot, 'provider-catalog.json');
    fs.writeFileSync(
      runnerPath,
      [
        "const fs = require('node:fs');",
        "process.stdin.resume();",
        "process.stdin.on('end', () => {",
        "  fs.writeFileSync(process.argv[2], JSON.stringify({",
        "    url: process.env.BREAKTWENTY_MOOMOO_OAUTH_AUTHORIZATION_URL,",
        "    syncId: process.env.BREAKTWENTY_VISIBLE_AUTH_SYNC_ID,",
        "    attemptId: process.env.BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID,",
        "  }));",
        "});",
      ].join('\n'),
      'utf8',
    );
    fs.writeFileSync(
      catalogPath,
      JSON.stringify({
        moomoo: {
          displayName: 'Moomoo',
          backend: { runtimeState: { artifactKinds: [] } },
          desktop: {
            visibleAuth: {
              enabled: true,
              runnerCommand: [MANAGED_VISIBLE_AUTH_PYTHON, runnerPath, capturePath],
            },
          },
        },
      }),
      'utf8',
    );
    const broker = new VisibleAuthBroker({
      appRoot,
      backendApiUrl: 'http://127.0.0.1:8000/api',
      runtimeEnv: {
        PROVIDER_CATALOG_PATH: catalogPath,
        BREAKTWENTY_VISIBLE_AUTH_PYTHON: process.execPath,
      },
      getLaunchAuthToken: () => 'desktop-role-secret',
    });
    broker.registerRunnerGrant = async () => {};
    broker.revokeRunnerScope = async () => {};

    const rejected = await broker.launch({
      provider: 'moomoo',
      authorizationUrl: 'https://example.com/steal',
      syncId: 'sync-safe',
      attemptId: 'attempt-safe',
    });
    assert.equal(rejected.status, 'error');
    assert.equal(broker.sessions.has('moomoo'), false);

    const challenge = 'c'.repeat(43);
    const state = 's'.repeat(43);
    const authorizationUrl = 'https://webapi.moomoo.com/oauth2/authorize/confirm?'
      + `client_id=client-safe&code_challenge=${challenge}&code_challenge_method=S256`
      + '&redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fapi%2Fauth%2Fmoomoo%2Foauth%2Fcallback'
      + `&response_type=code&state=${state}`;
    const launched = await broker.launch({
      provider: 'moomoo',
      authorizationUrl,
      syncId: 'sync-safe',
      attemptId: 'attempt-safe',
    });
    assert.equal(launched.requestStatus, 'launched');
    const child = broker.sessions.get('moomoo').process;
    await new Promise((resolve, reject) => {
      child.once('close', resolve);
      child.once('error', reject);
    });
    assert.deepEqual(JSON.parse(fs.readFileSync(capturePath, 'utf8')), {
      url: authorizationUrl,
      syncId: 'sync-safe',
      attemptId: 'attempt-safe',
    });
  } finally {
    fs.rmSync(appRoot, { recursive: true, force: true });
  }
});
