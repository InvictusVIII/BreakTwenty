const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  SUPPORT_ARCHIVE_METADATA_MAX_BYTES,
  materializeOwnedSupportArchive,
  resolveOwnedSupportArchivePath,
} = require('./supportArchiveReveal');

const ARCHIVE_ID = '11111111-1111-4111-8111-111111111111';

function fixture(metadata = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-support-archive-'));
  const archivesDir = path.join(root, 'exports', 'archives');
  fs.mkdirSync(archivesDir, { recursive: true });
  const targetPath = path.join(archivesDir, `${ARCHIVE_ID}.zip`);
  const metadataPath = path.join(archivesDir, `${ARCHIVE_ID}.json`);
  fs.writeFileSync(targetPath, 'zip');
  fs.writeFileSync(metadataPath, JSON.stringify({
    archive_id: ARCHIVE_ID,
    user_id: 1,
    filename: 'BreakTwenty_all_providers_recent_diagnostics_2026-08-20_08-25-25_EDT.zip',
    kind: 'all_recent',
    provider: null,
    ...metadata,
  }));
  return { root, targetPath, metadataPath };
}

test('resolves only a UUID archive with a matching bounded owner sidecar', (context) => {
  const current = fixture();
  context.after(() => fs.rmSync(current.root, { recursive: true, force: true }));

  assert.equal(
    resolveOwnedSupportArchivePath({ logDir: current.root, archiveId: ARCHIVE_ID, userId: 1 }),
    current.targetPath,
  );
  assert.equal(
    resolveOwnedSupportArchivePath({ logDir: current.root, archiveId: ARCHIVE_ID, userId: 2 }),
    '',
  );
  assert.equal(
    resolveOwnedSupportArchivePath({ logDir: current.root, archiveId: '../escape', userId: 1 }),
    '',
  );

  fs.writeFileSync(current.metadataPath, JSON.stringify({
    archive_id: '22222222-2222-4222-8222-222222222222',
    user_id: 1,
  }));
  assert.equal(
    resolveOwnedSupportArchivePath({ logDir: current.root, archiveId: ARCHIVE_ID, userId: 1 }),
    '',
  );
});

test('materializes one user-facing named zip without exposing the registry sidecar', (context) => {
  const current = fixture();
  context.after(() => fs.rmSync(current.root, { recursive: true, force: true }));

  const materializedPath = materializeOwnedSupportArchive({
    logDir: current.root,
    archiveId: ARCHIVE_ID,
    userId: 1,
  });

  assert.equal(
    materializedPath,
    path.join(
      current.root,
      'exports',
      'BreakTwenty_all_providers_recent_diagnostics_2026-08-20_08-25-25_EDT.zip',
    ),
  );
  assert.equal(fs.readFileSync(materializedPath, 'utf8'), 'zip');
  assert.deepEqual(
    fs.readdirSync(path.join(current.root, 'exports')).sort(),
    ['BreakTwenty_all_providers_recent_diagnostics_2026-08-20_08-25-25_EDT.zip', 'archives'],
  );
});

test('materializes a provider export inside that provider folder', (context) => {
  const current = fixture({
    filename: 'BreakTwenty_ibkr_flex_attempt_diagnostics_2026-08-21_16-47-35_EDT.zip',
    kind: 'provider_run',
    provider: 'ibkr_flex',
  });
  context.after(() => fs.rmSync(current.root, { recursive: true, force: true }));

  const materializedPath = materializeOwnedSupportArchive({
    logDir: current.root,
    archiveId: ARCHIVE_ID,
    userId: 1,
  });

  assert.equal(
    materializedPath,
    path.join(
      current.root,
      'exports',
      'ibkr_flex',
      'BreakTwenty_ibkr_flex_attempt_diagnostics_2026-08-21_16-47-35_EDT.zip',
    ),
  );
  assert.equal(fs.readFileSync(materializedPath, 'utf8'), 'zip');
  assert.deepEqual(fs.readdirSync(path.join(current.root, 'exports')).sort(), ['archives', 'ibkr_flex']);
  assert.equal(
    JSON.parse(fs.readFileSync(current.metadataPath, 'utf8')).materialized_relative_path,
    'ibkr_flex/BreakTwenty_ibkr_flex_attempt_diagnostics_2026-08-21_16-47-35_EDT.zip',
  );
});

test('rejects a provider archive with an unsafe provider folder', (context) => {
  const current = fixture({ kind: 'provider_run', provider: '../outside' });
  context.after(() => fs.rmSync(current.root, { recursive: true, force: true }));

  assert.equal(materializeOwnedSupportArchive({
    logDir: current.root,
    archiveId: ARCHIVE_ID,
    userId: 1,
  }), '');
});

test('rejects symlinked and oversized archive sidecars', (context) => {
  const current = fixture();
  context.after(() => fs.rmSync(current.root, { recursive: true, force: true }));
  const outside = path.join(current.root, 'outside.json');
  fs.writeFileSync(outside, JSON.stringify({ archive_id: ARCHIVE_ID, user_id: 1 }));
  fs.unlinkSync(current.metadataPath);
  fs.symlinkSync(outside, current.metadataPath);
  assert.equal(
    resolveOwnedSupportArchivePath({ logDir: current.root, archiveId: ARCHIVE_ID, userId: 1 }),
    '',
  );

  fs.unlinkSync(current.metadataPath);
  fs.writeFileSync(current.metadataPath, 'x'.repeat(SUPPORT_ARCHIVE_METADATA_MAX_BYTES + 1));
  assert.equal(
    resolveOwnedSupportArchivePath({ logDir: current.root, archiveId: ARCHIVE_ID, userId: 1 }),
    '',
  );
});
