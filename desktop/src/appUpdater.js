const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const log = require('electron-log');
const electronUpdater = require('electron-updater');
const { DOWNLOAD_PROGRESS } = require('electron-updater/out/types');
const {
  atomicWriteFile,
  decryptProtectedString,
  encryptProtectedString,
  secureStorageStatus,
} = require('./secureStorage');
const { tryStartPersistentInstallHelper } = require('./updateInstallHelper');
const { APP_BRAND_NAME } = require('./brand');
const { createSanitizingLogger, sanitizeLogText } = require('./appDiagnostics');
const { registerPrivilegedIpcHandler } = require('./ipcAuthorization');
const {
  buildStampedGithubFeed,
  displayedAppVersion,
  prereleaseToStableFallbackAllowed,
  prereleaseUpdatesEnabled,
  readStampedUpdateConfiguration,
  stateAfterUpdateFeedChange,
  updateAuthMutationBlocked,
} = require('./updateFeed');

const UPDATE_EVENTS_CHANNEL = 'breaktwenty:updates-changed';
const DISABLED_MESSAGE = `Desktop updates are available only in a packaged ${APP_BRAND_NAME} build.`;
const TOKEN_STORE_FILE = 'private-update-token.json';
function isoNow() {
  return new Date().toISOString();
}

function cleanError(error, exactSecrets = []) {
  if (!error) return null;
  return {
    name: error.name || 'Error',
    message: formatUpdateErrorMessage(error, exactSecrets),
  };
}

function formatUpdateErrorMessage(error, exactSecrets = []) {
  const message = String(error?.message || error || 'Could not check for updates.');
  const statusCode = Number(error?.statusCode || error?.status || error?.response?.statusCode || message.match(/\b(401|403|404)\b/)?.[1] || 0);
  const lowerMessage = message.toLowerCase();
  const isGithubFeed = lowerMessage.includes('github.com') || lowerMessage.includes('releases.atom');
  if (isGithubFeed && statusCode === 404) {
    return 'The public GitHub release feed is not available yet. Private release testing needs a fine-grained GitHub token; public releases will check automatically once the public repository has releases.';
  }
  if (isGithubFeed && (statusCode === 401 || statusCode === 403)) {
    return 'GitHub did not allow this private release check. Save a fine-grained GitHub token for private testing; public releases will not need a token.';
  }
  if (lowerMessage.includes('method: get') && lowerMessage.includes('headers:')) {
    return 'Could not reach the GitHub release feed. Save a fine-grained GitHub token for private release testing, or try again after public releases are available.';
  }
  return sanitizeLogText(message, [], exactSecrets);
}

function cleanUpdateInfo(info) {
  if (!info) return null;
  return {
    version: info.version || null,
    releaseName: info.releaseName || null,
    releaseDate: info.releaseDate || null,
    releaseNotes: typeof info.releaseNotes === 'string' ? info.releaseNotes : null,
  };
}

function cleanProgress(progress) {
  if (!progress) return null;
  return {
    percent: Number.isFinite(progress.percent) ? progress.percent : 0,
    bytesPerSecond: Number.isFinite(progress.bytesPerSecond) ? progress.bytesPerSecond : 0,
    transferred: Number.isFinite(progress.transferred) ? progress.transferred : 0,
    total: Number.isFinite(progress.total) ? progress.total : 0,
  };
}

function normalizeToken(value) {
  const raw = String(value || '').trim();
  if (raw.toLowerCase().startsWith('bearer ')) {
    return raw.slice(7).trim();
  }
  if (raw.toLowerCase().startsWith('token ')) {
    return raw.slice(6).trim();
  }
  return raw;
}

function normalizeEscapedShellValue(value) {
  return String(value).replace(/\\ /g, ' ');
}

function quoteBashArg(value) {
  return `'${normalizeEscapedShellValue(value).replace(/'/g, "'\\''")}'`;
}

function buildBashCommand(commandWithArgs) {
  return commandWithArgs.map(quoteBashArg).join(' ');
}

function filePathName(fileInfo) {
  try {
    return decodeURIComponent(fileInfo?.url?.pathname || '');
  } catch (_error) {
    return fileInfo?.url?.pathname || '';
  }
}

function fileCandidateNames(fileInfo) {
  return [
    filePathName(fileInfo),
    String(fileInfo?.info?.url || ''),
  ].filter(Boolean);
}

function fileHasExtension(fileInfo, extension) {
  const suffix = `.${extension.toLowerCase()}`;
  return fileCandidateNames(fileInfo).some((name) => name.toLowerCase().endsWith(suffix));
}

function currentDebArchNames() {
  if (process.arch === 'x64') return ['x64', 'amd64'];
  if (process.arch === 'arm64') return ['arm64', 'aarch64'];
  if (process.arch === 'ia32') return ['ia32', 'i386'];
  return [process.arch];
}

function selectDebFileInfo(provider, updateInfo) {
  const files = provider.resolveFiles(updateInfo);
  const debFiles = files.filter((fileInfo) => fileHasExtension(fileInfo, 'deb'));
  if (debFiles.length === 0) {
    throw new Error('No .deb update artifact was listed in latest-linux.yml.');
  }

  const archNames = currentDebArchNames();
  return debFiles.find((fileInfo) => (
    fileCandidateNames(fileInfo).some((name) => archNames.some((arch) => name.includes(arch)))
  )) || debFiles[0];
}

function spawnSyncChecked(logger, cmd, args = [], env = {}) {
  logger.info(`Executing: ${cmd} with args: ${args}`);
  const response = spawnSync(cmd, args, {
    env: { ...process.env, ...env },
    encoding: 'utf-8',
    shell: false,
  });
  const { error, status, stdout, stderr } = response;
  if (error != null) {
    logger.error(stderr);
    throw error;
  }
  if (status != null && status !== 0) {
    logger.error(stderr);
    throw new Error(`Command ${cmd} exited with code ${status}`);
  }
  return String(stdout || '').trim();
}

function patchDebUpdater() {
  if (process.platform !== 'linux') {
    return;
  }

  const { DebUpdater } = electronUpdater;
  if (!DebUpdater?.prototype || DebUpdater.prototype.__breaktwentyDebSudoPatch) {
    return;
  }

  const originalRunCommandWithSudoIfNeeded = DebUpdater.prototype.runCommandWithSudoIfNeeded;
  DebUpdater.prototype.doDownloadUpdate = function doDownloadDebUpdate(downloadUpdateOptions) {
    const provider = downloadUpdateOptions.updateInfoAndProvider.provider;
    const fileInfo = selectDebFileInfo(provider, downloadUpdateOptions.updateInfoAndProvider.info);
    this._logger.info(`Selected Debian update artifact: ${fileInfo.info.url}`);
    return this.executeDownload({
      fileExtension: 'deb',
      fileInfo,
      downloadUpdateOptions,
      task: async (updateFile, downloadOptions) => {
        if (this.listenerCount(DOWNLOAD_PROGRESS) > 0) {
          downloadOptions.onProgress = (progress) => this.emit(DOWNLOAD_PROGRESS, progress);
        }
        await this.httpExecutor.download(fileInfo.url, updateFile, downloadOptions);
      },
    });
  };
  DebUpdater.prototype.runCommandWithSudoIfNeeded = function runCommandWithBreakTwentySudoPatch(commandWithArgs) {
    if (!Array.isArray(commandWithArgs) || commandWithArgs.length === 0) {
      return originalRunCommandWithSudoIfNeeded.call(this, commandWithArgs);
    }
    const normalizedCommandWithArgs = commandWithArgs.map(normalizeEscapedShellValue);
    if (this.isRunningAsRoot()) {
      this._logger.info('Running as root, no need to use sudo');
      return spawnSyncChecked(this._logger, normalizedCommandWithArgs[0], normalizedCommandWithArgs.slice(1));
    }

    const { name } = this.app;
    const installComment = `"${name} would like to update"`;
    const sudo = this.sudoWithArgs(installComment);
    this._logger.info(`Running as non-root user, using sudo to install: ${sudo}`);
    return spawnSyncChecked(this._logger, sudo[0], [
      ...(sudo.length > 1 ? sudo.slice(1) : []),
      '/bin/bash',
      '-c',
      buildBashCommand(normalizedCommandWithArgs),
    ]);
  };
  Object.defineProperty(DebUpdater.prototype, '__breaktwentyDebSudoPatch', {
    value: true,
    configurable: false,
  });
}

function getEnvUpdateToken() {
  return normalizeToken(process.env.GH_TOKEN || process.env.GITHUB_TOKEN);
}

patchDebUpdater();
const { autoUpdater } = electronUpdater;

class BreakTwentyAppUpdater {
  constructor({
    app,
    ipcMain,
    getMainWindow,
    authorizeIpcEvent,
    prepareForInstall,
    onStatusChange,
  }) {
    this.app = app;
    this.ipcMain = ipcMain;
    this.getMainWindow = getMainWindow;
    if (typeof authorizeIpcEvent !== 'function') {
      throw new Error('BreakTwenty update IPC authorization is unavailable.');
    }
    this.authorizeIpcEvent = authorizeIpcEvent;
    this.prepareForInstall = typeof prepareForInstall === 'function'
      ? prepareForInstall
      : async () => {};
    this.onStatusChange = typeof onStatusChange === 'function' ? onStatusChange : null;
    this.disabledMessage = DISABLED_MESSAGE;
    this.enabled = (
      app.isPackaged &&
      process.env.BREAKTWENTY_DISABLE_AUTO_UPDATE !== '1'
    );
    this.autoCheckTimer = null;
    this.shuttingDown = false;
    this.tokenStorePath = path.join(app.getPath('userData'), TOKEN_STORE_FILE);
    this.authToken = null;
    this.authSource = 'none';
    this.authError = null;
    this.hasStoredToken = false;
    this.updateFeed = {
      provider: null,
      owner: null,
      repo: null,
      private: null,
    };
    this.storageStatus = {
      secure: false,
      encryptionAvailable: false,
      backend: 'unknown',
    };
    this.state = {
      enabled: this.enabled,
      isPackaged: app.isPackaged,
      platform: process.platform,
      currentVersion: displayedAppVersion({
        appVersion: app.getVersion(),
        isPackaged: app.isPackaged,
      }),
      status: this.enabled ? 'idle' : 'unavailable',
      message: this.enabled ? 'Ready to check for updates.' : this.disabledMessage,
      checkedAt: null,
      downloadedAt: null,
      updateInfo: null,
      progress: null,
      error: null,
      canCheck: this.enabled,
      canDownload: false,
      canInstall: false,
      auth: this.getAuthStatus(),
      updatedAt: isoNow(),
    };

    this.configureUpdater();
    this.registerUpdaterEvents();
    this.registerIpcHandlers();
    this.tokenInitializationPromise = this.initializeUpdateToken();
    this.feedConfigurationPromise = this.tokenInitializationPromise.then(
      () => this.applyAuthHeader(),
    );
  }

  configureUpdater() {
    log.transports.file.level = process.env.BREAKTWENTY_UPDATE_LOG_LEVEL || 'info';
    autoUpdater.logger = createSanitizingLogger(log, {
      exactSecrets: () => [this.authToken],
    });
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = false;
    autoUpdater.allowPrerelease = prereleaseUpdatesEnabled({
      appVersion: this.app.getVersion(),
    });
    if (process.env.BREAKTWENTY_UPDATE_CHANNEL) {
      autoUpdater.channel = process.env.BREAKTWENTY_UPDATE_CHANNEL;
    }
  }

  registerUpdaterEvents() {
    autoUpdater.on('checking-for-update', () => {
      this.updateState({
        status: 'checking',
        message: 'Checking for updates.',
        progress: null,
        error: null,
        canDownload: false,
        canInstall: false,
      });
    });

    autoUpdater.on('update-available', (info) => {
      this.updateState({
        status: 'available',
        message: `${APP_BRAND_NAME} ${info.version} is available.`,
        checkedAt: isoNow(),
        updateInfo: cleanUpdateInfo(info),
        progress: null,
        error: null,
        canDownload: true,
        canInstall: false,
      });
    });

    autoUpdater.on('update-not-available', () => {
      this.updateState({
        status: 'not-available',
        message: `${APP_BRAND_NAME} is up to date.`,
        checkedAt: isoNow(),
        progress: null,
        error: null,
        canDownload: false,
        canInstall: false,
      });
    });

    autoUpdater.on('download-progress', (progress) => {
      const clean = cleanProgress(progress) || {
        percent: 0,
        bytesPerSecond: 0,
        transferred: 0,
        total: 0,
      };
      const percent = Math.max(0, Math.min(100, clean.percent || 0));
      this.updateState({
        status: 'downloading',
        message: `Downloading update (${Math.round(percent)}%).`,
        progress: clean,
        error: null,
        canDownload: false,
        canInstall: false,
      });
    });

    autoUpdater.on('update-downloaded', (info) => {
      this.updateState({
        status: 'downloaded',
        message: `${APP_BRAND_NAME} ${info.version} is ready to install.`,
        downloadedAt: isoNow(),
        updateInfo: cleanUpdateInfo(info),
        progress: null,
        error: null,
        canDownload: false,
        canInstall: true,
      });
    });

    autoUpdater.on('error', (error) => {
      const exactSecrets = [this.authToken];
      const message = formatUpdateErrorMessage(error, exactSecrets);
      this.updateState({
        status: 'error',
        message,
        progress: null,
        error: cleanError(error, exactSecrets),
        canDownload: Boolean(this.state.updateInfo),
        canInstall: false,
      });
    });
  }

  registerIpcHandlers() {
    const handle = (channel, handler) => registerPrivilegedIpcHandler(
      this.ipcMain,
      this.authorizeIpcEvent,
      channel,
      handler,
    );
    handle('breaktwenty:updates-status', () => this.getStatus());
    handle('breaktwenty:updates-check', () => this.checkForUpdates());
    handle('breaktwenty:updates-download', () => this.downloadUpdate());
    handle('breaktwenty:updates-install', () => this.installUpdate());
    handle('breaktwenty:updates-save-token', (request) => this.savePrivateUpdateToken(request || {}));
    handle('breaktwenty:updates-clear-token', () => this.clearPrivateUpdateToken());
  }

  getStatus() {
    return {
      ...this.state,
      currentVersion: displayedAppVersion({
        appVersion: this.app.getVersion(),
        isPackaged: this.app.isPackaged,
      }),
      auth: this.getAuthStatus(),
    };
  }

  updateState(patch) {
    this.state = {
      ...this.state,
      ...patch,
      enabled: this.enabled,
      isPackaged: this.app.isPackaged,
      currentVersion: displayedAppVersion({
        appVersion: this.app.getVersion(),
        isPackaged: this.app.isPackaged,
      }),
      auth: this.getAuthStatus(),
      updatedAt: isoNow(),
    };
    this.broadcast();
    return this.getStatus();
  }

  broadcast() {
    const status = this.getStatus();
    const window = this.getMainWindow();
    if (window && !window.isDestroyed()) {
      window.webContents.send(UPDATE_EVENTS_CHANNEL, status);
    }
    if (this.onStatusChange) {
      try {
        this.onStatusChange(status);
      } catch (error) {
        log.warn(`Desktop update status callback failed: ${error.message}`);
      }
    }
  }

  getAuthStatus() {
    return {
      hasToken: Boolean(this.authToken),
      hasStoredToken: this.hasStoredToken,
      source: this.authSource,
      storageAvailable: this.storageStatus.secure,
      storageBackend: this.storageStatus.backend,
      feed: this.updateFeed,
      error: this.authError,
    };
  }

  async initializeUpdateToken() {
    try {
      this.storageStatus = await secureStorageStatus();
    } catch (error) {
      this.storageStatus = {
        secure: false,
        encryptionAvailable: false,
        backend: 'unknown',
      };
      this.authError = `Could not inspect encrypted token storage: ${error.message}`;
    }
    await this.loadUpdateToken();
    this.updateState({});
  }

  async loadUpdateToken() {
    const envToken = getEnvUpdateToken();
    if (envToken) {
      this.authToken = envToken;
      this.authSource = 'environment';
      this.authError = null;
      this.hasStoredToken = fs.existsSync(this.tokenStorePath);
      return;
    }

    this.hasStoredToken = fs.existsSync(this.tokenStorePath);
    if (!this.hasStoredToken) {
      this.authToken = null;
      this.authSource = 'none';
      this.authError = null;
      return;
    }
    if (!this.storageStatus.secure) {
      this.authToken = null;
      this.authSource = 'none';
      this.authError = this.authError || 'Encrypted token storage is not available in this desktop session.';
      return;
    }

    let encryptedToken = null;
    try {
      const payload = JSON.parse(fs.readFileSync(this.tokenStorePath, 'utf8'));
      const encodedToken = String(payload?.encryptedToken || '');
      if (
        payload?.version !== 1
        || !encodedToken
        || !/^[A-Za-z0-9+/]+={0,2}$/.test(encodedToken)
      ) {
        throw new Error('unsupported or corrupt token record');
      }
      encryptedToken = Buffer.from(encodedToken, 'base64');
      if (encryptedToken.length === 0 || encryptedToken.toString('base64') !== encodedToken) {
        throw new Error('unsupported or corrupt token record');
      }
      const decrypted = await decryptProtectedString(encryptedToken);
      const token = normalizeToken(decrypted.plaintext);
      this.authToken = token || null;
      this.authSource = token ? 'stored' : 'none';
      this.authError = token ? null : 'Stored update token is empty.';
      if (token && decrypted.shouldReEncrypt) {
        await this.writeStoredUpdateToken(token);
      }
    } catch (error) {
      this.authToken = null;
      this.authSource = 'none';
      this.authError = `Could not load the stored update token: ${error.message}`;
    } finally {
      if (encryptedToken) encryptedToken.fill(0);
    }
  }

  scheduleFeedConfiguration() {
    this.feedConfigurationPromise = this.feedConfigurationPromise.then(
      () => this.applyAuthHeader(),
      () => this.applyAuthHeader(),
    );
    return this.feedConfigurationPromise;
  }

  async writeStoredUpdateToken(token) {
    let encryptedToken = null;
    try {
      encryptedToken = await encryptProtectedString(token);
      atomicWriteFile(
        this.tokenStorePath,
        `${JSON.stringify({
          version: 1,
          encryptedToken: encryptedToken.toString('base64'),
          updatedAt: isoNow(),
        }, null, 2)}\n`,
        { encoding: 'utf8', mode: 0o600 },
      );
    } finally {
      if (encryptedToken) encryptedToken.fill(0);
    }
  }

  async applyAuthHeader() {
    if (!this.app.isPackaged) {
      autoUpdater.requestHeaders = null;
      return true;
    }
    try {
      const stampedConfiguration = await readStampedUpdateConfiguration();
      const feedConfiguration = buildStampedGithubFeed(
        stampedConfiguration,
        this.authToken,
        { forcePrivate: process.env.BREAKTWENTY_GITHUB_UPDATE_PRIVATE === '1' },
      );
      autoUpdater.allowPrerelease = prereleaseUpdatesEnabled({
        appVersion: this.app.getVersion(),
        privateFeed: feedConfiguration.private === true,
      });
      this.updateFeed = {
        provider: feedConfiguration.provider || null,
        owner: feedConfiguration.owner || null,
        repo: feedConfiguration.repo || null,
        private: feedConfiguration.private === true,
      };
      autoUpdater.setFeedURL(feedConfiguration);
      autoUpdater.updateInfoAndProvider = null;
      if (feedConfiguration.token) {
        autoUpdater.addAuthHeader(`token ${feedConfiguration.token}`);
      } else {
        autoUpdater.requestHeaders = null;
      }
      this.updateState({});
    } catch (error) {
      this.authError = `Could not configure the update feed: ${error.message}`;
      this.updateState({});
      return false;
    }
    return true;
  }

  async savePrivateUpdateToken(request = {}) {
    await this.tokenInitializationPromise;
    await this.feedConfigurationPromise;
    if (this.updateFeed.private === false) {
      return {
        ...this.getStatus(),
        actionStatus: 'error',
        actionMessage: 'This package uses the public BreakTwenty update feed and does not need a GitHub token.',
      };
    }
    if (updateAuthMutationBlocked(this.state.status)) {
      return {
        ...this.getStatus(),
        actionStatus: 'error',
        actionMessage: 'Finish or restart the current update before changing its GitHub token.',
      };
    }
    const token = normalizeToken(request.token);
    if (!token) {
      return {
        ...this.updateState({ auth: this.getAuthStatus() }),
        actionStatus: 'error',
        actionMessage: 'Paste a GitHub token before saving.',
      };
    }
    if (!this.storageStatus.secure) {
      this.authError = 'Encrypted token storage is not available in this desktop session.';
      return {
        ...this.updateState({ auth: this.getAuthStatus() }),
        actionStatus: 'error',
        actionMessage: this.authError,
      };
    }

    try {
      await this.writeStoredUpdateToken(token);
      this.authToken = token;
      this.authSource = 'stored';
      this.authError = null;
      this.hasStoredToken = true;
      this.scheduleFeedConfiguration();
      return {
        ...this.updateState(stateAfterUpdateFeedChange({
          enabled: this.enabled,
          disabledMessage: this.disabledMessage,
          successMessage: 'Private update token saved. Ready to check for updates.',
        })),
        actionStatus: 'ok',
        actionMessage: 'Private update token saved.',
      };
    } catch (error) {
      this.authError = `Could not save the update token: ${error.message}`;
      return {
        ...this.updateState({ auth: this.getAuthStatus() }),
        actionStatus: 'error',
        actionMessage: this.authError,
      };
    }
  }

  async clearPrivateUpdateToken() {
    await this.tokenInitializationPromise;
    await this.feedConfigurationPromise;
    if (updateAuthMutationBlocked(this.state.status)) {
      return {
        ...this.getStatus(),
        actionStatus: 'error',
        actionMessage: 'Finish or restart the current update before changing its GitHub token.',
      };
    }
    try {
      fs.rmSync(this.tokenStorePath, { force: true });
    } catch (error) {
      this.authError = `Could not remove the stored update token: ${error.message}`;
      return {
        ...this.updateState({ auth: this.getAuthStatus() }),
        actionStatus: 'error',
        actionMessage: this.authError,
      };
    }

    this.hasStoredToken = false;
    const envToken = getEnvUpdateToken();
    this.authToken = envToken || null;
    this.authSource = envToken ? 'environment' : 'none';
    this.authError = null;
    this.scheduleFeedConfiguration();
    return {
      ...this.updateState(stateAfterUpdateFeedChange({
        enabled: this.enabled,
        disabledMessage: this.disabledMessage,
        successMessage: 'Stored update token removed. Ready to check for updates.',
      })),
      actionStatus: 'ok',
      actionMessage: envToken
        ? 'Stored token removed. The environment token is still active.'
        : 'Stored update token removed.',
    };
  }

  ensureEnabled() {
    if (this.shuttingDown) {
      return false;
    }
    if (this.enabled) {
      return true;
    }
    this.updateState({
      status: 'unavailable',
      message: this.disabledMessage,
      error: null,
      canCheck: false,
      canDownload: false,
      canInstall: false,
    });
    return false;
  }

  scheduleStartupCheck(delayMs = 10000) {
    if (this.shuttingDown || !this.enabled || this.autoCheckTimer) {
      return;
    }
    this.autoCheckTimer = setTimeout(() => {
      this.autoCheckTimer = null;
      void this.checkForUpdates();
    }, delayMs);
  }

  async checkForUpdates() {
    if (!this.ensureEnabled()) {
      return this.getStatus();
    }
    if (['checking', 'downloading', 'downloaded', 'installing'].includes(this.state.status)) {
      return this.getStatus();
    }

    this.updateState({
      status: 'checking',
      message: 'Checking for updates.',
      progress: null,
      error: null,
      canDownload: false,
      canInstall: false,
    });

    try {
      const feedReady = await this.feedConfigurationPromise;
      if (!feedReady) {
        throw new Error(this.authError || 'Could not configure the packaged update feed.');
      }
      try {
        await autoUpdater.checkForUpdates();
      } catch (error) {
        if (!prereleaseToStableFallbackAllowed({
          error,
          appVersion: this.app.getVersion(),
          allowPrerelease: autoUpdater.allowPrerelease,
          privateFeed: this.updateFeed.private === true,
          explicitChannel: Boolean(process.env.BREAKTWENTY_UPDATE_CHANNEL),
        })) {
          throw error;
        }
        autoUpdater.allowPrerelease = false;
        autoUpdater.updateInfoAndProvider = null;
        this.updateState({
          status: 'checking',
          message: 'Prerelease testing is complete. Checking the stable release.',
          progress: null,
          error: null,
          canDownload: false,
          canInstall: false,
        });
        await autoUpdater.checkForUpdates();
      }
    } catch (error) {
      const exactSecrets = [this.authToken];
      const message = formatUpdateErrorMessage(error, exactSecrets);
      return this.updateState({
        status: 'error',
        message,
        progress: null,
        error: cleanError(error, exactSecrets),
        canDownload: Boolean(this.state.updateInfo),
        canInstall: false,
      });
    }
    return this.getStatus();
  }

  async downloadUpdate() {
    if (!this.ensureEnabled()) {
      return this.getStatus();
    }
    if (['downloading', 'downloaded', 'installing'].includes(this.state.status)) {
      return this.getStatus();
    }
    if (!this.state.updateInfo) {
      const status = await this.checkForUpdates();
      if (status.status !== 'available') {
        return status;
      }
    }
    this.updateState({
      status: 'downloading',
      message: 'Downloading update.',
      progress: null,
      error: null,
      canDownload: false,
      canInstall: false,
    });

    try {
      await autoUpdater.downloadUpdate();
    } catch (error) {
      const exactSecrets = [this.authToken];
      const message = formatUpdateErrorMessage(error, exactSecrets);
      return this.updateState({
        status: 'error',
        message,
        progress: null,
        error: cleanError(error, exactSecrets),
        canDownload: Boolean(this.state.updateInfo),
        canInstall: false,
      });
    }
    return this.getStatus();
  }

  quiesce() {
    this.shuttingDown = true;
    if (this.autoCheckTimer) {
      clearTimeout(this.autoCheckTimer);
      this.autoCheckTimer = null;
    }
  }

  async installUpdate() {
    if (!this.ensureEnabled()) {
      return this.getStatus();
    }
    if (this.state.status === 'installing') {
      return this.getStatus();
    }
    if (this.state.status !== 'downloaded') {
      return this.updateState({
        status: 'error',
        message: 'No downloaded update is ready to install.',
        error: null,
        canDownload: Boolean(this.state.updateInfo),
        canInstall: false,
      });
    }

    const status = this.updateState({
      status: 'installing',
      message: `Installing update. ${APP_BRAND_NAME} will reopen automatically.`,
      error: null,
      canDownload: false,
      canInstall: false,
    });
    let hiddenInstallWindow = null;
    try {
      await this.prepareForInstall();
      const installWindow = this.getMainWindow();
      if (installWindow && !installWindow.isDestroyed()) {
        installWindow.hide();
        hiddenInstallWindow = installWindow;
      }
      const helperStarted = await tryStartPersistentInstallHelper({
        app: this.app,
        version: this.state.updateInfo?.version || null,
      });
      if (!helperStarted) {
        log.warn('Update progress helper was unavailable; continuing with update installation.');
      }
      autoUpdater.quitAndInstall(true, true);
    } catch (error) {
      if (hiddenInstallWindow && !hiddenInstallWindow.isDestroyed()) {
        try {
          hiddenInstallWindow.show();
          hiddenInstallWindow.focus();
        } catch (restoreError) {
          log.warn(`Could not restore the application window after update handoff failed: ${restoreError.message}`);
        }
      }
      return this.updateState({
        status: 'error',
        message: `Could not prepare ${APP_BRAND_NAME} for update installation: ${error.message} Restart ${APP_BRAND_NAME} before trying again.`,
        error: cleanError(error, [this.authToken]),
        canDownload: false,
        canInstall: false,
      });
    }
    return status;
  }
}

module.exports = {
  BreakTwentyAppUpdater,
  UPDATE_EVENTS_CHANNEL,
};
