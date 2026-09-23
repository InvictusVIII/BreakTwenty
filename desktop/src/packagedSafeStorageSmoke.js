const fs = require('node:fs');
const path = require('node:path');
const { app } = require('electron');
const { APP_BRAND_NAME } = require('./brand');
const {
  resolvePackagedIdentity,
  resolveSafeStorageIdentity,
} = require('./appIdentity');
const {
  atomicWriteFile,
  decryptProtectedString,
  encryptProtectedString,
} = require('./secureStorage');
const {
  PACKAGED_SAFE_STORAGE_SMOKE_TEXT,
} = require('./packagedSafeStorageSmokeProtocol');
const { requestGracefulSmokeExit } = require('./packagedSmokeLifecycle');

const mode = String(process.env.BREAKTWENTY_SAFE_STORAGE_SMOKE_MODE || '').trim();
const smokeDir = String(process.env.BREAKTWENTY_SAFE_STORAGE_SMOKE_DIR || '').trim();
const smokeCiphertextPath = smokeDir ? path.join(smokeDir, 'ciphertext.bin') : '';

function writeSmokeResult(value) {
  atomicWriteFile(
    path.join(smokeDir, `${mode}.json`),
    `${JSON.stringify(value)}\n`,
    { encoding: 'utf8', mode: 0o600 },
  );
}

async function runSmoke() {
  if (!path.isAbsolute(smokeDir)) {
    throw new Error('The packaged Safe Storage smoke directory must be absolute.');
  }
  fs.mkdirSync(smokeDir, { recursive: true, mode: 0o700 });
  if (mode === 'smoke-encrypt') {
    const ciphertext = await encryptProtectedString(PACKAGED_SAFE_STORAGE_SMOKE_TEXT);
    try {
      atomicWriteFile(smokeCiphertextPath, ciphertext, { mode: 0o600 });
    } finally {
      ciphertext.fill(0);
    }
    writeSmokeResult({ ok: true, mode, storageIdentity: app.getName() });
    return;
  }
  if (mode === 'smoke-decrypt') {
    const ciphertext = fs.readFileSync(smokeCiphertextPath);
    try {
      const decrypted = await decryptProtectedString(ciphertext);
      if (decrypted.plaintext !== PACKAGED_SAFE_STORAGE_SMOKE_TEXT) {
        throw new Error('The packaged Safe Storage smoke plaintext did not match.');
      }
    } finally {
      ciphertext.fill(0);
    }
    writeSmokeResult({ ok: true, mode, storageIdentity: app.getName() });
    return;
  }
  throw new Error(`Unsupported Safe Storage smoke mode: ${mode || 'missing'}`);
}

app.setName(process.platform === 'darwin'
  ? resolveSafeStorageIdentity({
    isPackaged: app.isPackaged,
    identity: resolvePackagedIdentity(),
  })
  : APP_BRAND_NAME);
if (path.isAbsolute(smokeDir)) {
  app.setPath('userData', path.join(smokeDir, 'profile'));
  app.setPath('sessionData', path.join(smokeDir, 'profile'));
}

app.whenReady().then(async () => {
  try {
    await runSmoke();
    requestGracefulSmokeExit(app, 0);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    requestGracefulSmokeExit(app, 1);
  }
});
