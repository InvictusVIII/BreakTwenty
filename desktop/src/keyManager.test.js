const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { BreakTwentyKeyManager, KEY_RECORD_FILE } = require('./keyManager');
const { evaluateStorageStatus } = require('./secureStorage');

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
