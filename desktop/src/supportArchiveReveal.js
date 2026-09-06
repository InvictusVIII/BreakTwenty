const fs = require('node:fs');
const path = require('node:path');

const SUPPORT_ARCHIVE_METADATA_MAX_BYTES = 16 * 1024;
const LOCAL_DESKTOP_USER_ID = 1;
const SUPPORT_ARCHIVE_KINDS = new Set(['provider_run', 'all_recent']);

function normalizeSupportArchiveId(value) {
  const normalized = String(value || '').trim().toLowerCase();
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(normalized)
    ? normalized
    : '';
}

function readBoundedArchiveMetadata(metadataPath) {
  let descriptor;
  try {
    const noFollow = Number(fs.constants.O_NOFOLLOW || 0);
    descriptor = fs.openSync(metadataPath, fs.constants.O_RDONLY | noFollow);
    const stats = fs.fstatSync(descriptor);
    if (!stats.isFile() || stats.size <= 0 || stats.size > SUPPORT_ARCHIVE_METADATA_MAX_BYTES) {
      return null;
    }
    const payload = JSON.parse(fs.readFileSync(descriptor, { encoding: 'utf8' }));
    return payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : null;
  } catch (_) {
    return null;
  } finally {
    if (descriptor !== undefined) {
      fs.closeSync(descriptor);
    }
  }
}

function resolveOwnedSupportArchive({
  logDir,
  archiveId,
  userId = LOCAL_DESKTOP_USER_ID,
} = {}) {
  const normalizedId = normalizeSupportArchiveId(archiveId);
  const normalizedLogDir = String(logDir || '').trim();
  const normalizedUserId = Number(userId);
  if (!normalizedId || !normalizedLogDir || !Number.isSafeInteger(normalizedUserId) || normalizedUserId <= 0) {
    return '';
  }

  const archivesDir = path.resolve(normalizedLogDir, 'exports', 'archives');
  const targetPath = path.resolve(archivesDir, `${normalizedId}.zip`);
  const metadataPath = path.resolve(archivesDir, `${normalizedId}.json`);
  if (path.dirname(targetPath) !== archivesDir || path.dirname(metadataPath) !== archivesDir) {
    return '';
  }

  try {
    const metadataStats = fs.lstatSync(metadataPath);
    const targetStats = fs.lstatSync(targetPath);
    if (
      metadataStats.isSymbolicLink()
      || !metadataStats.isFile()
      || targetStats.isSymbolicLink()
      || !targetStats.isFile()
    ) {
      return '';
    }
  } catch (_) {
    return '';
  }

  const metadata = readBoundedArchiveMetadata(metadataPath);
  if (
    !metadata
    || metadata.archive_id !== normalizedId
    || metadata.user_id !== normalizedUserId
  ) {
    return null;
  }
  const filename = String(metadata.filename || '').trim();
  if (
    !filename
    || filename.length > 180
    || !filename.toLowerCase().endsWith('.zip')
    || /[\\/\0\r\n]/.test(filename)
    || path.basename(filename) !== filename
  ) {
    return null;
  }
  const kind = String(metadata.kind || '').trim();
  if (!SUPPORT_ARCHIVE_KINDS.has(kind)) return null;
  const provider = String(metadata.provider || '').trim();
  if (
    kind === 'provider_run'
    && (
      !provider
      || provider.length > 80
      || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(provider)
      || path.basename(provider) !== provider
    )
  ) {
    return null;
  }
  return {
    sourcePath: targetPath,
    metadataPath,
    metadata,
    filename,
    kind,
    provider,
  };
}

function resolveOwnedSupportArchivePath(options = {}) {
  return resolveOwnedSupportArchive(options)?.sourcePath || '';
}

function materializeOwnedSupportArchive(options = {}) {
  const resolved = resolveOwnedSupportArchive(options);
  const normalizedLogDir = String(options.logDir || '').trim();
  if (!resolved || !normalizedLogDir) return '';
  const exportsDir = path.resolve(normalizedLogDir, 'exports');
  const destinationDir = resolved.kind === 'provider_run'
    ? path.resolve(exportsDir, resolved.provider)
    : exportsDir;
  if (destinationDir !== exportsDir && path.dirname(destinationDir) !== exportsDir) return '';
  const destinationPath = path.resolve(destinationDir, resolved.filename);
  if (path.dirname(destinationPath) !== destinationDir) return '';
  let temporaryMetadataPath = '';
  try {
    // Make the user-facing copy owner-addressable before materializing it.
    // Provider deletion and archive retention can then remove the copy without
    // scanning or guessing ownership in the shared exports tree.
    const materializedRelativePath = path.relative(exportsDir, destinationPath);
    const updatedMetadata = {
      ...resolved.metadata,
      materialized_relative_path: materializedRelativePath.split(path.sep).join('/'),
    };
    temporaryMetadataPath = `${resolved.metadataPath}.${process.pid}.${Date.now()}.tmp`;
    const descriptor = fs.openSync(
      temporaryMetadataPath,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL,
      0o600,
    );
    try {
      fs.writeFileSync(descriptor, JSON.stringify(updatedMetadata));
      if (process.platform !== 'win32') fs.fchmodSync(descriptor, 0o600);
    } finally {
      fs.closeSync(descriptor);
    }
    fs.renameSync(temporaryMetadataPath, resolved.metadataPath);

    fs.mkdirSync(destinationDir, { recursive: true, mode: 0o700 });
    fs.copyFileSync(resolved.sourcePath, destinationPath);
    if (process.platform !== 'win32') fs.chmodSync(destinationPath, 0o600);
  } catch (_) {
    try {
      fs.unlinkSync(destinationPath);
    } catch (_) {
      // Nothing was materialized, or cleanup already succeeded.
    }
    if (temporaryMetadataPath) {
      try {
        fs.unlinkSync(temporaryMetadataPath);
      } catch (_) {
        // The atomic metadata replacement succeeded, or no temp file exists.
      }
    }
    return '';
  }
  return destinationPath;
}

module.exports = {
  LOCAL_DESKTOP_USER_ID,
  SUPPORT_ARCHIVE_METADATA_MAX_BYTES,
  SUPPORT_ARCHIVE_KINDS,
  materializeOwnedSupportArchive,
  normalizeSupportArchiveId,
  resolveOwnedSupportArchivePath,
};
