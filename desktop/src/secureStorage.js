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
  'secret_service_or_kwallet',
  'secret_portal',
]);
const LINUX_ASYNC_BACKEND_BY_CIPHERTEXT_PREFIX = new Map([
  ['v10', INSECURE_LINUX_BACKEND],
  ['v11', 'secret_service_or_kwallet'],
  ['v12', 'secret_portal'],
]);
const STORAGE_PROBE_TEXT = 'breaktwenty-secure-storage-probe';
const TEMPORARY_STORAGE_ERROR_CODE = 'BREAKTWENTY_SECURE_STORAGE_TEMPORARILY_UNAVAILABLE';
const TRANSIENT_DECRYPT_RETRY_DELAYS_MS = Object.freeze([150, 500]);

function waitForDelay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function temporaryStorageError(message, cause = null) {
  const error = new Error(message, cause ? { cause } : undefined);
  error.code = TEMPORARY_STORAGE_ERROR_CODE;
  return error;
}

function isTemporaryStorageError(error) {
  return (
    error?.code === TEMPORARY_STORAGE_ERROR_CODE
    || String(error?.message || '').includes(
      'safeStorage.decryptStringAsync is temporarily unavailable',
    )
  );
}

function classifyLinuxAsyncBackend(ciphertext, selectedBackend = 'unknown') {
  if (!Buffer.isBuffer(ciphertext) || ciphertext.length < 3) return 'unknown';
  const prefix = ciphertext.subarray(0, 3).toString('ascii');
  const probedBackend = LINUX_ASYNC_BACKEND_BY_CIPHERTEXT_PREFIX.get(prefix) || 'unknown';
  if (probedBackend !== 'secret_service_or_kwallet') return probedBackend;
  return SUPPORTED_LINUX_BACKENDS.has(selectedBackend)
    ? selectedBackend
    : probedBackend;
}

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

function selectedStorageBackend({ platform = process.platform, storage = safeStorage } = {}) {
  if (platform === 'linux') {
    try {
      return storage.getSelectedStorageBackend();
    } catch (_error) {
      return 'unknown';
    }
  }
  if (platform === 'win32') return 'dpapi';
  if (platform === 'darwin') return 'keychain';
  return 'unknown';
}

async function secureStorageStatus({ platform = process.platform, storage = safeStorage } = {}) {
  const encryptionAvailable = await storage.isAsyncEncryptionAvailable();
  let backend = selectedStorageBackend({ platform, storage });
  if (platform === 'linux' && encryptionAvailable) {
    let probe = null;
    try {
      probe = await storage.encryptStringAsync(STORAGE_PROBE_TEXT);
      backend = classifyLinuxAsyncBackend(probe, backend);
    } catch (_error) {
      backend = 'unknown';
    } finally {
      if (probe) probe.fill(0);
    }
  }
  return evaluateStorageStatus({
    platform,
    encryptionAvailable,
    backend,
  });
}

async function requireSecureStorage(options = {}) {
  const status = await secureStorageStatus(options);
  const platform = options.platform || process.platform;
  if (!status.encryptionAvailable) {
    throw temporaryStorageError(
      'OS-backed secure storage is temporarily unavailable. Unlock the OS credential store and restart BreakTwenty.',
    );
  }
  if (platform === 'linux' && status.backend === INSECURE_LINUX_BACKEND) {
    throw new Error(
      `${APP_BRAND_NAME} could not use Secret Service or KWallet on Linux and refused Electron's insecure plaintext fallback. Unlock or start your system keyring, then restart ${APP_BRAND_NAME}.`,
    );
  }
  if (!status.secure) {
    throw new Error(`${APP_BRAND_NAME} could not verify a supported OS secure-storage backend.`);
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

async function encryptProtectedString(
  value,
  { storage = safeStorage, requireStorage = requireSecureStorage } = {},
) {
  await requireStorage({ storage });
  return storage.encryptStringAsync(value);
}

async function decryptProtectedString(
  value,
  {
    storage = safeStorage,
    requireStorage = requireSecureStorage,
    retryDelaysMs = TRANSIENT_DECRYPT_RETRY_DELAYS_MS,
    wait = waitForDelay,
  } = {},
) {
  await requireStorage({ storage });
  for (let attempt = 0; attempt <= retryDelaysMs.length; attempt += 1) {
    try {
      const decrypted = await storage.decryptStringAsync(value);
      return {
        plaintext: decrypted.result,
        shouldReEncrypt: decrypted.shouldReEncrypt === true,
      };
    } catch (error) {
      if (!isTemporaryStorageError(error)) {
        throw error;
      }
      if (attempt >= retryDelaysMs.length) {
        throw temporaryStorageError(
          'OS-backed secure storage remained temporarily unavailable after bounded retries. Unlock the OS credential store and restart BreakTwenty.',
          error,
        );
      }
      await wait(retryDelaysMs[attempt]);
    }
  }
  throw temporaryStorageError('OS-backed secure storage is temporarily unavailable.');
}

module.exports = {
  atomicWriteFile,
  classifyLinuxAsyncBackend,
  decryptProtectedString,
  encryptProtectedString,
  evaluateStorageStatus,
  isTemporaryStorageError,
  requireSecureStorage,
  secureStorageStatus,
  TEMPORARY_STORAGE_ERROR_CODE,
  TRANSIENT_DECRYPT_RETRY_DELAYS_MS,
};
