const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  acquireDesktopStartupLock,
  releaseDesktopStartupLock,
  startupLockPath,
} = require('./startupLock');

function temporaryRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-startup-lock-'));
}

test('a concurrent launcher cannot replace the live first-launch lock', () => {
  const root = temporaryRoot();
  try {
    const first = acquireDesktopStartupLock(root);
    const before = fs.readFileSync(first.lockPath, 'utf8');

    assert.throws(
      () => acquireDesktopStartupLock(root),
      /already launching or running/,
    );
    assert.equal(fs.readFileSync(first.lockPath, 'utf8'), before);
    assert.equal(releaseDesktopStartupLock(first), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a dead launcher lock is recovered without broad directory deletion', () => {
  const root = temporaryRoot();
  try {
    const lockPath = startupLockPath(root);
    fs.writeFileSync(
      lockPath,
      `${JSON.stringify({
        version: 1,
        pid: 2_147_483_647,
        processStart: 'stale',
        nonce: 'a'.repeat(32),
        acquiredAt: new Date(0).toISOString(),
      })}\n`,
      { mode: 0o600 },
    );
    const unrelated = path.join(root, 'keep-me');
    fs.writeFileSync(unrelated, 'preserved', 'utf8');

    const recovered = acquireDesktopStartupLock(root);

    assert.notEqual(recovered.nonce, 'a'.repeat(32));
    assert.equal(fs.readFileSync(unrelated, 'utf8'), 'preserved');
    assert.equal(releaseDesktopStartupLock(recovered), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a paused first writer cannot publish over a concurrent initialized owner', async () => {
  const root = temporaryRoot();
  const readyPath = path.join(root, 'writer-ready');
  const continuePath = path.join(root, 'writer-continue');
  const helperPath = path.join(__dirname, 'startupLock.js');
  const child = spawn(
    process.execPath,
    [
      '-e',
      [
        'const fs = require("node:fs");',
        'const { acquireDesktopStartupLock } = require(process.argv[1]);',
        'try {',
        '  acquireDesktopStartupLock(process.argv[2], { beforePublish() {',
        '    fs.writeFileSync(process.argv[3], "ready");',
        '    const view = new Int32Array(new SharedArrayBuffer(4));',
        '    while (!fs.existsSync(process.argv[4])) Atomics.wait(view, 0, 0, 10);',
        '  } });',
        '  process.exit(2);',
        '} catch (error) {',
        '  process.exit(/already launching or running/.test(error.message) ? 0 : 3);',
        '}',
      ].join('\n'),
      helperPath,
      root,
      readyPath,
      continuePath,
    ],
    { stdio: 'ignore' },
  );
  try {
    const deadline = Date.now() + 5000;
    while (!fs.existsSync(readyPath) && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    assert.equal(fs.existsSync(readyPath), true);
    const winner = acquireDesktopStartupLock(root);
    fs.writeFileSync(continuePath, 'continue');
    const exitCode = await new Promise((resolve) => child.once('exit', resolve));
    assert.equal(exitCode, 0);
    assert.equal(releaseDesktopStartupLock(winner), true);
  } finally {
    if (child.exitCode === null) child.kill('SIGKILL');
    fs.rmSync(root, { recursive: true, force: true });
  }
});
