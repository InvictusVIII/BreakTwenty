const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const zlib = require('node:zlib');

const {
  APP_INCIDENT_MAX_COUNT,
  APP_INCIDENT_TOTAL_MAX_BYTES,
  AppDiagnostics,
  createSanitizingLogger,
  formatLocalFilenameTimestamp,
  sanitizeLogText,
} = require('./appDiagnostics');

function readZipEntries(archive) {
  const entries = new Map();
  let offset = 0;
  while (offset + 30 <= archive.length && archive.readUInt32LE(offset) === 0x04034b50) {
    const method = archive.readUInt16LE(offset + 8);
    const compressedSize = archive.readUInt32LE(offset + 18);
    const filenameLength = archive.readUInt16LE(offset + 26);
    const extraLength = archive.readUInt16LE(offset + 28);
    const filenameStart = offset + 30;
    const dataStart = filenameStart + filenameLength + extraLength;
    const dataEnd = dataStart + compressedSize;
    const filename = archive.subarray(filenameStart, filenameStart + filenameLength).toString('utf8');
    const compressed = archive.subarray(dataStart, dataEnd);
    const data = method === 8 ? zlib.inflateRawSync(compressed) : compressed;
    entries.set(filename, data.toString('utf8'));
    offset = dataEnd;
  }
  return entries;
}

test('application diagnostic filenames use the requested timezone and DST abbreviation', () => {
  assert.equal(
    formatLocalFilenameTimestamp('2026-09-08T23:28:41.643Z', 'America/Toronto'),
    '2026-09-08_19-28-41_EDT',
  );
  assert.equal(
    formatLocalFilenameTimestamp('2026-01-08T23:28:41.643Z', 'America/Toronto'),
    '2026-01-08_18-28-41_EST',
  );
  assert.equal(
    formatLocalFilenameTimestamp('2026-09-08T23:28:41.643Z', '../invalid'),
    '2026-09-08_23-28-41_UTC',
  );
});

test('application diagnostics redact sensitive lines, bearer values, and local paths', () => {
  const localRoot = path.join(os.tmpdir(), 'private-user-root');
  const nestedPrivateRoot = path.join(localRoot, 'FinApp', 'BreakTwenty-private', 'runtime');
  const sanitized = sanitizeLogText([
    `loaded ${localRoot}/data/logs/main.log`,
    `runtime ${nestedPrivateRoot}/data/logs`,
    'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789',
    'ordinary backend health timeout',
    'password=do-not-keep-this',
    'account balance was 123.45 CAD',
  ].join('\n'), [localRoot, nestedPrivateRoot]);

  assert.match(sanitized, /\[LOCAL_PATH\]\/data\/logs\/main\.log/);
  assert.match(sanitized, /runtime \[LOCAL_PATH\]\/data\/logs/);
  assert.doesNotMatch(sanitized, /BreakTwenty-private/);
  assert.doesNotMatch(sanitized, /abcdefghijklmnopqrstuvwxyz|do-not-keep-this/);
  assert.match(sanitized, /ordinary backend health timeout/);
  assert.doesNotMatch(sanitized, /123\.45 CAD/);
  assert.match(sanitized, /\[REDACTED DATA-BEARING LOG LINE\]/);
  assert.equal(
    sanitized.split('\n').filter((line) => line === '[REDACTED SENSITIVE LOG LINE]').length,
    2,
  );
});

test('application diagnostics redact Windows and macOS path representations', () => {
  const windowsRoot = 'C:\\Users\\Alice Example\\AppData\\Roaming\\BreakTwenty';
  const macRoot = '/Users/Alice Example/Library/Application Support/BreakTwenty';
  const sanitized = sanitizeLogText([
    'windows-forward c:/users/alice example/appdata/roaming/breaktwenty/data/logs/main.log',
    'windows-json C:\\\\Users\\\\Alice Example\\\\AppData\\\\Roaming\\\\BreakTwenty\\\\data\\\\logs',
    'mac-file-url file:///Users/Alice%20Example/Library/Application%20Support/BreakTwenty/data/logs',
  ].join('\n'), [windowsRoot, macRoot]);

  assert.match(sanitized, /windows-forward \[LOCAL_PATH\]\/data\/logs\/main\.log/);
  assert.match(sanitized, /windows-json \[LOCAL_PATH\]\\\\data\\\\logs/);
  assert.match(sanitized, /mac-file-url file:\/\/\[LOCAL_PATH\]\/data\/logs/);
  assert.doesNotMatch(sanitized, /Alice Example|Alice%20Example/i);
});

test('application diagnostics consume multiline cookie header arrays and embedded cookie lines', () => {
  const sanitized = sanitizeLogText([
    'HttpError: response headers {',
    "  'set-cookie': [",
    '    "_gh_sess=fixture-session-material; Path=/; HttpOnly; Secure",',
    '    "_octo=fixture-octo-material; Domain=.example.test; SameSite=Lax",',
    '    "logged_in=fixture-login-material; Path=/; Secure",',
    '    "custom_session=fixture-custom-material"',
    '  ],',
    "  Cookie: [",
    '    "anonymous_cookie=fixture-anonymous-material; preference=fixture-preference-material"',
    '  ],',
    "  'content-type': 'text/plain'",
    '}',
    '"__Host-user_session_same_site=tail-start-material; Path=/; Secure"',
    'ordinary updater retry message',
  ].join('\n'));

  assert.doesNotMatch(
    sanitized,
    /_gh_sess|_octo|logged_in|custom_session|anonymous_cookie|fixture-session|fixture-octo|fixture-login|fixture-custom|fixture-anonymous|fixture-preference|tail-start-material/i,
  );
  assert.match(sanitized, /content-type/);
  assert.match(sanitized, /ordinary updater retry message/);
});

test('updater logger redacts its active GitHub credential before writing', () => {
  const activeCredential = 'opaque-private-release-credential-fixture';
  const captured = [];
  const baseLogger = Object.fromEntries(
    ['debug', 'info', 'warn', 'error'].map((level) => [
      level,
      (...args) => captured.push(JSON.stringify(args)),
    ]),
  );
  const logger = createSanitizingLogger(baseLogger, {
    exactSecrets: () => [activeCredential],
  });

  logger.error(
    `Update request failed for token ${activeCredential}`,
    {
      requestHeaders: { Authorization: `token ${activeCredential}` },
      releaseUrl: `https://x-access-token:${activeCredential}@github.example.test/releases`,
    },
  );

  const logged = captured.join('\n');
  assert.doesNotMatch(logged, new RegExp(activeCredential, 'i'));
  assert.doesNotMatch(logged, /x-access-token:/i);
  assert.match(logged, /REDACTED/);

  const updaterSource = fs.readFileSync(path.join(__dirname, 'appUpdater.js'), 'utf8');
  assert.match(updaterSource, /autoUpdater\.logger = createSanitizingLogger/);
  assert.match(updaterSource, /exactSecrets: \(\) => \[this\.authToken\]/);
});

test('application incidents are bounded by count and expire after seven days', (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-diagnostics-'));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const electronLogPath = path.join(root, 'main.log');
  const backendLogPath = path.join(root, 'backend.log');
  fs.writeFileSync(electronLogPath, 'renderer error without private data\n');
  fs.writeFileSync(backendLogPath, 'backend health timeout\n');
  let now = new Date('2026-08-26T20:00:00.000Z');
  const diagnostics = new AppDiagnostics({
    logDir: root,
    appVersion: '1.0.0-test',
    electronLogPath,
    backendLogPath,
    redactionRoots: [root],
    now: () => new Date(now),
  });

  for (let index = 0; index < APP_INCIDENT_MAX_COUNT + 3; index += 1) {
    diagnostics.capture({
      trigger: 'test_incident',
      summary: `incident ${index}`,
      backend: { state: 'running', path: root },
      probes: [{ ok: false, message: 'timed out' }],
    });
    now = new Date(now.getTime() + 1000);
  }

  const incidents = diagnostics.list();
  assert.equal(incidents.length, APP_INCIDENT_MAX_COUNT);
  assert.equal(incidents[0].summary, `incident ${APP_INCIDENT_MAX_COUNT + 2}`);
  const newestManifest = fs.readFileSync(
    path.join(root, 'app-incidents', incidents[0].incidentId, 'manifest.json'),
    'utf8',
  );
  assert.doesNotMatch(newestManifest, new RegExp(root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.match(newestManifest, /\[LOCAL_PATH\]/);

  now = new Date(now.getTime() + (8 * 24 * 60 * 60 * 1000));
  assert.deepEqual(diagnostics.list(), []);
});

test('application incident finalization records the recovery outcome', (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-diagnostics-'));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const diagnostics = new AppDiagnostics({
    logDir: root,
    now: () => new Date('2026-08-26T20:00:00.000Z'),
  });
  const incident = diagnostics.capture({ trigger: 'health_supervisor_unresponsive' });
  diagnostics.finalize(incident.incidentId, {
    outcome: 'recovered',
    recovery: { attempted: true, recovered: true },
  });

  const [updated] = diagnostics.list();
  assert.equal(updated.outcome, 'recovered');
  assert.deepEqual(updated.recovery, { attempted: true, recovered: true });
});

test('application incidents preserve pre-recovery evidence while refreshing final logs', (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-diagnostics-'));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const electronLogPath = path.join(root, 'main.log');
  const backendLogPath = path.join(root, 'backend.log');
  fs.writeFileSync(electronLogPath, 'recovery started\n');
  fs.writeFileSync(backendLogPath, 'stack sample 1/3\n');
  const diagnostics = new AppDiagnostics({
    logDir: root,
    electronLogPath,
    backendLogPath,
  });
  const incident = diagnostics.capture({ trigger: 'health_supervisor_unresponsive' });
  diagnostics.refreshLogs(incident.incidentId, { suffix: '.pre-recovery' });

  fs.appendFileSync(backendLogPath, 'replacement backend healthy\n');
  diagnostics.refreshLogs(incident.incidentId, {
    electronMarker: 'renderer recovery finalized completion=soft_acknowledged',
  });

  const incidentDir = path.join(root, 'app-incidents', incident.incidentId);
  assert.match(fs.readFileSync(path.join(incidentDir, 'embedded-backend-process.pre-recovery.log'), 'utf8'), /stack sample 1\/3/);
  assert.match(fs.readFileSync(path.join(incidentDir, 'electron-main.log'), 'utf8'), /recovery finalized/);
});

test('application incidents and exports include the packaged runtime fingerprint when available', async (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-diagnostics-'));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const runtimeFingerprintPath = path.join(root, 'python-runtime-fingerprint.json');
  fs.writeFileSync(runtimeFingerprintPath, JSON.stringify({
    schemaVersion: 1,
    fingerprintSha256: 'a'.repeat(64),
    python: { version: '3.12.12' },
    dependencies: { httpx: '0.27.2' },
  }));
  const diagnostics = new AppDiagnostics({
    logDir: root,
    runtimeFingerprintPath,
    now: () => new Date('2026-08-26T20:00:00.000Z'),
  });

  const incident = diagnostics.capture({ trigger: 'runtime_fingerprint_fixture' });
  const manifest = JSON.parse(fs.readFileSync(
    path.join(root, 'app-incidents', incident.incidentId, 'manifest.json'),
    'utf8',
  ));

  assert.equal(manifest.app.runtimeFingerprint.fingerprintSha256, 'a'.repeat(64));
  assert.equal(manifest.app.runtimeFingerprint.dependencies.httpx, '0.27.2');

  const destination = path.join(root, 'runtime-fingerprint-export.zip');
  await diagnostics.exportIncident(incident.incidentId, destination);
  const exportedManifest = JSON.parse(
    readZipEntries(fs.readFileSync(destination)).get('manifest.json'),
  );
  assert.equal(exportedManifest.app.runtimeFingerprint.fingerprintSha256, 'a'.repeat(64));
});

test('application incident pruning enforces the aggregate byte cap', (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-diagnostics-'));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  let now = new Date('2026-08-26T20:00:00.000Z');
  const diagnostics = new AppDiagnostics({
    logDir: root,
    now: () => new Date(now),
  });

  for (let index = 0; index < 5; index += 1) {
    const incident = diagnostics.capture({
      trigger: 'byte_cap_fixture',
      summary: `incident ${index}`,
    });
    fs.writeFileSync(
      path.join(root, 'app-incidents', incident.incidentId, 'bounded-fixture.log'),
      Buffer.alloc(2 * 1024 * 1024),
    );
    now = new Date(now.getTime() + 1000);
  }

  diagnostics.prune();
  const incidents = diagnostics.list();
  const retainedBytes = incidents.reduce((total, incident) => total + incident.bytes, 0);
  assert.ok(retainedBytes <= APP_INCIDENT_TOTAL_MAX_BYTES);
  assert.ok(incidents.length < 5);
  assert.equal(incidents[0].summary, 'incident 4');
});

test('application incident export produces a bounded zip archive', async (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-diagnostics-'));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const diagnostics = new AppDiagnostics({
    logDir: root,
    now: () => new Date('2026-08-26T20:00:00.000Z'),
  });
  const incident = diagnostics.capture({ trigger: 'user_requested_app_logs' });
  const destination = path.join(root, 'export.zip');

  const result = await diagnostics.exportIncident(incident.incidentId, destination);

  assert.ok(result.bytes > 0);
  const archive = fs.readFileSync(destination);
  assert.equal(archive.subarray(0, 2).toString('ascii'), 'PK');
});

test('application incident export re-sanitizes legacy files so no cookie or updater token material survives', async (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-diagnostics-'));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const diagnostics = new AppDiagnostics({
    logDir: root,
    now: () => new Date('2026-08-26T20:00:00.000Z'),
  });
  const incident = diagnostics.capture({ trigger: 'legacy_sanitizer_fixture' });
  const incidentDir = path.join(root, 'app-incidents', incident.incidentId);
  const githubToken = ['github', 'pat', 'FAKEFAKEFAKEFAKEFAKEFAKEFAKE'].join('_');
  const classicToken = ['ghp', 'FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE'].join('_');
  fs.writeFileSync(path.join(incidentDir, 'electron-main.log'), [
    'Updater request failed with response headers {',
    "  'set-cookie': [",
    '    "_gh_sess=legacy-session-material; Path=/; HttpOnly; Secure",',
    '    "_octo=legacy-octo-material; Domain=.example.test; SameSite=Lax",',
    '    "logged_in=legacy-login-material; Path=/; Secure"',
    '  ]',
    '}',
    `Authorization: token ${classicToken}`,
    `release metadata url=https://github.example.test/releases?token=${githubToken}`,
    'signed release url=https://assets.example.test/file?X-Amz-Credential=release-credential-material&X-Amz-Signature=release-signature-material',
    'ordinary updater retry message',
  ].join('\n'));
  const manifestPath = path.join(incidentDir, 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  manifest.details = {
    headers: {
      Cookie: 'session=manifest-cookie-material; preference=manifest-preference-material',
      Authorization: `Bearer ${githubToken}`,
    },
    releaseUrl: `https://x-access-token:${classicToken}@github.example.test/releases`,
    CSC_LINK: 'release-certificate-material',
  };
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  const destination = path.join(root, 'export.zip');

  await diagnostics.exportIncident(incident.incidentId, destination);

  const entries = readZipEntries(fs.readFileSync(destination));
  const exportedText = [...entries.values()].join('\n');
  assert.ok(entries.has('electron-main.log'));
  assert.ok(entries.has('manifest.json'));
  assert.doesNotMatch(
    exportedText,
    /_gh_sess|_octo|logged_in|legacy-session|legacy-octo|legacy-login|manifest-cookie|manifest-preference/i,
  );
  assert.doesNotMatch(
    exportedText,
    /github[_]pat_|gh[p]_|FAKEFAKE|x-access-token:|release-credential-material|release-signature-material|release-certificate-material/i,
  );
  assert.match(exportedText, /ordinary updater retry message/);
  assert.match(exportedText, /REDACTED/);
});
