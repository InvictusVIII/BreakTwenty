const fs = require('node:fs');
const path = require('node:path');
const { safeStorage } = require('electron');
const { APP_BRAND_NAME } = require('./brand');

const INSECURE_LINUX_BACKEND = 'basic_text';
const SUPPORTED_LINUX_BACKENDS = new Set([
  'gnome_libsecret',
  'kwallet',
  'kwallet5',
  'kwallet6',
]);

function evaluateStorageStatus({ platform, encryptionAvailable, backend }) {
  const supportedBackend = (
    (platform === 'linux' && SUPPORTED_LINUX_BACKENDS.has(backend))
    || (platform === 'win32' && backend === 'dpapi')
    || (platform === 'darwin' && backend === 'keychain')
  );
  const secure = (
    encryptionAvailable
    && supportedBackend
  );
  return {
    secure,
    encryptionAvailable,
    backend,
  };
}

function selectedStorageBackend() {
  if (process.platform === 'linux') {
    try {
      return safeStorage.getSelectedStorageBackend();
    } catch (_error) {
      return 'unknown';
    }
  }
  if (process.platform === 'win32') return 'dpapi';
  if (process.platform === 'darwin') return 'keychain';
  return 'unknown';
}

async function secureStorageStatus() {
  const backend = selectedStorageBackend();
  const encryptionAvailable = await safeStorage.isAsyncEncryptionAvailable();
  return evaluateStorageStatus({
    platform: process.platform,
    encryptionAvailable,
    backend,
  });
}

async function requireSecureStorage() {
  const status = await secureStorageStatus();
  if (!status.encryptionAvailable) {
    throw new Error('OS-backed secure storage is temporarily unavailable. Unlock the OS credential store and restart BreakTwenty.');
  }
  if (process.platform === 'linux' && status.backend === INSECURE_LINUX_BACKEND) {
    throw new Error(
      `${APP_BRAND_NAME} requires Secret Service or KWallet on Linux; Electron selected the insecure basic_text backend.`,
    );
  }
  if (!status.secure) {
    throw new Error(`${APP_BRAND_NAME} could not identify a supported OS secure-storage backend.`);
  }
  return status;
}

function atomicWriteFile(filePath, content, options = {}) {
  const mode = options.mode ?? 0o600;
  const directory = path.dirname(filePath);
  const temporaryPath = `${filePath}.tmp`;
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  try {
    fs.rmSync(temporaryPath, { force: true });
  } catch (_error) {
  }
  const descriptor = fs.openSync(temporaryPath, 'wx', mode);
  try {
    fs.writeFileSync(descriptor, content, options.encoding ? { encoding: options.encoding } : undefined);
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  fs.renameSync(temporaryPath, filePath);
  try {
    fs.chmodSync(filePath, mode);
  } catch (_error) {
  }
  try {
    const directoryDescriptor = fs.openSync(directory, 'r');
    try {
      fs.fsyncSync(directoryDescriptor);
    } finally {
      fs.closeSync(directoryDescriptor);
    }
  } catch (_error) {
  }
}

async function encryptProtectedString(value) {
  await requireSecureStorage();
  return safeStorage.encryptStringAsync(value);
}

async function decryptProtectedString(value) {
  await requireSecureStorage();
  const decrypted = await safeStorage.decryptStringAsync(value);
  return {
    plaintext: decrypted.result,
    shouldReEncrypt: decrypted.shouldReEncrypt === true,
  };
}

module.exports = {
  atomicWriteFile,
  decryptProtectedString,
  encryptProtectedString,
  evaluateStorageStatus,
  requireSecureStorage,
  secureStorageStatus,
};
