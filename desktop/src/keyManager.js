const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {
  atomicWriteFile,
  decryptProtectedString,
  encryptProtectedString,
  requireSecureStorage,
} = require('./secureStorage');
const { APP_BRAND_NAME } = require('./brand');

const KEY_BYTES = 32;
const KEY_RECORD_VERSION = 1;
const KEY_BUNDLE_VERSION = 1;
const KEY_RECORD_FILE = 'wrapped-key-bundle.json';
const KEY_RECORD_SCHEMA = 'breaktwenty.wrapped-key-bundle';
const KEY_BUNDLE_SCHEMA = 'breaktwenty.key-bundle';
const KEY_PROTECTOR = 'electron.safeStorage';
const SQLITE_HEADER = Buffer.from('SQLite format 3\0', 'binary');

function isoNow() {
  return new Date().toISOString();
}

function databaseFileState(databasePath) {
  if (!fs.existsSync(databasePath)) {
    return 'missing';
  }
  if (fs.statSync(databasePath).size === 0) {
    return 'empty';
  }
  const descriptor = fs.openSync(databasePath, 'r');
  const header = Buffer.alloc(SQLITE_HEADER.length);
  try {
    const bytesRead = fs.readSync(descriptor, header, 0, header.length, 0);
    return bytesRead === SQLITE_HEADER.length && header.equals(SQLITE_HEADER)
      ? 'plaintext'
      : 'encrypted-or-corrupt';
  } finally {
    header.fill(0);
    fs.closeSync(descriptor);
  }
}

function decodeKey(value, label) {
  const text = String(value || '').trim();
  if (!/^[A-Za-z0-9+/]{43}=$/.test(text)) {
    throw new Error(`The protected ${APP_BRAND_NAME} ${label} is invalid.`);
  }
  const decoded = Buffer.from(text, 'base64');
  if (decoded.length !== KEY_BYTES || decoded.toString('base64') !== text) {
    decoded.fill(0);
    throw new Error(`The protected ${APP_BRAND_NAME} ${label} is invalid.`);
  }
  return decoded;
}

function decodeCiphertext(value) {
  const text = String(value || '').trim();
  if (!text || !/^[A-Za-z0-9+/]+={0,2}$/.test(text)) {
    throw new Error('invalid wrapped ciphertext');
  }
  const decoded = Buffer.from(text, 'base64');
  if (decoded.length === 0 || decoded.toString('base64') !== text) {
    decoded.fill(0);
    throw new Error('invalid wrapped ciphertext');
  }
  return decoded;
}

class BreakTwentyKeyManager {
  constructor({ dataDir, dbPath, secureStorage = null }) {
    this.dbPath = dbPath;
    this.recordPath = path.join(dataDir, KEY_RECORD_FILE);
    this.keyMaterial = null;
    this.secureStorage = secureStorage || {
      decryptProtectedString,
      encryptProtectedString,
      requireSecureStorage,
    };
  }

  async loadOrCreate() {
    const storageStatus = await this.secureStorage.requireSecureStorage();
    const databaseState = databaseFileState(this.dbPath);
    if (databaseState === 'plaintext') {
      throw new Error(
        `${APP_BRAND_NAME} found a plaintext SQLite database. This build accepts only SQLCipher-encrypted databases and left the file unchanged.`,
      );
    }
    if (databaseState === 'empty') {
      throw new Error(
        `${APP_BRAND_NAME} found an empty database file and will not replace or initialize it.`,
      );
    }
    if (fs.existsSync(this.recordPath)) {
      this.keyMaterial = await this.loadExistingRecord(storageStatus);
      return this.keyMaterial;
    }
    if (databaseState === 'encrypted-or-corrupt') {
      throw new Error(
        `The encrypted ${APP_BRAND_NAME} database has no wrapped key bundle. ${APP_BRAND_NAME} will not reset or replace it.`,
      );
    }
    this.keyMaterial = await this.createRecord(storageStatus.backend);
    return this.keyMaterial;
  }

  async loadExistingRecord(storageStatus) {
    let record;
    try {
      record = JSON.parse(fs.readFileSync(this.recordPath, 'utf8'));
    } catch (_error) {
      throw new Error(
        `The ${APP_BRAND_NAME} wrapped key bundle is unreadable or corrupt. ${APP_BRAND_NAME} will not reset the database.`,
      );
    }
    if (
      !record
      || record.schema !== KEY_RECORD_SCHEMA
      || record.version !== KEY_RECORD_VERSION
      || record.protector?.type !== KEY_PROTECTOR
      || record.ciphertext?.encoding !== 'base64'
      || typeof record.ciphertext?.value !== 'string'
      || typeof record.createdAt !== 'string'
      || typeof record.updatedAt !== 'string'
    ) {
      throw new Error(
        `The ${APP_BRAND_NAME} wrapped key bundle format is unsupported or corrupt.`,
      );
    }

    let wrapped = null;
    let plaintext = '';
    let databaseKey = null;
    let appEncryptionKey = null;
    try {
      wrapped = decodeCiphertext(record.ciphertext.value);
      const decrypted = await this.secureStorage.decryptProtectedString(wrapped);
      plaintext = decrypted.plaintext;
      const bundle = JSON.parse(plaintext);
      if (
        !bundle
        || bundle.schema !== KEY_BUNDLE_SCHEMA
        || bundle.version !== KEY_BUNDLE_VERSION
        || bundle.keys?.database?.algorithm !== 'raw-256'
        || bundle.keys?.database?.encoding !== 'base64'
        || bundle.keys?.fieldEncryption?.algorithm !== 'aes-256-gcm'
        || bundle.keys?.fieldEncryption?.encoding !== 'base64'
      ) {
        throw new Error('unsupported bundle');
      }
      databaseKey = decodeKey(bundle.keys.database.value, 'database key');
      appEncryptionKey = decodeKey(bundle.keys.fieldEncryption.value, 'app-encryption key');
      if (decrypted.shouldReEncrypt) {
        await this.writeRecord({
          plaintext,
          wrappingBackend: storageStatus.backend,
          createdAt: record.createdAt,
          rewrappedAt: isoNow(),
        });
      }
      return { databaseKey, appEncryptionKey };
    } catch (_error) {
      if (databaseKey) databaseKey.fill(0);
      if (appEncryptionKey) appEncryptionKey.fill(0);
      throw new Error(
        `${APP_BRAND_NAME} could not unwrap the protected key bundle in this OS user context. The database was not changed.`,
      );
    } finally {
      if (wrapped) wrapped.fill(0);
      plaintext = '';
    }
  }

  async createRecord(wrappingBackend) {
    const databaseKey = crypto.randomBytes(KEY_BYTES);
    const appEncryptionKey = crypto.randomBytes(KEY_BYTES);
    const bundleText = JSON.stringify({
      schema: KEY_BUNDLE_SCHEMA,
      version: KEY_BUNDLE_VERSION,
      keys: {
        database: {
          algorithm: 'raw-256',
          encoding: 'base64',
          value: databaseKey.toString('base64'),
        },
        fieldEncryption: {
          algorithm: 'aes-256-gcm',
          encoding: 'base64',
          value: appEncryptionKey.toString('base64'),
        },
      },
    });
    try {
      const createdAt = isoNow();
      await this.writeRecord({
        plaintext: bundleText,
        wrappingBackend,
        createdAt,
      });
    } catch (error) {
      databaseKey.fill(0);
      appEncryptionKey.fill(0);
      throw error;
    }
    return { databaseKey, appEncryptionKey };
  }

  async writeRecord({ plaintext, wrappingBackend, createdAt, rewrappedAt = null }) {
    let wrapped = null;
    try {
      wrapped = await this.secureStorage.encryptProtectedString(plaintext);
      const updatedAt = rewrappedAt || createdAt;
      atomicWriteFile(
        this.recordPath,
        `${JSON.stringify({
          schema: KEY_RECORD_SCHEMA,
          version: KEY_RECORD_VERSION,
          protector: {
            type: KEY_PROTECTOR,
            backend: wrappingBackend || 'unknown',
          },
          ciphertext: {
            encoding: 'base64',
            value: wrapped.toString('base64'),
          },
          createdAt,
          updatedAt,
          ...(rewrappedAt ? { rewrappedAt } : {}),
        }, null, 2)}\n`,
        { encoding: 'utf8', mode: 0o600 },
      );
    } finally {
      if (wrapped) wrapped.fill(0);
    }
  }

  bootstrapPayload({ desktopToken, rendererToken }) {
    if (!this.keyMaterial) {
      throw new Error(`${APP_BRAND_NAME} protected key material has not been loaded.`);
    }
    let desktopTokenBytes = null;
    let rendererTokenBytes = null;
    try {
      desktopTokenBytes = decodeKey(desktopToken, 'desktop launch authentication token');
      rendererTokenBytes = decodeKey(rendererToken, 'renderer launch authentication token');
      return Buffer.from(`${JSON.stringify({
        version: 2,
        databaseKey: this.keyMaterial.databaseKey.toString('base64'),
        appEncryptionKey: this.keyMaterial.appEncryptionKey.toString('base64'),
        desktopLaunchToken: desktopTokenBytes.toString('base64'),
        rendererLaunchToken: rendererTokenBytes.toString('base64'),
      })}\n`, 'utf8');
    } finally {
      if (desktopTokenBytes) desktopTokenBytes.fill(0);
      if (rendererTokenBytes) rendererTokenBytes.fill(0);
    }
  }

  clear() {
    if (!this.keyMaterial) {
      return;
    }
    this.keyMaterial.databaseKey.fill(0);
    this.keyMaterial.appEncryptionKey.fill(0);
    this.keyMaterial = null;
  }
}

module.exports = {
  BreakTwentyKeyManager,
  KEY_RECORD_FILE,
};
