const fs = require('node:fs');
const path = require('node:path');

const MIGRATION_RECOVERY_SCHEMA = 'breaktwenty.migration-recovery';
const MIGRATION_RECOVERY_VERSION = 1;

function recoveryPaths(databasePath) {
  const directory = path.join(path.dirname(databasePath), 'migration-recovery');
  return {
    directory,
    snapshotPath: path.join(directory, 'pre-migration.db'),
    manifestPath: path.join(directory, 'manifest.json'),
  };
}

function atomicWriteJson(filePath, value) {
  const directory = path.dirname(filePath);
  const temporaryPath = `${filePath}.tmp`;
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  fs.rmSync(temporaryPath, { force: true });
  const descriptor = fs.openSync(temporaryPath, 'wx', 0o600);
  try {
    fs.writeFileSync(descriptor, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  fs.renameSync(temporaryPath, filePath);
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

function assertHealthyState(state, label) {
  if (!state || state.integrity !== 'ok' || Number(state.foreignKeyErrors || 0) !== 0) {
    throw new Error(`${label} failed encrypted database validation.`);
  }
}

function assertSnapshotState(state) {
  assertHealthyState(state, 'Pre-migration snapshot');
  if (
    !Number.isSafeInteger(state.bytes)
    || state.bytes <= 0
    || typeof state.sha256 !== 'string'
    || !/^[a-f0-9]{64}$/.test(state.sha256)
  ) {
    throw new Error('Pre-migration snapshot returned invalid checksum metadata.');
  }
}

function sameRevisions(left, right) {
  return JSON.stringify(left || []) === JSON.stringify(right || []);
}

function readRecoveryManifest(paths, databasePath) {
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(paths.manifestPath, 'utf8'));
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw new Error('The database migration recovery manifest is unreadable; recovery files were preserved.');
  }
  const validStatuses = new Set(['prepared', 'recovering', 'recovery_failed', 'recovered', 'succeeded']);
  if (
    manifest?.schema !== MIGRATION_RECOVERY_SCHEMA
    || manifest?.version !== MIGRATION_RECOVERY_VERSION
    || manifest?.databaseFile !== path.basename(databasePath)
    || manifest?.snapshotFile !== path.basename(paths.snapshotPath)
    || !Array.isArray(manifest?.fromRevisions)
    || !Number.isSafeInteger(manifest?.snapshotBytes)
    || manifest.snapshotBytes <= 0
    || typeof manifest?.snapshotSha256 !== 'string'
    || !/^[a-f0-9]{64}$/.test(manifest.snapshotSha256)
    || !validStatuses.has(manifest?.status)
  ) {
    throw new Error('The database migration recovery manifest is invalid; recovery files were preserved.');
  }
  return manifest;
}

function removeNewDatabaseFiles(databasePath) {
  for (const filePath of [databasePath, `${databasePath}-wal`, `${databasePath}-shm`]) {
    fs.rmSync(filePath, { force: true });
  }
}

function runMigrationsSafely({
  databasePath,
  databaseExisted,
  inspect,
  createSnapshot,
  migrate,
  restoreSnapshot,
  discardNewDatabase = removeNewDatabaseFiles,
  log = () => {},
  now = () => new Date().toISOString(),
}) {
  if (!databaseExisted) {
    try {
      migrate();
      const createdState = inspect();
      assertHealthyState(createdState, 'New database migration');
      if (createdState.needsMigration) {
        throw new Error('New database migration did not reach the current schema head.');
      }
      return { migrated: true, recovered: false, snapshotPath: null };
    } catch (error) {
      discardNewDatabase(databasePath);
      throw new Error(`New database initialization failed and its partial database was removed: ${error.message}`);
    }
  }

  const existingRecoveryPaths = recoveryPaths(databasePath);
  const existingRecovery = readRecoveryManifest(existingRecoveryPaths, databasePath);
  if (existingRecovery?.status === 'recovery_failed') {
    throw new Error('A prior database recovery failed; its encrypted snapshot was preserved for manual recovery.');
  }
  if (existingRecovery?.status === 'prepared' || existingRecovery?.status === 'recovering') {
    try {
      const restored = restoreSnapshot(
        existingRecoveryPaths.snapshotPath,
        existingRecovery.snapshotSha256,
      );
      assertHealthyState(restored, 'Interrupted migration recovery');
      if (!sameRevisions(restored.currentRevisions, existingRecovery.fromRevisions)) {
        throw new Error('Interrupted migration recovery restored an unexpected database revision.');
      }
    } catch (recoveryError) {
      atomicWriteJson(existingRecoveryPaths.manifestPath, {
        ...existingRecovery,
        status: 'recovery_failed',
        recoveryFailedAt: now(),
        recoveryFailure: String(recoveryError?.message || recoveryError),
      });
      throw new Error('An interrupted database migration could not be recovered; its encrypted snapshot was preserved for manual recovery.');
    }
    atomicWriteJson(existingRecoveryPaths.manifestPath, {
      ...existingRecovery,
      status: 'recovered',
      failedAt: existingRecovery.failedAt || now(),
      recoveredAt: now(),
      failure: existingRecovery.failure || 'Database migration was interrupted before completion.',
    });
    log('interrupted database migration detected; encrypted pre-migration snapshot restored');
    throw new Error('An interrupted database migration was restored; restart BreakTwenty to retry the update.');
  }

  const before = inspect();
  assertHealthyState(before, 'Pre-migration database');
  if (!before.needsMigration) {
    log('database already at alembic head; migration snapshot skipped');
    return { migrated: false, recovered: false, snapshotPath: null };
  }

  const paths = recoveryPaths(databasePath);
  const snapshot = createSnapshot(paths.snapshotPath);
  assertSnapshotState(snapshot);
  const baseManifest = {
    schema: MIGRATION_RECOVERY_SCHEMA,
    version: MIGRATION_RECOVERY_VERSION,
    databaseFile: path.basename(databasePath),
    snapshotFile: path.basename(paths.snapshotPath),
    fromRevisions: before.currentRevisions,
    toRevisions: before.headRevisions,
    snapshotBytes: snapshot.bytes,
    snapshotSha256: snapshot.sha256,
    preparedAt: now(),
  };
  atomicWriteJson(paths.manifestPath, { ...baseManifest, status: 'prepared' });
  log(`encrypted pre-migration snapshot ready for ${before.currentRevisions.join(',') || 'unversioned'} -> ${before.headRevisions.join(',')}`);

  try {
    migrate();
    const after = inspect();
    assertHealthyState(after, 'Migrated database');
    if (after.needsMigration || !sameRevisions(after.currentRevisions, before.headRevisions)) {
      throw new Error('Database migration did not reach the expected schema head.');
    }
    atomicWriteJson(paths.manifestPath, {
      ...baseManifest,
      status: 'succeeded',
      completedAt: now(),
    });
    log('database migration verified; encrypted recovery snapshot retained');
    return { migrated: true, recovered: false, snapshotPath: paths.snapshotPath };
  } catch (migrationError) {
    atomicWriteJson(paths.manifestPath, {
      ...baseManifest,
      status: 'recovering',
      failedAt: now(),
      failure: String(migrationError?.message || migrationError),
    });
    try {
      const restored = restoreSnapshot(paths.snapshotPath, snapshot.sha256);
      assertHealthyState(restored, 'Restored database');
      if (!sameRevisions(restored.currentRevisions, before.currentRevisions)) {
        throw new Error('Restored database revision does not match the pre-migration revision.');
      }
    } catch (recoveryError) {
      atomicWriteJson(paths.manifestPath, {
        ...baseManifest,
        status: 'recovery_failed',
        failedAt: now(),
        recoveryFailedAt: now(),
        failure: String(migrationError?.message || migrationError),
        recoveryFailure: String(recoveryError?.message || recoveryError),
      });
      throw new Error('Database migration and encrypted snapshot recovery both failed; the encrypted snapshot was preserved for manual recovery.');
    }
    atomicWriteJson(paths.manifestPath, {
      ...baseManifest,
      status: 'recovered',
      failedAt: now(),
      recoveredAt: now(),
      failure: String(migrationError?.message || migrationError),
    });
    log('database migration failed; encrypted pre-migration snapshot restored');
    throw new Error('Database migration failed and the encrypted pre-migration database was restored.');
  }
}

module.exports = {
  MIGRATION_RECOVERY_SCHEMA,
  MIGRATION_RECOVERY_VERSION,
  removeNewDatabaseFiles,
  recoveryPaths,
  runMigrationsSafely,
};
