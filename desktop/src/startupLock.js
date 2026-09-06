const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const { ensurePrivateLaunchDirectory } = require('./launchAuth');

const STARTUP_LOCK_DIR_NAME = 'desktop-launch.lock';

function processStartIdentity(pid) {
  if (process.platform === 'linux') {
    try {
      const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
      const commandEnd = stat.lastIndexOf(')');
      const fieldsAfterCommand = stat.slice(commandEnd + 2).trim().split(/\s+/);
      return fieldsAfterCommand[19] || '';
    } catch (_error) {
      return '';
    }
  }
  if (process.platform === 'darwin') {
    const result = spawnSync('ps', ['-o', 'lstart=', '-p', String(pid)], {
      encoding: 'utf8',
      windowsHide: true,
    });
    return result.status === 0 ? String(result.stdout || '').trim() : '';
  }
  if (process.platform === 'win32') {
    const result = spawnSync(
      'powershell.exe',
      [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `(Get-Process -Id ${Number(pid)} -ErrorAction Stop).StartTime.ToUniversalTime().Ticks`,
      ],
      { encoding: 'utf8', windowsHide: true },
    );
    return result.status === 0 ? String(result.stdout || '').trim() : '';
  }
  return '';
}

function processIsOwnerAlive(owner) {
  const pid = Number(owner?.pid || 0);
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
  } catch (error) {
    return error?.code === 'EPERM';
  }
  const recordedStart = String(owner?.processStart || '');
  const observedStart = processStartIdentity(pid);
  return !recordedStart || !observedStart || recordedStart === observedStart;
}

function startupLockPath(desktopAuthDir) {
  return path.join(path.resolve(desktopAuthDir), STARTUP_LOCK_DIR_NAME);
}

function readLockOwner(lockPath) {
  try {
    const lockStat = fs.lstatSync(lockPath);
    if (lockStat.isSymbolicLink() || !lockStat.isFile()) {
      throw new Error('startup lock is not a real file');
    }
    const owner = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
    if (
      owner?.version !== 1
      || !Number.isInteger(owner.pid)
      || owner.pid <= 0
      || !/^[a-f0-9]{32}$/.test(String(owner.nonce || ''))
    ) {
      return null;
    }
    return owner;
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    if (error instanceof SyntaxError) return null;
    throw error;
  }
}

function removeStaleLock(lockPath) {
  const stalePath = `${lockPath}.stale.${process.pid}.${crypto.randomBytes(8).toString('hex')}`;
  try {
    fs.renameSync(lockPath, stalePath);
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
  fs.unlinkSync(stalePath);
  return true;
}

function acquireDesktopStartupLock(
  desktopAuthDir,
  { ownerPid = process.pid, beforePublish = null } = {},
) {
  const directory = ensurePrivateLaunchDirectory(desktopAuthDir, { create: true });
  const lockPath = startupLockPath(directory);
  const pid = Number(ownerPid);
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new Error('BreakTwenty desktop startup lock owner is invalid.');
  }

  for (let attempt = 0; attempt < 4; attempt += 1) {
    const nonce = crypto.randomBytes(16).toString('hex');
    const owner = {
      version: 1,
      pid,
      processStart: processStartIdentity(pid),
      nonce,
      acquiredAt: new Date().toISOString(),
    };
    const temporaryPath = path.join(
      directory,
      `.${STARTUP_LOCK_DIR_NAME}.${pid}.${crypto.randomBytes(8).toString('hex')}.tmp`,
    );
    try {
      fs.writeFileSync(
        temporaryPath,
        `${JSON.stringify(owner)}\n`,
        { encoding: 'utf8', flag: 'wx', mode: 0o600 },
      );
      if (process.platform !== 'win32') fs.chmodSync(temporaryPath, 0o600);
      if (typeof beforePublish === 'function') beforePublish(temporaryPath);
      fs.linkSync(temporaryPath, lockPath);
      return { desktopAuthDir: directory, lockPath, nonce, ownerPid: pid };
    } catch (error) {
      if (error?.code !== 'EEXIST') {
        throw error;
      }
      const existingOwner = readLockOwner(lockPath);
      if (existingOwner && processIsOwnerAlive(existingOwner)) {
        throw new Error(
          `BreakTwenty is already launching or running (launcher process ${existingOwner.pid}).`,
        );
      }
      removeStaleLock(lockPath);
    } finally {
      try {
        fs.unlinkSync(temporaryPath);
      } catch (_error) {
      }
    }
  }
  throw new Error('BreakTwenty could not acquire its desktop startup lock.');
}

function releaseDesktopStartupLock(lock) {
  if (!lock?.lockPath || !lock?.nonce) return false;
  let owner;
  try {
    owner = readLockOwner(lock.lockPath);
  } catch (_error) {
    return false;
  }
  if (!owner || owner.nonce !== lock.nonce || owner.pid !== Number(lock.ownerPid)) {
    return false;
  }
  const releasedPath = `${lock.lockPath}.released.${process.pid}.${crypto.randomBytes(8).toString('hex')}`;
  try {
    fs.renameSync(lock.lockPath, releasedPath);
    fs.unlinkSync(releasedPath);
    return true;
  } catch (_error) {
    return false;
  }
}

function assertDesktopStartupLockOwner(desktopAuthDir, { ownerPid, nonce } = {}) {
  const lockPath = startupLockPath(desktopAuthDir);
  const owner = readLockOwner(lockPath);
  if (
    !owner
    || owner.pid !== Number(ownerPid)
    || owner.nonce !== String(nonce || '')
    || !processIsOwnerAlive(owner)
  ) {
    throw new Error('BreakTwenty must be started through its authenticated desktop launcher.');
  }
  return owner;
}

function takeOverDesktopStartupLock(
  desktopAuthDir,
  { ownerPid, nonce, nextOwnerPid = process.pid } = {},
) {
  const owner = assertDesktopStartupLockOwner(desktopAuthDir, { ownerPid, nonce });
  const lockPath = startupLockPath(desktopAuthDir);
  const nextPid = Number(nextOwnerPid);
  if (!Number.isInteger(nextPid) || nextPid <= 0) {
    throw new Error('BreakTwenty desktop startup lock owner is invalid.');
  }
  const nextOwner = {
    ...owner,
    pid: nextPid,
    processStart: processStartIdentity(nextPid),
  };
  const temporaryPath = path.join(
    path.dirname(lockPath),
    `.${STARTUP_LOCK_DIR_NAME}.${nextPid}.${crypto.randomBytes(8).toString('hex')}.transfer`,
  );
  try {
    fs.writeFileSync(
      temporaryPath,
      `${JSON.stringify(nextOwner)}\n`,
      { encoding: 'utf8', flag: 'wx', mode: 0o600 },
    );
    if (process.platform !== 'win32') fs.chmodSync(temporaryPath, 0o600);
    const currentOwner = readLockOwner(lockPath);
    if (
      !currentOwner
      || currentOwner.pid !== Number(ownerPid)
      || currentOwner.nonce !== String(nonce || '')
    ) {
      throw new Error('BreakTwenty desktop startup lock ownership changed during handoff.');
    }
    fs.renameSync(temporaryPath, lockPath);
    return {
      desktopAuthDir: path.resolve(desktopAuthDir),
      lockPath,
      nonce: nextOwner.nonce,
      ownerPid: nextPid,
    };
  } finally {
    try {
      fs.unlinkSync(temporaryPath);
    } catch (_error) {
    }
  }
}

function runCli() {
  const [command, desktopAuthDir, rawPid, nonce] = process.argv.slice(2);
  if (command === 'acquire') {
    const lock = acquireDesktopStartupLock(desktopAuthDir, { ownerPid: Number(rawPid) });
    process.stdout.write(`${lock.nonce}\n`);
    return;
  }
  if (command === 'release') {
    const lockPath = startupLockPath(desktopAuthDir);
    releaseDesktopStartupLock({
      lockPath,
      nonce: String(nonce || ''),
      ownerPid: Number(rawPid),
    });
    return;
  }
  throw new Error('Usage: startupLock.js acquire|release <desktop-auth-dir> <owner-pid> [nonce]');
}

if (require.main === module) {
  try {
    runCli();
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  acquireDesktopStartupLock,
  assertDesktopStartupLockOwner,
  processIsOwnerAlive,
  releaseDesktopStartupLock,
  startupLockPath,
  takeOverDesktopStartupLock,
};
