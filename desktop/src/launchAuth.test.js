const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  LAUNCH_TOKEN_FILE_NAME,
  readLaunchTokenFile,
  rotateLaunchTokenFile,
} = require('./launchAuth');

function withTemporaryDirectory(callback) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-launch-auth-'));
  try {
    callback(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test('rotation tightens an existing auth directory and token file', { skip: process.platform === 'win32' }, () => {
  withTemporaryDirectory((root) => {
    const authDirectory = path.join(root, 'auth');
    fs.mkdirSync(authDirectory, { mode: 0o775 });

    const generated = rotateLaunchTokenFile(authDirectory);
    const directoryMode = fs.lstatSync(authDirectory).mode & 0o777;
    const tokenMode = fs.lstatSync(path.join(authDirectory, LAUNCH_TOKEN_FILE_NAME)).mode & 0o777;

    assert.equal(directoryMode, 0o700);
    assert.equal(tokenMode, 0o600);
    assert.deepEqual(readLaunchTokenFile(authDirectory), generated);
  });
});

test('rotation rejects a symlink launch-auth directory', { skip: process.platform === 'win32' }, () => {
  withTemporaryDirectory((root) => {
    const realDirectory = path.join(root, 'real');
    const linkedDirectory = path.join(root, 'linked');
    fs.mkdirSync(realDirectory, { mode: 0o700 });
    fs.symlinkSync(realDirectory, linkedDirectory, 'dir');

    assert.throws(
      () => rotateLaunchTokenFile(linkedDirectory),
      /not a real directory/,
    );
  });
});

test('reader rejects a symlink launch-token file', { skip: process.platform === 'win32' }, () => {
  withTemporaryDirectory((root) => {
    const sourceDirectory = path.join(root, 'source');
    const authDirectory = path.join(root, 'auth');
    fs.mkdirSync(sourceDirectory, { mode: 0o700 });
    fs.mkdirSync(authDirectory, { mode: 0o700 });
    rotateLaunchTokenFile(sourceDirectory);
    fs.symlinkSync(
      path.join(sourceDirectory, LAUNCH_TOKEN_FILE_NAME),
      path.join(authDirectory, LAUNCH_TOKEN_FILE_NAME),
    );

    assert.throws(
      () => readLaunchTokenFile(authDirectory),
      /not a file/,
    );
  });
});
