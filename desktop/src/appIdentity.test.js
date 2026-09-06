const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  APP_STORAGE_NAMESPACE,
  APP_TECHNICAL_ID,
  APP_UPDATE_PROVIDER,
  APP_UPDATE_REPOSITORY_URL,
  configureApplicationIdentity,
  resolvePackagedIdentity,
  resolveWindowsLocalAppRoot,
} = require('./appIdentity');

test('configures permanent desktop identity independently of the visible app name', (context) => {
  const appDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-identity-'));
  context.after(() => fs.rmSync(appDataDir, { recursive: true, force: true }));
  const configured = {};
  const app = {
    isPackaged: true,
    getName: () => 'A Future Display Name',
    getPath: (name) => {
      assert.equal(name, 'appData');
      return appDataDir;
    },
    setAppUserModelId: (value) => {
      configured.appUserModelId = value;
    },
    setPath: (name, value) => {
      configured[name] = value;
    },
  };

  const userDataDir = configureApplicationIdentity(app, {
    developmentUserDataDir: '/ignored/development/profile',
  });

  assert.equal(userDataDir, path.join(appDataDir, APP_STORAGE_NAMESPACE));
  assert.equal(configured.userData, userDataDir);
  assert.equal(configured.sessionData, userDataDir);
  assert.equal(configured.appUserModelId, APP_TECHNICAL_ID);
  assert.equal(fs.statSync(userDataDir).isDirectory(), true);
});

test('uses an alternate packaged namespace when release metadata requests it', (context) => {
  const appDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-identity-'));
  context.after(() => fs.rmSync(appDataDir, { recursive: true, force: true }));
  const configured = {};
  const app = {
    isPackaged: true,
    getPath: (name) => {
      assert.equal(name, 'appData');
      return appDataDir;
    },
    setAppUserModelId: (value) => {
      configured.appUserModelId = value;
    },
    setPath: (name, value) => {
      configured[name] = value;
    },
  };

  const metadata = {
    breaktwentyDesktopIdentity: {
      technicalId: 'app.breaktwenty.testbuild',
      storageNamespace: 'TestBuild',
      windowsLocalAppDataNamespace: 'TestBuild',
    },
  };
  const userDataDir = configureApplicationIdentity(app, { metadata });

  assert.equal(userDataDir, path.join(appDataDir, 'TestBuild'));
  assert.equal(configured.userData, userDataDir);
  assert.equal(configured.sessionData, userDataDir);
  assert.equal(configured.appUserModelId, 'app.breaktwenty.testbuild');
  assert.equal(
    resolveWindowsLocalAppRoot({
      fallbackDir: '/fallback',
      localAppData: 'C:\\Users\\Test\\AppData\\Local',
      platform: 'win32',
    }),
    path.join('C:\\Users\\Test\\AppData\\Local', 'TestBuild'),
  );
});

test('normalizes incomplete packaged identity metadata back to BreakTwenty defaults', () => {
  assert.deepEqual(resolvePackagedIdentity({}), {
    technicalId: APP_TECHNICAL_ID,
    storageNamespace: APP_STORAGE_NAMESPACE,
    windowsLocalAppDataNamespace: APP_STORAGE_NAMESPACE,
  });
  assert.deepEqual(resolvePackagedIdentity({ breaktwentyDesktopIdentity: { storageNamespace: '  TestBuild  ' } }), {
    technicalId: APP_TECHNICAL_ID,
    storageNamespace: 'TestBuild',
    windowsLocalAppDataNamespace: 'TestBuild',
  });
});

test('isolates development Electron state from the packaged profile', (context) => {
  const appDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-app-identity-'));
  const developmentUserDataDir = path.join(appDataDir, 'private-runtime', 'electron-profile');
  context.after(() => fs.rmSync(appDataDir, { recursive: true, force: true }));
  const configured = {};
  const app = {
    isPackaged: false,
    getPath: (name) => {
      assert.equal(name, 'appData');
      return appDataDir;
    },
    setAppUserModelId: () => {},
    setPath: (name, value) => {
      configured[name] = value;
    },
  };

  const userDataDir = configureApplicationIdentity(app, { developmentUserDataDir });

  assert.equal(userDataDir, developmentUserDataDir);
  assert.equal(configured.userData, developmentUserDataDir);
  assert.equal(configured.sessionData, developmentUserDataDir);
  assert.equal(fs.statSync(developmentUserDataDir).isDirectory(), true);
  assert.notEqual(userDataDir, path.join(appDataDir, APP_STORAGE_NAMESPACE));
});

test('keeps the Windows local runtime root on the permanent storage namespace', () => {
  assert.equal(
    resolveWindowsLocalAppRoot({
      fallbackDir: '/fallback',
      localAppData: 'C:\\Users\\Test\\AppData\\Local',
      platform: 'win32',
      storageNamespace: APP_STORAGE_NAMESPACE,
    }),
    path.join('C:\\Users\\Test\\AppData\\Local', APP_STORAGE_NAMESPACE),
  );
  assert.equal(
    resolveWindowsLocalAppRoot({
      fallbackDir: '/fallback',
      localAppData: '/ignored',
      platform: 'linux',
    }),
    '/fallback',
  );
});

test('release metadata matches the permanent desktop and declared update lanes', () => {
  const desktopRoot = path.resolve(__dirname, '..');
  const repositoryRoot = path.resolve(desktopRoot, '..');
  const packageMetadata = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'));
  const builderConfig = fs.readFileSync(path.join(desktopRoot, 'electron-builder.yml'), 'utf8');
  const releaseWorkflow = fs.readFileSync(
    path.join(repositoryRoot, '.github', 'workflows', 'desktop-release.yml'),
    'utf8',
  );
  const releaseAction = fs.readFileSync(
    path.join(repositoryRoot, '.github', 'actions', 'desktop-package', 'action.yml'),
    'utf8',
  );
  const releasePipeline = `${releaseWorkflow}\n${releaseAction}`;
  const builderOwner = builderConfig.match(/^  owner: (.+)$/m)?.[1];
  const builderRepo = builderConfig.match(/^  repo: (.+)$/m)?.[1];
  const builderPrivate = builderConfig.match(/^  private: (true|false)$/m)?.[1] === 'true';

  assert.equal(packageMetadata.desktopName, APP_TECHNICAL_ID);
  assert.equal(packageMetadata.productName, APP_STORAGE_NAMESPACE);
  assert.match(builderConfig, new RegExp(`^appId: ${APP_TECHNICAL_ID.replaceAll('.', '\\.')}$`, 'm'));
  assert.match(builderConfig, new RegExp(`^productName: ${APP_STORAGE_NAMESPACE}$`, 'm'));
  assert.match(builderConfig, new RegExp(`^executableName: ${APP_STORAGE_NAMESPACE}$`, 'm'));
  assert.ok(builderConfig.includes('artifactName: BreakTwenty-${version}-${arch}.${ext}'));
  assert.match(builderConfig, /^  provider: github$/m);
  assert.equal(packageMetadata.homepage, `https://github.com/${builderOwner}/${builderRepo}`);
  assert.equal(builderOwner, APP_UPDATE_PROVIDER.owner);
  assert.equal(builderRepo, APP_UPDATE_PROVIDER.repo);
  assert.equal(builderPrivate, false);
  assert.equal(APP_UPDATE_REPOSITORY_URL, 'https://github.com/InvictusVIII/BreakTwenty');
  assert.ok(releasePipeline.includes('--config.publish.owner=${BREAKTWENTY_UPDATER_OWNER}'));
  assert.ok(releasePipeline.includes('--config.publish.repo=${BREAKTWENTY_UPDATER_REPO}'));
  assert.ok(releasePipeline.includes('--config.publish.private=${BREAKTWENTY_UPDATER_PRIVATE}'));
  assert.ok(releasePipeline.includes('BREAKTWENTY_DESKTOP_IDENTITY_CONFIG'));
  assert.ok(releasePipeline.includes('artifact-basename'));
});
