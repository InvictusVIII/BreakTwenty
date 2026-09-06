const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  OPENSSL_SHA256,
  OPENSSL_VERSION,
  ensurePinnedMacX64OpenSsl,
  resolvePythonRuntimeInstallPolicy,
  versionAtLeast,
} = require('./python_runtime_install_policy');

function createOpenSslRoot(root) {
  for (const relative of [
    'include/openssl/ssl.h',
    'lib/libssl.a',
    'lib/libcrypto.a',
    'bin/openssl',
  ]) {
    const destination = path.join(root, relative);
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, 'test\n', 'utf8');
  }
}

function successfulMacSpawn(cacheRoot, rustVersion = 'rustc 1.91.0 (test)') {
  const opensslRoot = path.join(cacheRoot, `openssl-${OPENSSL_VERSION}-macos-x64`);
  return (command, args) => {
    let stdout = '';
    if (command === 'rustc') stdout = rustVersion;
    else if (command === 'cargo') stdout = 'cargo 1.91.0 (test)';
    else if (command === 'xcrun') stdout = '/usr/bin/clang';
    else if (command === 'make') stdout = 'GNU Make 3.81';
    else if (command === 'perl') stdout = 'This is perl 5, version 30';
    else if (command === path.join(opensslRoot, 'bin', 'openssl')) stdout = `OpenSSL ${OPENSSL_VERSION} 9 Jun 2026`;
    else if (args.includes('platform.system()')) stdout = '{"system":"Darwin","machine":"x86_64"}';
    return { error: null, status: stdout ? 0 : 1, stdout, stderr: '' };
  };
}

test('non-macOS-x64 runtimes remain wheel-only without probing build tools', async () => {
  const policy = await resolvePythonRuntimeInstallPolicy({
    pythonBin: '/python',
    targetTriple: 'aarch64-apple-darwin',
    pipOnlyBinary: ':all:',
    pipNoBinary: '',
    baseEnv: {},
    spawn: () => {
      throw new Error('unexpected probe');
    },
  });
  assert.equal(policy.requiresMacX64BuildPrerequisites, false);
  assert.equal(policy.pipNoBinary, '');
});

test('Intel macOS uses cryptography source with pinned static OpenSSL', async () => {
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-openssl-policy-'));
  const opensslRoot = path.join(cacheRoot, `openssl-${OPENSSL_VERSION}-macos-x64`);
  createOpenSslRoot(opensslRoot);
  try {
    const policy = await resolvePythonRuntimeInstallPolicy({
      pythonBin: '/python',
      targetTriple: 'x86_64-apple-darwin',
      pipOnlyBinary: ':all:',
      pipNoBinary: '',
      opensslCacheRoot: cacheRoot,
      baseEnv: { PATH: '/usr/bin' },
      hostPlatform: 'darwin',
      hostArch: 'x64',
      spawn: successfulMacSpawn(cacheRoot),
    });
    assert.equal(policy.requiresMacX64BuildPrerequisites, true);
    assert.equal(policy.pipNoBinary, 'cryptography');
    assert.equal(policy.env.PATH, `${path.dirname('/python')}${path.delimiter}/usr/bin`);
    assert.equal(policy.env.OPENSSL_DIR, opensslRoot);
    assert.equal(policy.env.OPENSSL_STATIC, '1');
    assert.equal(policy.env.MACOSX_DEPLOYMENT_TARGET, '13.0');
    assert.equal(policy.env.ARCHFLAGS, '-arch x86_64');
  } finally {
    fs.rmSync(cacheRoot, { recursive: true, force: true });
  }
});

test('Intel macOS source builds reject an obsolete Rust toolchain', async () => {
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-openssl-policy-'));
  try {
    await assert.rejects(
      resolvePythonRuntimeInstallPolicy({
        pythonBin: '/python',
        targetTriple: 'x86_64-apple-darwin',
        hostPlatform: 'darwin',
        hostArch: 'x64',
        opensslCacheRoot: cacheRoot,
        baseEnv: {},
        spawn: successfulMacSpawn(cacheRoot, 'rustc 1.82.0 (test)'),
      }),
      /Rust 1\.83\.0 or newer/,
    );
  } finally {
    fs.rmSync(cacheRoot, { recursive: true, force: true });
  }
});

test('Intel macOS source builds reject cross-architecture hosts', async () => {
  await assert.rejects(
    resolvePythonRuntimeInstallPolicy({
      pythonBin: '/python',
      targetTriple: 'x86_64-apple-darwin',
      hostPlatform: 'darwin',
      hostArch: 'arm64',
      baseEnv: {},
    }),
    /must be assembled on Intel macOS/,
  );
});

test('cached OpenSSL archive is rejected when its SHA-256 does not match', async () => {
  const cacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-openssl-policy-'));
  const archive = path.join(cacheRoot, 'downloads', `openssl-${OPENSSL_VERSION}.tar.gz`);
  fs.mkdirSync(path.dirname(archive), { recursive: true });
  fs.writeFileSync(archive, 'tampered\n', 'utf8');
  try {
    await assert.rejects(
      ensurePinnedMacX64OpenSsl({ cacheRoot }),
      /failed SHA-256 verification/,
    );
    assert.notEqual(
      crypto.createHash('sha256').update(fs.readFileSync(archive)).digest('hex'),
      OPENSSL_SHA256,
    );
  } finally {
    fs.rmSync(cacheRoot, { recursive: true, force: true });
  }
});

test('Rust semantic version comparison honors the cryptography minimum', () => {
  assert.equal(versionAtLeast([1, 83, 0], [1, 83, 0]), true);
  assert.equal(versionAtLeast([1, 91, 0], [1, 83, 0]), true);
  assert.equal(versionAtLeast([1, 82, 9], [1, 83, 0]), false);
});
