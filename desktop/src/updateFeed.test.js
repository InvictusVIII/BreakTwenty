const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const {
  buildStampedGithubFeed,
  displayedAppVersion,
  prereleaseToStableFallbackAllowed,
  prereleaseUpdatesEnabled,
  readStampedUpdateConfiguration,
  stateAfterUpdateFeedChange,
  updateAuthMutationBlocked,
} = require('./updateFeed');

test('development builds do not present a release-lane version', () => {
  assert.equal(displayedAppVersion({
    appVersion: '1.0.1-rc.2',
    isPackaged: false,
    sourceCommit: '23c30e61aabbccdd',
  }), 'Development build (source 23c30e61aabb)');
  assert.equal(displayedAppVersion({
    appVersion: '1.0.1-rc.2',
    isPackaged: false,
    sourceCommit: '',
  }), 'Development build');
  assert.equal(displayedAppVersion({
    appVersion: '1.0.2-rc.1',
    isPackaged: true,
    sourceCommit: '23c30e61aabbccdd',
  }), '1.0.2-rc.1');
});

test('prerelease updates require an explicit test launch for stable builds', () => {
  assert.equal(prereleaseUpdatesEnabled({ appVersion: '1.0.0', argv: [], env: {} }), false);
  assert.equal(prereleaseUpdatesEnabled({
    appVersion: '1.0.0',
    argv: ['BreakTwenty', '--allow-prerelease-updates'],
    env: {},
  }), true);
  assert.equal(prereleaseUpdatesEnabled({
    appVersion: '1.0.0',
    argv: [],
    env: { BREAKTWENTY_ALLOW_PRERELEASE_UPDATES: '1' },
  }), true);
  assert.equal(prereleaseUpdatesEnabled({ appVersion: '1.0.1-beta.1', argv: [], env: {} }), true);
});

test('private release feeds use GitHub latest instead of release-list prerelease selection', () => {
  assert.equal(prereleaseUpdatesEnabled({
    appVersion: '1.0.0-rc.11',
    argv: ['PrivateBuild', '--allow-prerelease-updates'],
    env: { BREAKTWENTY_ALLOW_PRERELEASE_UPDATES: '1' },
    privateFeed: true,
  }), false);
});

test('public prerelease clients may retry stable after their RC channel is removed', () => {
  const noPublishedVersions = { code: 'ERR_UPDATER_NO_PUBLISHED_VERSIONS' };
  assert.equal(prereleaseToStableFallbackAllowed({
    error: noPublishedVersions,
    appVersion: '1.0.1-rc.2',
    allowPrerelease: true,
  }), true);
  for (const override of [
    { appVersion: '1.0.1' },
    { allowPrerelease: false },
    { privateFeed: true },
    { explicitChannel: true },
    { error: { code: 'ERR_UPDATER_CHANNEL_FILE_NOT_FOUND' } },
  ]) {
    assert.equal(prereleaseToStableFallbackAllowed({
      error: noPublishedVersions,
      appVersion: '1.0.1-rc.2',
      allowPrerelease: true,
      ...override,
    }), false);
  }
});

test('keeps an arbitrary private repository stamped into a test package', () => {
  assert.deepEqual(
    buildStampedGithubFeed({
      provider: 'github',
      owner: 'InvictusVIII',
      repo: 'PrivateFeed',
      private: true,
    }, 'private-token'),
    {
      provider: 'github',
      owner: 'InvictusVIII',
      repo: 'PrivateFeed',
      private: true,
      token: 'private-token',
    },
  );
});

test('keeps the public BreakTwenty repository stamped into a public package', () => {
  assert.deepEqual(
    buildStampedGithubFeed(
      {
        provider: 'github',
        owner: 'InvictusVIII',
        repo: 'BreakTwenty',
        private: false,
      },
      'leftover-private-test-token',
    ),
    {
      provider: 'github',
      owner: 'InvictusVIII',
      repo: 'BreakTwenty',
      private: false,
    },
  );
});

test('rejects missing or non-GitHub packaged update metadata', () => {
  assert.throws(
    () => buildStampedGithubFeed({ provider: 'generic', url: 'https://example.invalid' }, null),
    /valid GitHub repository/,
  );
  assert.throws(
    () => buildStampedGithubFeed({ provider: 'github', owner: '', repo: 'BreakTwenty' }, null),
    /valid GitHub repository/,
  );
});

test('never trusts embedded updater credentials from packaged metadata', () => {
  assert.deepEqual(
    buildStampedGithubFeed({
      provider: 'github',
      owner: 'InvictusVIII',
      repo: 'BreakTwenty',
      private: false,
      token: 'must-not-ship',
      requestHeaders: { authorization: 'must-not-ship' },
    }, null),
    {
      provider: 'github',
      owner: 'InvictusVIII',
      repo: 'BreakTwenty',
      private: false,
    },
  );
});

test('reads the exact updater metadata generated into packaged resources', async (context) => {
  const resourcesPath = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'breaktwenty-feed-'));
  context.after(() => fs.rmSync(resourcesPath, { recursive: true, force: true }));
  fs.writeFileSync(path.join(resourcesPath, 'app-update.yml'), [
    'provider: github',
    'owner: InvictusVIII',
    'repo: PrivateFeed',
    'private: true',
    '',
  ].join('\n'));

  assert.deepEqual(await readStampedUpdateConfiguration(resourcesPath), {
    provider: 'github',
    owner: 'InvictusVIII',
    repo: 'PrivateFeed',
    private: true,
  });
});

test('changing update authentication invalidates stale discovered updates', () => {
  assert.equal(updateAuthMutationBlocked('available'), false);
  assert.equal(updateAuthMutationBlocked('checking'), true);
  assert.equal(updateAuthMutationBlocked('downloading'), true);
  assert.equal(updateAuthMutationBlocked('downloaded'), true);
  assert.equal(updateAuthMutationBlocked('installing'), true);
  assert.deepEqual(
    stateAfterUpdateFeedChange({
      enabled: true,
      disabledMessage: 'disabled',
      successMessage: 'Token saved. Ready.',
    }),
    {
      status: 'idle',
      message: 'Token saved. Ready.',
      checkedAt: null,
      downloadedAt: null,
      updateInfo: null,
      progress: null,
      error: null,
      canCheck: true,
      canDownload: false,
      canInstall: false,
    },
  );
});
