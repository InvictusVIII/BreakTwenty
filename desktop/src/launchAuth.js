const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const LAUNCH_TOKEN_FILE_NAME = 'launch-auth.token';
const TOKEN_BYTES = 32;

function validateLaunchToken(value) {
  const token = String(value || '');
  if (!/^[A-Za-z0-9+/]{43}=$/.test(token)) {
    throw new Error('BreakTwenty launch authentication token is invalid.');
  }
  const decoded = Buffer.from(token, 'base64');
  const valid = decoded.length === TOKEN_BYTES && decoded.toString('base64') === token;
  decoded.fill(0);
  if (!valid) {
    throw new Error('BreakTwenty launch authentication token must contain exactly 256 bits.');
  }
  return token;
}

function generateLaunchTokenBundle() {
  return {
    desktopToken: crypto.randomBytes(TOKEN_BYTES).toString('base64'),
    rendererToken: crypto.randomBytes(TOKEN_BYTES).toString('base64'),
  };
}

function launchTokenPath(desktopAuthDir) {
  return path.join(path.resolve(desktopAuthDir), LAUNCH_TOKEN_FILE_NAME);
}

function ensurePrivateLaunchDirectory(desktopAuthDir, { create = false } = {}) {
  const directory = path.resolve(desktopAuthDir);
  if (create) {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  }
  const directoryStat = fs.lstatSync(directory);
  if (directoryStat.isSymbolicLink() || !directoryStat.isDirectory()) {
    throw new Error(`BreakTwenty launch authentication directory is not a real directory: ${directory}`);
  }
  if (process.platform !== 'win32') {
    fs.chmodSync(directory, 0o700);
    const privateStat = fs.lstatSync(directory);
    if (privateStat.isSymbolicLink() || !privateStat.isDirectory() || (privateStat.mode & 0o777) !== 0o700) {
      throw new Error(`BreakTwenty launch authentication directory must have mode 0700: ${directory}`);
    }
  }
  return directory;
}

function rotateLaunchTokenFile(desktopAuthDir) {
  const directory = ensurePrivateLaunchDirectory(desktopAuthDir, { create: true });
  const target = launchTokenPath(directory);
  const temporary = path.join(
    directory,
    `.${LAUNCH_TOKEN_FILE_NAME}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`,
  );
  const tokens = generateLaunchTokenBundle();
  try {
    fs.writeFileSync(
      temporary,
      `${JSON.stringify({ version: 1, ...tokens })}\n`,
      { encoding: 'ascii', flag: 'wx', mode: 0o600 },
    );
    fs.chmodSync(temporary, 0o600);
    fs.renameSync(temporary, target);
    fs.chmodSync(target, 0o600);
    return tokens;
  } finally {
    try {
      if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
    } catch (_error) {
      // The atomic destination remains authoritative if temp cleanup fails.
    }
  }
}

function readLaunchTokenFile(desktopAuthDir) {
  const directory = ensurePrivateLaunchDirectory(desktopAuthDir);
  const target = launchTokenPath(directory);
  const fileStat = fs.lstatSync(target);
  if (fileStat.isSymbolicLink() || !fileStat.isFile()) {
    throw new Error(`BreakTwenty launch authentication token is not a file: ${target}`);
  }
  if (process.platform !== 'win32' && (fileStat.mode & 0o077) !== 0) {
    throw new Error(`BreakTwenty launch authentication token must have mode 0600: ${target}`);
  }
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(target, 'ascii'));
  } catch (_error) {
    throw new Error('BreakTwenty launch authentication token file is invalid.');
  }
  if (
    !payload
    || payload.version !== 1
    || Object.keys(payload).sort().join(',') !== 'desktopToken,rendererToken,version'
  ) {
    throw new Error('BreakTwenty launch authentication token file is invalid.');
  }
  const tokens = {
    desktopToken: validateLaunchToken(payload.desktopToken),
    rendererToken: validateLaunchToken(payload.rendererToken),
  };
  if (tokens.desktopToken === tokens.rendererToken) {
    throw new Error('BreakTwenty launch authentication roles are invalid.');
  }
  return tokens;
}

module.exports = {
  LAUNCH_TOKEN_FILE_NAME,
  generateLaunchTokenBundle,
  ensurePrivateLaunchDirectory,
  launchTokenPath,
  readLaunchTokenFile,
  rotateLaunchTokenFile,
  validateLaunchToken,
};
