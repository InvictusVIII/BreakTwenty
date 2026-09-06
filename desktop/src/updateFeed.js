const fs = require('node:fs');
const path = require('node:path');
const { load } = require('js-yaml');

const UPDATE_AUTH_MUTATION_BLOCKED_STATUSES = new Set([
  'checking',
  'downloading',
  'downloaded',
  'installing',
]);

function prereleaseUpdatesEnabled({
  appVersion,
  argv = process.argv,
  env = process.env,
  privateFeed = false,
}) {
  if (privateFeed) {
    return false;
  }
  return (
    env.BREAKTWENTY_ALLOW_PRERELEASE_UPDATES === '1'
    || argv.includes('--allow-prerelease-updates')
    || String(appVersion || '').includes('-')
  );
}

async function readStampedUpdateConfiguration(resourcesPath = process.resourcesPath) {
  const configurationPath = path.join(resourcesPath, 'app-update.yml');
  return load(await fs.promises.readFile(configurationPath, 'utf8'));
}

function buildStampedGithubFeed(stampedConfiguration, token, { forcePrivate = false } = {}) {
  if (
    stampedConfiguration?.provider !== 'github'
    || typeof stampedConfiguration?.owner !== 'string'
    || !stampedConfiguration.owner.trim()
    || typeof stampedConfiguration?.repo !== 'string'
    || !stampedConfiguration.repo.trim()
  ) {
    throw new Error('Packaged update metadata does not contain a valid GitHub repository.');
  }

  const normalizedToken = String(token || '').trim();
  const privateFeed = stampedConfiguration.private === true || forcePrivate;
  const sanitizedConfiguration = { ...stampedConfiguration };
  delete sanitizedConfiguration.token;
  delete sanitizedConfiguration.requestHeaders;
  if (normalizedToken && privateFeed) {
    return {
      ...sanitizedConfiguration,
      private: true,
      token: normalizedToken,
    };
  }
  if (privateFeed) {
    return {
      ...sanitizedConfiguration,
      private: true,
    };
  }
  return sanitizedConfiguration;
}

function updateAuthMutationBlocked(status) {
  return UPDATE_AUTH_MUTATION_BLOCKED_STATUSES.has(status);
}

function stateAfterUpdateFeedChange({ enabled, disabledMessage, successMessage }) {
  return {
    status: enabled ? 'idle' : 'unavailable',
    message: enabled ? successMessage : disabledMessage,
    checkedAt: null,
    downloadedAt: null,
    updateInfo: null,
    progress: null,
    error: null,
    canCheck: enabled,
    canDownload: false,
    canInstall: false,
  };
}

module.exports = {
  buildStampedGithubFeed,
  prereleaseUpdatesEnabled,
  readStampedUpdateConfiguration,
  stateAfterUpdateFeedChange,
  updateAuthMutationBlocked,
};
