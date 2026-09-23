const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { BreakTwentyKeyManager, KEY_RECORD_FILE } = require('./keyManager');
const {
  classifyLinuxAsyncBackend,
  decryptProtectedString,
  evaluateStorageStatus,
  secureStorageStatus,
  TEMPORARY_STORAGE_ERROR_CODE,
} = require('./secureStorage');

function fakeSecureStorage() {
  const wrappingKey = crypto.randomBytes(32);
  let shouldReEncrypt = false;
  return {
    async requireSecureStorage() {
      return { secure: true, backend: 'test' };
    },
    async encryptProtectedString(value) {
      const nonce = crypto.randomBytes(12);
      const cipher = crypto.createCipheriv('aes-256-gcm', wrappingKey, nonce);
      return Buffer.concat([
        nonce,
        cipher.update(value, 'utf8'),
        cipher.final(),
        cipher.getAuthTag(),
      ]);
    },
    async decryptProtectedString(value) {
      const nonce = value.subarray(0, 12);
      const tag = value.subarray(value.length - 16);
      const decipher = crypto.createDecipheriv('aes-256-gcm', wrappingKey, nonce);
      decipher.setAuthTag(tag);
      return {
        plaintext: Buffer.concat([
          decipher.update(value.subarray(12, value.length - 16)),
          decipher.final(),
        ]).toString('utf8'),
        shouldReEncrypt,
      };
    },
    requestRewrap() {
      shouldReEncrypt = true;
    },
  };
}

function temporaryRuntime() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-key-manager-'));
  const dataDir = path.join(root, 'data');
  fs.mkdirSync(dataDir, { recursive: true });
  return {
    root,
    dataDir,
    dbPath: path.join(dataDir, 'breaktwenty.db'),
  };
}

test('creates an atomically wrapped two-key bundle and reopens it', async (context) => {
  const runtime = temporaryRuntime();
  context.after(() => fs.rmSync(runtime.root, { recursive: true, force: true }));
  const storage = fakeSecureStorage();
  const firstManager = new BreakTwentyKeyManager({ ...runtime, secureStorage: storage });
  const created = await firstManager.loadOrCreate();
  const databaseKey = Buffer.from(created.databaseKey);
  const appEncryptionKey = Buffer.from(created.appEncryptionKey);
  const recordText = fs.readFileSync(path.join(runtime.dataDir, KEY_RECORD_FILE), 'utf8');

  assert.equal(recordText.includes(databaseKey.toString('base64')), false);
  assert.equal(recordText.includes(appEncryptionKey.toString('base64')), false);
  const record = JSON.parse(recordText);
  assert.equal(record.schema, 'breaktwenty.wrapped-key-bundle');
  assert.equal(record.version, 1);
  assert.equal(record.protector.type, 'electron.safeStorage');
  assert.equal(record.ciphertext.encoding, 'base64');
  assert.equal(record.migration, undefined);
  assert.equal(fs.statSync(path.join(runtime.dataDir, KEY_RECORD_FILE)).mode & 0o777, 0o600);

  const secondManager = new BreakTwentyKeyManager({ ...runtime, secureStorage: storage });
  const reopened = await secondManager.loadOrCreate();
  assert.deepEqual(reopened.databaseKey, databaseKey);
  assert.deepEqual(reopened.appEncryptionKey, appEncryptionKey);
  firstManager.clear();
  secondManager.clear();
  databaseKey.fill(0);
  appEncryptionKey.fill(0);
});

test('rewraps an unchanged key bundle when the OS protector rotates', async (context) => {
  const runtime = temporaryRuntime();
  context.after(() => fs.rmSync(runtime.root, { recursive: true, force: true }));
  const storage = fakeSecureStorage();
  const firstManager = new BreakTwentyKeyManager({ ...runtime, secureStorage: storage });
  const created = await firstManager.loadOrCreate();
  const databaseKey = Buffer.from(created.databaseKey);
  const appEncryptionKey = Buffer.from(created.appEncryptionKey);
  const recordPath = path.join(runtime.dataDir, KEY_RECORD_FILE);
  const before = JSON.parse(fs.readFileSync(recordPath, 'utf8'));
  firstManager.clear();

  storage.requestRewrap();
  const secondManager = new BreakTwentyKeyManager({ ...runtime, secureStorage: storage });
  const reopened = await secondManager.loadOrCreate();
  const after = JSON.parse(fs.readFileSync(recordPath, 'utf8'));

  assert.deepEqual(reopened.databaseKey, databaseKey);
  assert.deepEqual(reopened.appEncryptionKey, appEncryptionKey);
  assert.notEqual(after.ciphertext.value, before.ciphertext.value);
  assert.equal(after.createdAt, before.createdAt);
  assert.equal(typeof after.rewrappedAt, 'string');
  assert.equal(after.updatedAt, after.rewrappedAt);
  secondManager.clear();
  databaseKey.fill(0);
  appEncryptionKey.fill(0);
});

test('rejects corrupt wrapped data without replacing it', async (context) => {
  const runtime = temporaryRuntime();
  context.after(() => fs.rmSync(runtime.root, { recursive: true, force: true }));
  const storage = fakeSecureStorage();
  const manager = new BreakTwentyKeyManager({ ...runtime, secureStorage: storage });
  await manager.loadOrCreate();
  manager.clear();
  const recordPath = path.join(runtime.dataDir, KEY_RECORD_FILE);
  const record = JSON.parse(fs.readFileSync(recordPath, 'utf8'));
  record.ciphertext.value = `${record.ciphertext.value.slice(0, -4)}AAAA`;
  fs.writeFileSync(recordPath, JSON.stringify(record));

  await assert.rejects(
    new BreakTwentyKeyManager({ ...runtime, secureStorage: storage }).loadOrCreate(),
    /could not unwrap/i,
  );
  assert.equal(JSON.parse(fs.readFileSync(recordPath, 'utf8')).ciphertext.value, record.ciphertext.value);
});

test('refuses an encrypted database when its wrapped key record is missing', async (context) => {
  const runtime = temporaryRuntime();
  context.after(() => fs.rmSync(runtime.root, { recursive: true, force: true }));
  fs.writeFileSync(runtime.dbPath, crypto.randomBytes(64));

  await assert.rejects(
    new BreakTwentyKeyManager({
      ...runtime,
      secureStorage: fakeSecureStorage(),
    }).loadOrCreate(),
    /no wrapped key bundle/i,
  );
});

test('refuses a plaintext database without creating a wrapped key record', async (context) => {
  const runtime = temporaryRuntime();
  context.after(() => fs.rmSync(runtime.root, { recursive: true, force: true }));
  const plaintext = Buffer.concat([
    Buffer.from('SQLite format 3\0', 'binary'),
    crypto.randomBytes(64),
  ]);
  fs.writeFileSync(runtime.dbPath, plaintext);

  await assert.rejects(
    new BreakTwentyKeyManager({
      ...runtime,
      secureStorage: fakeSecureStorage(),
    }).loadOrCreate(),
    /plaintext SQLite database/i,
  );
  assert.deepEqual(fs.readFileSync(runtime.dbPath), plaintext);
  assert.equal(fs.existsSync(path.join(runtime.dataDir, KEY_RECORD_FILE)), false);
  plaintext.fill(0);
});

test('refuses an empty database without replacing it', async (context) => {
  const runtime = temporaryRuntime();
  context.after(() => fs.rmSync(runtime.root, { recursive: true, force: true }));
  fs.writeFileSync(runtime.dbPath, '');

  await assert.rejects(
    new BreakTwentyKeyManager({
      ...runtime,
      secureStorage: fakeSecureStorage(),
    }).loadOrCreate(),
    /empty database file/i,
  );
  assert.equal(fs.statSync(runtime.dbPath).size, 0);
  assert.equal(fs.existsSync(path.join(runtime.dataDir, KEY_RECORD_FILE)), false);
});

test('Linux basic_text is classified as insecure', () => {
  assert.deepEqual(
    evaluateStorageStatus({
      platform: 'linux',
      encryptionAvailable: true,
      backend: 'basic_text',
    }),
    {
      secure: false,
      encryptionAvailable: true,
      backend: 'basic_text',
    },
  );
  assert.equal(
    evaluateStorageStatus({
      platform: 'linux',
      encryptionAvailable: true,
      backend: 'gnome_libsecret',
    }).secure,
    true,
  );
  assert.equal(
    evaluateStorageStatus({
      platform: 'linux',
      encryptionAvailable: true,
      backend: 'secret_portal',
    }).secure,
    true,
  );
  assert.equal(
    evaluateStorageStatus({
      platform: 'linux',
      encryptionAvailable: true,
      backend: 'unsupported_backend',
    }).secure,
    false,
  );
  assert.equal(
    evaluateStorageStatus({
      platform: 'win32',
      encryptionAvailable: true,
      backend: 'dpapi',
    }).secure,
    true,
  );
  assert.equal(
    evaluateStorageStatus({
      platform: 'darwin',
      encryptionAvailable: true,
      backend: 'keychain',
    }).secure,
    true,
  );
  assert.equal(
    evaluateStorageStatus({
      platform: 'freebsd',
      encryptionAvailable: true,
      backend: 'keychain',
    }).secure,
    false,
  );
});

test('Linux async ciphertext identifies the provider that actually encrypted it', () => {
  assert.equal(
    classifyLinuxAsyncBackend(Buffer.from('v10ciphertext'), 'gnome_libsecret'),
    'basic_text',
  );
  assert.equal(
    classifyLinuxAsyncBackend(Buffer.from('v11ciphertext'), 'basic_text'),
    'secret_service_or_kwallet',
  );
  assert.equal(
    classifyLinuxAsyncBackend(Buffer.from('v11ciphertext'), 'kwallet6'),
    'kwallet6',
  );
  assert.equal(
    classifyLinuxAsyncBackend(Buffer.from('v12ciphertext'), 'basic_text'),
    'secret_portal',
  );
  assert.equal(
    classifyLinuxAsyncBackend(Buffer.from('v13ciphertext'), 'gnome_libsecret'),
    'unknown',
  );
  assert.equal(classifyLinuxAsyncBackend(Buffer.from('v1')), 'unknown');
  assert.equal(classifyLinuxAsyncBackend('v11ciphertext'), 'unknown');
});

test('Linux secure-storage status trusts the async provider instead of the legacy label', async () => {
  const secureStatus = await secureStorageStatus({
    platform: 'linux',
    storage: {
      async isAsyncEncryptionAvailable() {
        return true;
      },
      getSelectedStorageBackend() {
        return 'basic_text';
      },
      async encryptStringAsync(value) {
        assert.equal(value, 'breaktwenty-secure-storage-probe');
        return Buffer.from('v11ciphertext');
      },
    },
  });
  assert.deepEqual(secureStatus, {
    secure: true,
    encryptionAvailable: true,
    backend: 'secret_service_or_kwallet',
  });

  const fallbackStatus = await secureStorageStatus({
    platform: 'linux',
    storage: {
      async isAsyncEncryptionAvailable() {
        return true;
      },
      getSelectedStorageBackend() {
        return 'gnome_libsecret';
      },
      async encryptStringAsync() {
        return Buffer.from('v10ciphertext');
      },
    },
  });
  assert.deepEqual(fallbackStatus, {
    secure: false,
    encryptionAvailable: true,
    backend: 'basic_text',
  });
});

test('temporary OS decrypt failures receive only the bounded retry budget', async () => {
  let calls = 0;
  const waits = [];
  const decrypted = await decryptProtectedString(Buffer.from('ciphertext'), {
    storage: {
      async decryptStringAsync() {
        calls += 1;
        if (calls < 3) {
          throw new Error('safeStorage.decryptStringAsync is temporarily unavailable. Please try again.');
        }
        return { result: 'plaintext', shouldReEncrypt: false };
      },
    },
    requireStorage: async () => ({ secure: true, backend: 'keychain' }),
    retryDelaysMs: [10, 20],
    wait: async (delay) => waits.push(delay),
  });

  assert.deepEqual(decrypted, { plaintext: 'plaintext', shouldReEncrypt: false });
  assert.equal(calls, 3);
  assert.deepEqual(waits, [10, 20]);
});

test('permanent OS decrypt failures are never retried', async () => {
  let calls = 0;
  await assert.rejects(
    decryptProtectedString(Buffer.from('ciphertext'), {
      storage: {
        async decryptStringAsync() {
          calls += 1;
          throw new Error('permanent decrypt failure');
        },
      },
      requireStorage: async () => ({ secure: true, backend: 'keychain' }),
      retryDelaysMs: [10, 20],
      wait: async () => assert.fail('permanent failures must not wait'),
    }),
    /permanent decrypt failure/,
  );
  assert.equal(calls, 1);
});

test('exhausted temporary decrypt failures remain distinguishable at key startup', async (context) => {
  const runtime = temporaryRuntime();
  context.after(() => fs.rmSync(runtime.root, { recursive: true, force: true }));
  const storage = fakeSecureStorage();
  const firstManager = new BreakTwentyKeyManager({ ...runtime, secureStorage: storage });
  await firstManager.loadOrCreate();
  firstManager.clear();
  const recordPath = path.join(runtime.dataDir, KEY_RECORD_FILE);
  const before = fs.readFileSync(recordPath);
  const temporaryError = new Error('OS-backed secure storage remained temporarily unavailable.');
  temporaryError.code = TEMPORARY_STORAGE_ERROR_CODE;

  await assert.rejects(
    new BreakTwentyKeyManager({
      ...runtime,
      secureStorage: {
        async requireSecureStorage() {
          return { secure: true, backend: 'keychain' };
        },
        async decryptProtectedString() {
          throw temporaryError;
        },
      },
    }).loadOrCreate(),
    (error) => error.code === TEMPORARY_STORAGE_ERROR_CODE,
  );
  assert.deepEqual(fs.readFileSync(recordPath), before);
});
