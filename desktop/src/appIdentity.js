const fs = require('node:fs');
const path = require('node:path');
const packageMetadata = require('../package.json');

const APP_TECHNICAL_ID = 'app.breaktwenty.desktop';
const APP_STORAGE_NAMESPACE = 'BreakTwenty';
const APP_DEVELOPMENT_STORAGE_NAMESPACE = 'BreakTwenty Development';
const APP_DATABASE_FILE_NAME = 'breaktwenty.db';
const APP_UPDATE_PROVIDER = Object.freeze({
  provider: 'github',
  owner: 'InvictusVIII',
  repo: 'BreakTwenty',
});
const APP_UPDATE_REPOSITORY_URL = `https://github.com/${APP_UPDATE_PROVIDER.owner}/${APP_UPDATE_PROVIDER.repo}`;
const DEFAULT_PACKAGED_IDENTITY = Object.freeze({
  technicalId: APP_TECHNICAL_ID,
  storageNamespace: APP_STORAGE_NAMESPACE,
  windowsLocalAppDataNamespace: APP_STORAGE_NAMESPACE,
});
let activePackagedIdentity = DEFAULT_PACKAGED_IDENTITY;

function cleanIdentityValue(value) {
  const normalized = String(value || '').trim();
  return normalized || null;
}

function resolvePackagedIdentity(metadata = packageMetadata) {
  const identity = metadata?.breaktwentyDesktopIdentity;
  if (!identity || typeof identity !== 'object') {
    return DEFAULT_PACKAGED_IDENTITY;
  }

  const technicalId = cleanIdentityValue(identity.technicalId) || APP_TECHNICAL_ID;
  const storageNamespace = cleanIdentityValue(identity.storageNamespace) || APP_STORAGE_NAMESPACE;
  const windowsLocalAppDataNamespace = (
    cleanIdentityValue(identity.windowsLocalAppDataNamespace)
    || storageNamespace
  );
  return {
    technicalId,
    storageNamespace,
    windowsLocalAppDataNamespace,
  };
}

function getApplicationIdentity() {
  return activePackagedIdentity;
}

function resolveUserDataPath(
  appDataDir,
  {
    isPackaged = true,
    developmentUserDataDir = '',
    storageNamespace = activePackagedIdentity.storageNamespace,
  } = {},
) {
  if (!isPackaged) {
    const configuredDevelopmentDir = String(developmentUserDataDir || '').trim();
    if (configuredDevelopmentDir) {
      return path.resolve(configuredDevelopmentDir);
    }
    return path.join(appDataDir, APP_DEVELOPMENT_STORAGE_NAMESPACE);
  }
  return path.join(appDataDir, storageNamespace);
}

function resolveWindowsLocalAppRoot({
  fallbackDir,
  localAppData = process.env.LOCALAPPDATA,
  platform = process.platform,
  storageNamespace = activePackagedIdentity.windowsLocalAppDataNamespace,
}) {
  const normalizedLocalAppData = String(localAppData || '').trim();
  if (platform === 'win32' && normalizedLocalAppData) {
    return path.join(normalizedLocalAppData, storageNamespace);
  }
  return fallbackDir;
}

function configureApplicationIdentity(
  app,
  { developmentUserDataDir = '', metadata = packageMetadata } = {},
) {
  activePackagedIdentity = resolvePackagedIdentity(metadata);
  const userDataDir = resolveUserDataPath(app.getPath('appData'), {
    isPackaged: app.isPackaged !== false,
    developmentUserDataDir,
    storageNamespace: activePackagedIdentity.storageNamespace,
  });
  fs.mkdirSync(userDataDir, { recursive: true });
  app.setPath('userData', userDataDir);
  app.setPath('sessionData', userDataDir);
  app.setAppUserModelId(activePackagedIdentity.technicalId);
  return userDataDir;
}

module.exports = {
  APP_DATABASE_FILE_NAME,
  APP_DEVELOPMENT_STORAGE_NAMESPACE,
  APP_STORAGE_NAMESPACE,
  APP_TECHNICAL_ID,
  APP_UPDATE_PROVIDER,
  APP_UPDATE_REPOSITORY_URL,
  configureApplicationIdentity,
  getApplicationIdentity,
  resolvePackagedIdentity,
  resolveUserDataPath,
  resolveWindowsLocalAppRoot,
};
