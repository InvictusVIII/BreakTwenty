const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  MIGRATION_RECOVERY_SCHEMA,
  recoveryPaths,
  runMigrationsSafely,
} = require('./migrationSafety');

function healthyState(currentRevisions, headRevisions) {
  return {
    currentRevisions,
    headRevisions,
    needsMigration: JSON.stringify(currentRevisions) !== JSON.stringify(headRevisions),
    integrity: 'ok',
    foreignKeyErrors: 0,
  };
}

const FIXTURE_SHA256 = 'f'.repeat(64);

test('installed v1 fixture upgrades to the next revision with an encrypted snapshot retained', (context) => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-v1-upgrade-'));
  context.after(() => fs.rmSync(dataDir, { recursive: true, force: true }));
  const databasePath = path.join(dataDir, 'breaktwenty.db');
  fs.writeFileSync(databasePath, 'installed-v1-data');
  let revision = '0001';
  let restoreCalls = 0;

  const result = runMigrationsSafely({
    databasePath,
    databaseExisted: true,
    inspect: () => healthyState([revision], ['0002']),
    createSnapshot: (snapshotPath) => {
      fs.mkdirSync(path.dirname(snapshotPath), { recursive: true });
      fs.copyFileSync(databasePath, snapshotPath);
      return {
        ...healthyState(['0001'], ['0002']),
        bytes: fs.statSync(snapshotPath).size,
        sha256: FIXTURE_SHA256,
      };
    },
    migrate: () => {
      fs.writeFileSync(databasePath, 'installed-v1-data+migrated-v2');
      revision = '0002';
    },
    restoreSnapshot: () => {
      restoreCalls += 1;
      return healthyState(['0001'], ['0002']);
    },
    now: () => '2026-08-03T00:00:00.000Z',
  });

  const paths = recoveryPaths(databasePath);
  const manifest = JSON.parse(fs.readFileSync(paths.manifestPath, 'utf8'));
  assert.deepEqual(result, {
    migrated: true,
    recovered: false,
    snapshotPath: paths.snapshotPath,
  });
  assert.equal(fs.readFileSync(paths.snapshotPath, 'utf8'), 'installed-v1-data');
  assert.equal(fs.readFileSync(databasePath, 'utf8'), 'installed-v1-data+migrated-v2');
  assert.equal(restoreCalls, 0);
  assert.equal(manifest.schema, MIGRATION_RECOVERY_SCHEMA);
  assert.equal(manifest.status, 'succeeded');
  assert.deepEqual(manifest.fromRevisions, ['0001']);
  assert.deepEqual(manifest.toRevisions, ['0002']);
});

test('failed migration restores the installed v1 fixture before startup fails', (context) => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-failed-upgrade-'));
  context.after(() => fs.rmSync(dataDir, { recursive: true, force: true }));
  const databasePath = path.join(dataDir, 'breaktwenty.db');
  fs.writeFileSync(databasePath, 'installed-v1-data');
  let revision = '0001';

  assert.throws(
    () => runMigrationsSafely({
      databasePath,
      databaseExisted: true,
      inspect: () => healthyState([revision], ['0002']),
      createSnapshot: (snapshotPath) => {
        fs.mkdirSync(path.dirname(snapshotPath), { recursive: true });
        fs.copyFileSync(databasePath, snapshotPath);
        return {
          ...healthyState(['0001'], ['0002']),
          bytes: fs.statSync(snapshotPath).size,
          sha256: FIXTURE_SHA256,
        };
      },
      migrate: () => {
        fs.writeFileSync(databasePath, 'partially-migrated-data');
        revision = 'partial-0002';
        throw new Error('forced migration failure');
      },
      restoreSnapshot: (snapshotPath) => {
        fs.copyFileSync(snapshotPath, databasePath);
        revision = '0001';
        return healthyState(['0001'], ['0002']);
      },
      now: () => '2026-08-03T00:00:00.000Z',
    }),
    /encrypted pre-migration database was restored/,
  );

  const manifest = JSON.parse(
    fs.readFileSync(recoveryPaths(databasePath).manifestPath, 'utf8'),
  );
  assert.equal(fs.readFileSync(databasePath, 'utf8'), 'installed-v1-data');
  assert.equal(manifest.status, 'recovered');
  assert.equal(manifest.failure, 'forced migration failure');
});

test('database already at head skips snapshot creation', () => {
  let snapshotCalls = 0;
  let migrationCalls = 0;
  const result = runMigrationsSafely({
    databasePath: '/tmp/breaktwenty.db',
    databaseExisted: true,
    inspect: () => healthyState(['0001'], ['0001']),
    createSnapshot: () => {
      snapshotCalls += 1;
    },
    migrate: () => {
      migrationCalls += 1;
    },
    restoreSnapshot: () => {},
  });

  assert.deepEqual(result, { migrated: false, recovered: false, snapshotPath: null });
  assert.equal(snapshotCalls, 0);
  assert.equal(migrationCalls, 0);
});

test('next launch restores a migration interrupted before completion', (context) => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-interrupted-upgrade-'));
  context.after(() => fs.rmSync(dataDir, { recursive: true, force: true }));
  const databasePath = path.join(dataDir, 'breaktwenty.db');
  const paths = recoveryPaths(databasePath);
  fs.mkdirSync(paths.directory, { recursive: true });
  fs.writeFileSync(databasePath, 'partially-migrated-data');
  fs.writeFileSync(paths.snapshotPath, 'installed-v1-data');
  fs.writeFileSync(paths.manifestPath, JSON.stringify({
    schema: MIGRATION_RECOVERY_SCHEMA,
    version: 1,
    status: 'prepared',
    databaseFile: 'breaktwenty.db',
    snapshotFile: 'pre-migration.db',
    fromRevisions: ['0001'],
    toRevisions: ['0002'],
    snapshotBytes: fs.statSync(paths.snapshotPath).size,
    snapshotSha256: FIXTURE_SHA256,
  }));
  let inspectCalls = 0;

  assert.throws(
    () => runMigrationsSafely({
      databasePath,
      databaseExisted: true,
      inspect: () => {
        inspectCalls += 1;
        return healthyState(['partial-0002'], ['0002']);
      },
      createSnapshot: () => assert.fail('must not overwrite the recovery snapshot'),
      migrate: () => assert.fail('must not retry before recovery is surfaced'),
      restoreSnapshot: (snapshotPath, expectedSha256) => {
        assert.equal(snapshotPath, paths.snapshotPath);
        assert.equal(expectedSha256, FIXTURE_SHA256);
        fs.copyFileSync(snapshotPath, databasePath);
        return healthyState(['0001'], ['0002']);
      },
      now: () => '2026-08-03T00:00:00.000Z',
    }),
    /interrupted database migration was restored/,
  );

  const manifest = JSON.parse(fs.readFileSync(paths.manifestPath, 'utf8'));
  assert.equal(inspectCalls, 0);
  assert.equal(fs.readFileSync(databasePath, 'utf8'), 'installed-v1-data');
  assert.equal(manifest.status, 'recovered');
});

test('failed first-install migration removes only the partial new database', (context) => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-new-database-'));
  context.after(() => fs.rmSync(dataDir, { recursive: true, force: true }));
  const databasePath = path.join(dataDir, 'breaktwenty.db');

  assert.throws(
    () => runMigrationsSafely({
      databasePath,
      databaseExisted: false,
      inspect: () => healthyState([], ['0001']),
      createSnapshot: () => {},
      migrate: () => {
        fs.writeFileSync(databasePath, 'partial-new-database');
        fs.writeFileSync(`${databasePath}-wal`, 'partial-wal');
        throw new Error('forced first-install failure');
      },
      restoreSnapshot: () => {},
    }),
    /partial database was removed/,
  );

  assert.equal(fs.existsSync(databasePath), false);
  assert.equal(fs.existsSync(`${databasePath}-wal`), false);
});

test('invalid snapshot checksum metadata fails closed before migration', (context) => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-invalid-snapshot-'));
  context.after(() => fs.rmSync(dataDir, { recursive: true, force: true }));
  const databasePath = path.join(dataDir, 'breaktwenty.db');
  fs.writeFileSync(databasePath, 'installed-v1-data');
  let migrationCalls = 0;

  assert.throws(
    () => runMigrationsSafely({
      databasePath,
      databaseExisted: true,
      inspect: () => healthyState(['0001'], ['0002']),
      createSnapshot: () => ({
        ...healthyState(['0001'], ['0002']),
        bytes: 17,
        sha256: 'not-a-checksum',
      }),
      migrate: () => {
        migrationCalls += 1;
      },
      restoreSnapshot: () => {},
    }),
    /invalid checksum metadata/,
  );

  assert.equal(migrationCalls, 0);
  assert.equal(fs.existsSync(recoveryPaths(databasePath).manifestPath), false);
});
