const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { terminateChildProcess } = require('./appLifecycle');
const { normalizeLocalBackendApiUrl } = require('./backendApiUrl');
const { normalizeMoomooOAuthAuthorizationUrl } = require('./externalNavigation');
const {
  MANAGED_VISIBLE_AUTH_PYTHON,
  isPythonCommand,
  resolveVisibleAuthPython,
} = require('./visibleAuthPython');

const DESKTOP_AUTH_LOCALE_ENV = Object.freeze({
  LANG: 'en_CA.UTF-8',
  LANGUAGE: 'en_CA:en',
  LC_ALL: 'en_CA.UTF-8',
  LC_CTYPE: 'en_CA.UTF-8',
});
const VISIBLE_AUTH_RESULT_PREFIX = 'BREAKTWENTY_VISIBLE_AUTH_RESULT ';
const VISIBLE_AUTH_LOG_PATH_PREFIX = 'BREAKTWENTY_VISIBLE_AUTH_LOG_PATH ';
const VISIBLE_AUTH_HEARTBEAT_PREFIX = 'BREAKTWENTY_VISIBLE_AUTH_HEARTBEAT ';
const VISIBLE_AUTH_DIAGNOSTICS_READY_PREFIX = 'BREAKTWENTY_VISIBLE_AUTH_DIAGNOSTICS_READY ';
const DEFAULT_VISIBLE_AUTH_STALL_WATCHDOG_SECONDS = 10 * 60;
const MAX_RUNNER_OUTPUT_LINE_CHARS = 16 * 1024;
const SECURE_BROWSER_CLOSED_TEXT = 'login window was closed before secure login finished';
const SAFE_ATTEMPT_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;
const RUNNER_GRANT_REQUEST_TIMEOUT_MS = 5000;

function normalizeInstitutionId(value) {
  const institutionId = Number(value || 0);
  return Number.isInteger(institutionId) && institutionId > 0 ? String(institutionId) : '';
}

class VisibleAuthBroker {
  constructor({
    appRoot,
    backendApiUrl,
    backendMode = 'docker',
    runtimeEnv = {},
    getLaunchAuthToken = null,
    restoreBackendReadiness = null,
  }) {
    this.appRoot = appRoot;
    this.backendApiUrl = normalizeLocalBackendApiUrl(backendApiUrl);
    this.backendMode = String(backendMode || 'docker').toLowerCase() === 'embedded' ? 'embedded' : 'docker';
    this.runtimeEnv = { ...runtimeEnv };
    this.getLaunchAuthToken = typeof getLaunchAuthToken === 'function' ? getLaunchAuthToken : null;
    this.restoreBackendReadiness = typeof restoreBackendReadiness === 'function'
      ? restoreBackendReadiness
      : null;
    this.sessions = new Map();
    this.acceptingLaunches = true;
    this.providerCatalogPath = path.join(this.appRoot, 'config', 'provider_catalog.json');
    this.visibleAuthLogDir = path.join(this.appRoot, '.desktop-auth', 'logs', 'visible-auth');
    this.applyRuntimeEnv(this.runtimeEnv);
  }

  setRuntimeEnv(runtimeEnv = {}) {
    this.runtimeEnv = { ...runtimeEnv };
    this.applyRuntimeEnv(this.runtimeEnv);
  }

  quiesce() {
    this.acceptingLaunches = false;
  }

  async shutdown() {
    this.quiesce();
    const activeSessions = [...this.sessions.values()].filter(
      (session) => session.process && session.process.exitCode === null,
    );
    const results = await Promise.all(activeSessions.map(async (session) => {
      session.status = 'cancelled';
      session.message = 'Closing secure login because BreakTwenty is shutting down.';
      this.appendSessionLog(session, 'broker', session.message);
      const stopped = await terminateChildProcess(session.process, { processTree: true });
      session.finishedAt = session.finishedAt || new Date().toISOString();
      if (!stopped) {
        session.status = 'failed';
        session.message = 'Secure login process did not stop cleanly during shutdown.';
        this.appendSessionLog(session, 'broker', session.message);
      }
      return stopped;
    }));
    return results.every(Boolean);
  }

  applyRuntimeEnv(runtimeEnv = {}) {
    if (runtimeEnv.PROVIDER_CATALOG_PATH) {
      this.providerCatalogPath = runtimeEnv.PROVIDER_CATALOG_PATH;
    }
    if (runtimeEnv.BREAKTWENTY_DESKTOP_AUTH_DIR) {
      this.visibleAuthLogDir = path.join(
        runtimeEnv.BREAKTWENTY_DESKTOP_AUTH_DIR,
        'logs',
        'visible-auth',
      );
    }
  }

  describe() {
    const providerCatalog = this.loadProviderCatalog();
    return {
      contractVersion: 1,
      backendApiUrl: this.backendApiUrl,
      backendMode: this.backendMode,
      configuredProviders: Object.entries(providerCatalog)
        .filter(([, metadata]) => Boolean(this.getVisibleAuthConfig(metadata)?.enabled))
        .map(([provider, metadata]) => ({
          provider,
          displayName: metadata.displayName || provider,
          artifactKinds: this.getArtifactKinds(metadata),
        })),
    };
  }

  async launch(request) {
    const providerCatalog = this.loadProviderCatalog();
    const provider = this.normalizeProvider(request.provider);
    if (!this.acceptingLaunches) {
      return this.errorSnapshot(provider, 'BreakTwenty is shutting down; secure login cannot start.');
    }
    const metadata = providerCatalog[provider];
    if (!metadata) {
      return this.errorSnapshot(provider, 'Unknown provider for desktop visible auth.');
    }

    const existing = this.sessions.get(provider);
    if (existing) {
      this.checkTechnicalStall(existing);
    }
    if (existing && !existing.process && existing.status === 'running' && !existing.finishedAt) {
      return {
        ...this.snapshot(existing),
        requestStatus: 'already_running',
      };
    }
    if (existing?.process && existing.process.exitCode === null) {
      if (existing.status === 'cancelled' || existing.status === 'failed') {
        this.appendSessionLog(existing, 'broker', `Discarding ${existing.status} visible auth session before relaunch.`);
        void terminateChildProcess(existing.process, {
          forceWaitMs: 1000,
          graceMs: 0,
          processTree: true,
        });
        this.sessions.delete(provider);
      } else {
        return {
          ...this.snapshot(existing),
          requestStatus: 'already_running',
        };
      }
    }

    const visibleAuth = this.getVisibleAuthConfig(metadata);
    if (!visibleAuth?.enabled) {
      return this.errorSnapshot(
        provider,
        `${metadata.displayName || provider} is not configured for desktop visible auth yet.`,
      );
    }

    const command = Array.isArray(visibleAuth.runnerCommand) ? visibleAuth.runnerCommand : [];
    if (command.length === 0) {
      return this.errorSnapshot(
        provider,
        `${metadata.displayName || provider} desktop visible auth has no runner command configured.`,
      );
    }

    const requestedAttemptId = provider === 'moomoo'
      ? String(request.attemptId || request.attempt_id || '').trim()
      : '';
    if (requestedAttemptId && !SAFE_ATTEMPT_ID_RE.test(requestedAttemptId)) {
      return this.errorSnapshot(provider, 'Secure login attempt identity was invalid.');
    }
    if (provider === 'moomoo' && !requestedAttemptId) {
      return this.errorSnapshot(provider, 'Moomoo authorization browser handoff was incomplete or unsafe.');
    }
    const attemptId = requestedAttemptId || crypto.randomUUID();
    const startedAtMs = Date.now();
    const timeoutSeconds = this.resolveVisibleAuthTimeoutSeconds(request, visibleAuth);
    const technicalStallSeconds = this.resolveTechnicalStallSeconds(request, visibleAuth);
    const session = {
      provider,
      attemptId,
      status: 'running',
      message: `Opening ${metadata.displayName || provider} secure login browser.`,
      lastOutput: '',
      lastOutputAt: startedAtMs,
      lastHeartbeatAt: startedAtMs,
      logPath: '',
      resultData: null,
      diagnosticsReady: null,
      diagnosticsPublished: false,
      startedAt: new Date().toISOString(),
      finishedAt: null,
      artifactKinds: this.getArtifactKinds(metadata),
      technicalStallSeconds,
      process: null,
    };

    const [commandName, ...commandArgs] = this.resolveRunnerCommand(command);
    session.logPath = this.createSessionLogPath(provider, attemptId);
    if (!this.commandAvailable(commandName)) {
      return this.errorSnapshot(
        provider,
        `Desktop visible auth runner command is missing: ${commandName || MANAGED_VISIBLE_AUTH_PYTHON}. Set BREAKTWENTY_VISIBLE_AUTH_PYTHON or prepare the visible-auth Python environment from ${this.runtimeEnv.BREAKTWENTY_VISIBLE_AUTH_REQUIREMENTS_PATH || 'scripts/desktop_visible_auth_requirements.lock'}.`,
      );
    }
    this.appendSessionLog(session, 'broker', `Launching ${provider} visible auth.`);
    this.appendSessionLog(session, 'broker', `Command: ${[commandName, ...commandArgs].join(' ')}`);
    const localeEnvMode = this.getLocaleEnvMode(visibleAuth);
    const recordHarEnabled = this.getRecordHarEnabled(visibleAuth, request);
    const captureLevel = this.normalizeCaptureLevel(request.captureLevel);
    const institutionId = normalizeInstitutionId(request.institutionId || request.institution_id);
    const providerSpecificEnv = {};
    if (provider === 'moomoo') {
      const authorizationUrl = normalizeMoomooOAuthAuthorizationUrl(request.authorizationUrl);
      const syncId = String(request.syncId || '').trim();
      if (!authorizationUrl || !/^[A-Za-z0-9_-]{1,128}$/.test(syncId)) {
        return this.errorSnapshot(
          provider,
          'Moomoo authorization browser handoff was incomplete or unsafe.',
        );
      }
      providerSpecificEnv.BREAKTWENTY_MOOMOO_OAUTH_AUTHORIZATION_URL = authorizationUrl;
      providerSpecificEnv.BREAKTWENTY_VISIBLE_AUTH_SYNC_ID = syncId;
    }
    // Publish the pending authorization before awaiting the backend so repeated
    // renderer clicks cannot create a second browser process for this provider.
    this.sessions.set(provider, session);
    const runnerGrant = crypto.randomBytes(32).toString('base64');
    try {
      await this.authorizeRunnerGrant({
        grant: runnerGrant,
        provider,
        attemptId,
        institutionId,
        addFlow: Boolean(request.addFlow),
      }, session);
    } catch (error) {
      if (this.sessions.get(provider) === session) this.sessions.delete(provider);
      return this.errorSnapshot(
        provider,
        `Could not authorize the secure login runner: ${error.message}`,
      );
    }
    const runnerEnv = this.buildRunnerEnv({
      visibleAuth,
      extraEnv: {
        BREAKTWENTY_BACKEND_API_URL: this.backendApiUrl,
        BREAKTWENTY_VISIBLE_AUTH_PROVIDER: provider,
        BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID: attemptId,
        BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW: request.addFlow ? '1' : '0',
        BREAKTWENTY_VISIBLE_AUTH_INSTITUTION_ID: institutionId,
        BREAKTWENTY_VISIBLE_AUTH_USER_ID: '1',
        BREAKTWENTY_VISIBLE_AUTH_TIMEZONE: String(request.userTimezone || ''),
        BREAKTWENTY_VISIBLE_AUTH_TIMEOUT_SECONDS: String(timeoutSeconds),
        BREAKTWENTY_VISIBLE_AUTH_CAPTURE_LEVEL: captureLevel,
        BREAKTWENTY_VISIBLE_AUTH_RECORD_HAR: recordHarEnabled ? '1' : '0',
        BREAKTWENTY_VISIBLE_AUTH_LOG_PATH: session.logPath,
        ...providerSpecificEnv,
      },
    });
    this.appendSessionLog(session, 'broker', `Locale env mode: ${localeEnvMode}`);
    this.appendSessionLog(session, 'broker', `Diagnostic capture level: ${captureLevel}`);
    this.appendSessionLog(session, 'broker', `HAR capture enabled: ${recordHarEnabled ? 'yes' : 'no'}`);
    this.appendSessionLog(session, 'broker', `Interactive timeout seconds: ${timeoutSeconds > 0 ? timeoutSeconds : 'none'}`);
    this.appendSessionLog(session, 'broker', `Technical stall watchdog seconds: ${technicalStallSeconds > 0 ? technicalStallSeconds : 'disabled'}`);
    this.appendSessionLog(session, 'broker', `Env keys: ${Object.keys(runnerEnv).sort().join(', ')}`);
    const child = spawn(commandName, commandArgs, {
      cwd: this.appRoot,
      env: runnerEnv,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      detached: process.platform !== 'win32',
    });

    session.process = child;
    this.sessions.set(provider, session);
    this.bindProcessOutput(session, child);
    const bootstrap = Buffer.from(`${JSON.stringify({ version: 1, grant: runnerGrant })}\n`, 'utf8');
    let bootstrapCleared = false;
    const clearBootstrap = () => {
      if (bootstrapCleared) return;
      bootstrapCleared = true;
      bootstrap.fill(0);
    };
    child.stdin.once('error', (error) => {
      clearBootstrap();
      this.appendSessionLog(session, 'broker', `Runner bootstrap pipe error: ${error.message}`);
    });
    child.once('exit', clearBootstrap);
    child.stdin.end(bootstrap, clearBootstrap);

    return {
      ...this.snapshot(session),
      requestStatus: 'launched',
    };
  }

  status(request) {
    const provider = this.normalizeProvider(request.provider);
    const session = this.sessions.get(provider);
    if (!session) {
      return this.errorSnapshot(provider, 'No desktop visible auth session is running.', 'idle');
    }
    if (request.attemptId && request.attemptId !== session.attemptId) {
      return this.errorSnapshot(provider, 'No matching desktop visible auth session is running.', 'idle');
    }
    this.checkTechnicalStall(session);
    return this.snapshot(session);
  }

  cancel(request) {
    const provider = this.normalizeProvider(request.provider);
    const session = this.sessions.get(provider);
    if (!session) {
      return this.errorSnapshot(provider, 'No desktop visible auth session is running.', 'idle');
    }
    if (request.attemptId && request.attemptId !== session.attemptId) {
      return this.errorSnapshot(provider, 'No matching desktop visible auth session is running.', 'idle');
    }
    if (session.process && session.process.exitCode === null) {
      session.status = 'cancelled';
      session.message = 'Cancelling desktop visible auth.';
      void terminateChildProcess(session.process, { processTree: true });
    }
    return this.snapshot(session);
  }

  bindProcessOutput(session, child) {
    const buffers = { stdout: '', stderr: '' };
    const processLine = (source, rawLine) => {
      const line = String(rawLine || '').trim();
      if (!line) return;
      const observedAt = Date.now();
      session.lastOutputAt = observedAt;
      if (line.startsWith(VISIBLE_AUTH_HEARTBEAT_PREFIX)) {
        session.lastHeartbeatAt = observedAt;
        return;
      }
      if (line.startsWith(VISIBLE_AUTH_RESULT_PREFIX)) {
        const result = this.normalizeRunnerResult(
          line.slice(VISIBLE_AUTH_RESULT_PREFIX.length),
          session,
        );
        if (result) session.resultData = result;
        return;
      }
      if (line.startsWith(VISIBLE_AUTH_DIAGNOSTICS_READY_PREFIX)) {
        const diagnosticsReady = this.normalizeDiagnosticsReady(
          line.slice(VISIBLE_AUTH_DIAGNOSTICS_READY_PREFIX.length),
          session,
        );
        if (diagnosticsReady) session.diagnosticsReady = diagnosticsReady;
        return;
      }
      if (line.startsWith(VISIBLE_AUTH_LOG_PATH_PREFIX)) return;
      if (line.toLowerCase().includes(SECURE_BROWSER_CLOSED_TEXT)) {
        session.status = 'failed';
        session.finishedAt = new Date().toISOString();
        session.message = line;
        session.lastOutput = line;
        this.appendSessionLog(session, 'broker', 'Secure login browser closed before handoff.');
        return;
      }
      this.appendSessionLog(session, source, '[runner output withheld]');
      if (this.isFatalRunnerOutput(line)) {
        this.failSessionFromFatalOutput(session, child);
      }
    };
    const capture = (source, { final = false } = {}) => (chunk) => {
      buffers[source] += String(chunk || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
      if (buffers[source].length > MAX_RUNNER_OUTPUT_LINE_CHARS) {
        buffers[source] = '';
        processLine(source, '[oversized runner output withheld]');
        return;
      }
      const lines = buffers[source].split('\n');
      buffers[source] = lines.pop() || '';
      for (const line of lines) processLine(source, line);
      if (final && buffers[source]) {
        processLine(source, buffers[source]);
        buffers[source] = '';
      }
    };
    child.stdout.on('data', capture('stdout'));
    child.stderr.on('data', capture('stderr'));
    child.on('error', () => {
      session.status = 'failed';
      session.finishedAt = new Date().toISOString();
      session.message = 'Secure login process could not start. Start sync again.';
      this.appendSessionLog(session, 'broker', 'Secure login runner process error.');
    });
    child.on('close', (code) => {
      capture('stdout', { final: true })('');
      capture('stderr', { final: true })('');
      void this.revokeRunnerScope(session);
      session.finishedAt = session.finishedAt || new Date().toISOString();
      const handoffReady = this.hasHandoffReadyResult(session);
      if (session.status === 'cancelled') {
        session.message = 'Desktop visible auth was cancelled.';
        this.appendSessionLog(session, 'broker', session.message);
      } else if (session.status === 'failed') {
        this.appendSessionLog(session, 'broker', `Process exited with code ${code}. ${session.message}`);
      } else if (code === 0 || handoffReady) {
        session.status = 'succeeded';
        session.message = handoffReady
          ? 'Desktop visible auth completed with handoff-ready result.'
          : 'Desktop visible auth completed.';
        this.appendSessionLog(session, 'broker', session.message);
      } else {
        session.status = 'failed';
        session.message = 'Secure login did not finish. Start sync again.';
        this.appendSessionLog(session, 'broker', `Process exited with code ${code}.`);
      }
      void this.publishDiagnosticsReady(session);
    });
  }

  normalizeRunnerResult(rawPayload, session) {
    let payload;
    try {
      payload = JSON.parse(rawPayload);
    } catch (_error) {
      return null;
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    const provider = String(payload.provider || '').trim().toLowerCase();
    const attemptId = String(payload.attemptId || '').trim();
    const responseStatus = String(payload.responseStatus || '').trim().toLowerCase();
    const syncId = String(payload.syncId || '').trim();
    if (
      provider !== session.provider
      || attemptId !== session.attemptId
      || !['handoff_ready', 'completed'].includes(responseStatus)
      || !/^[A-Za-z0-9_-]{1,128}$/.test(syncId)
    ) {
      return null;
    }
    return {
      provider,
      attemptId,
      syncId,
      synced: payload.synced === true,
      authenticated: payload.authenticated === true,
      storageStateStaged: payload.storageStateStaged === true,
      sessionArtifactStaged: payload.sessionArtifactStaged === true,
      credentialsCaptured: payload.credentialsCaptured === true,
      credentialsStaged: payload.credentialsStaged === true,
      responseStatus,
    };
  }

  normalizeDiagnosticsReady(rawPayload, session) {
    let payload;
    try {
      payload = JSON.parse(rawPayload);
    } catch (_error) {
      return null;
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    const provider = String(payload.provider || '').trim().toLowerCase();
    const attemptId = String(payload.attempt_id || '').trim();
    const syncId = String(payload.sync_id || '').trim();
    const result = String(payload.result || '').trim().toLowerCase();
    if (
      provider !== session.provider
      || attemptId !== session.attemptId
      || !/^[A-Za-z0-9_-]{1,128}$/.test(syncId)
      || !['error', 'failed', 'cancelled'].includes(result)
    ) {
      return null;
    }
    return {
      provider,
      attemptId,
      syncId,
      addFlow: payload.add_flow === true,
      result,
      message: String(payload.message || '').trim().slice(0, 1200),
      lastOutput: String(payload.last_output || '').trim().slice(0, 1200),
    };
  }

  checkTechnicalStall(session) {
    if (!session || session.status !== 'running' || !session.process || session.process.exitCode !== null) {
      return;
    }
    const watchdogSeconds = Number(session.technicalStallSeconds || 0);
    if (!Number.isFinite(watchdogSeconds) || watchdogSeconds <= 0) {
      return;
    }
    const lastResponsiveAt = Math.max(
      Number(session.lastHeartbeatAt || 0),
      Number(session.lastOutputAt || 0),
    );
    if (!lastResponsiveAt || Date.now() - lastResponsiveAt <= watchdogSeconds * 1000) {
      return;
    }
    const message = 'Secure login runner stopped responding while the bank browser was open. Close any leftover secure browser window and start sync again.';
    session.status = 'failed';
    session.finishedAt = new Date().toISOString();
    session.message = message;
    session.lastOutput = message;
    this.appendSessionLog(session, 'broker', `Technical stall watchdog fired after ${watchdogSeconds}s without runner heartbeat/output.`);
    void terminateChildProcess(session.process, { processTree: true });
  }

  isFatalRunnerOutput(line) {
    const lowered = String(line || '').toLowerCase();
    return (
      lowered.includes('protocolerror: protocol error') ||
      lowered.includes('node:internal/process/promises') ||
      lowered.includes('triggeruncaughtexception') ||
      lowered.includes('pipe closed by peer')
    );
  }

  hasHandoffReadyResult(session) {
    return Boolean(
      session?.resultData?.synced ||
      (session?.resultData?.authenticated && session?.resultData?.responseStatus === 'handoff_ready')
    );
  }

  failSessionFromFatalOutput(session, child) {
    if (!session || session.status !== 'running' || this.hasHandoffReadyResult(session)) {
      return;
    }
    const message = 'Secure login browser automation crashed before login finished. Start sync again.';
    session.status = 'failed';
    session.finishedAt = new Date().toISOString();
    session.message = message;
    session.lastOutput = message;
    this.appendSessionLog(session, 'broker', 'Fatal runner output detected; raw output withheld.');
    void terminateChildProcess(child, { processTree: true });
  }

  resolveRunnerCommand(command) {
    const resolved = command.map((part) => {
      const text = String(part || '');
      if (!text || path.isAbsolute(text)) {
        return text;
      }
      if (text.includes('/') || text.includes('\\')) {
        return path.join(this.appRoot, text);
      }
      if (text.endsWith('.py')) {
        return path.join(this.appRoot, text);
      }
      return text;
    });
    if (resolved.length > 0) {
      const pythonCommand = this.resolveVisibleAuthPython(resolved[0]);
      resolved[0] = resolved[0] === MANAGED_VISIBLE_AUTH_PYTHON
        ? pythonCommand
        : (pythonCommand || resolved[0]);
    }
    return resolved;
  }

  resolveVisibleAuthPython(commandName) {
    if (!isPythonCommand(commandName)) {
      return '';
    }
    return resolveVisibleAuthPython({
      runtimeEnv: this.runtimeEnv,
      commandName,
    });
  }

  commandAvailable(commandName) {
    if (!commandName) {
      return false;
    }
    return !commandName.includes(path.sep) || fs.existsSync(commandName);
  }

  buildRunnerEnv({ visibleAuth = null, extraEnv = {} } = {}) {
    const env = {
      ...process.env,
      ...this.runtimeEnv,
      PYTHONUNBUFFERED: '1',
      ...this.getLocaleEnvOverrides(visibleAuth),
      ...extraEnv,
    };
    delete env.BREAKTWENTY_DATABASE_ENCRYPTION_KEY;
    delete env.BREAKTWENTY_APP_ENCRYPTION_KEY;
    delete env.BREAKTWENTY_LAUNCH_TOKEN;
    delete env.BREAKTWENTY_LAUNCH_TOKEN_FILE;
    delete env.BREAKTWENTY_RUNNER_GRANT;
    return env;
  }

  async registerRunnerGrant({ grant, provider, attemptId, institutionId, addFlow }) {
    if (!this.getLaunchAuthToken) {
      throw new Error('desktop launch authentication is unavailable');
    }
    const launchAuthToken = this.getLaunchAuthToken();
    let response;
    try {
      response = await fetch(`${this.backendApiUrl}/auth/runner-grants`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${launchAuthToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          grant,
          provider,
          attempt_id: attemptId,
          institution_id: institutionId ? Number(institutionId) : null,
          add_flow: addFlow,
        }),
        signal: AbortSignal.timeout(RUNNER_GRANT_REQUEST_TIMEOUT_MS),
      });
    } catch (error) {
      error.runnerGrantRetryable = true;
      throw error;
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      const error = new Error(String(payload?.detail || `backend returned HTTP ${response.status}`));
      error.runnerGrantRetryable = response.status >= 500;
      throw error;
    }
  }

  async authorizeRunnerGrant(request, session) {
    try {
      await this.registerRunnerGrant(request);
      return;
    } catch (error) {
      if (!error?.runnerGrantRetryable || !this.restoreBackendReadiness) throw error;
      this.appendSessionLog(
        session,
        'broker',
        `Runner authorization was interrupted; waiting for backend readiness (${error.message}).`,
      );
      const readiness = await this.restoreBackendReadiness({
        provider: request.provider,
        attemptId: request.attemptId,
        error,
      });
      if (!readiness?.ok) {
        throw new Error(String(readiness?.message || error.message));
      }
      if (!this.acceptingLaunches) {
        throw new Error('BreakTwenty is shutting down; secure login cannot start.');
      }
      this.appendSessionLog(session, 'broker', 'Backend readiness restored; retrying runner authorization once.');
      await this.registerRunnerGrant(request);
    }
  }

  async revokeRunnerScope(session) {
    if (!this.getLaunchAuthToken || !session?.provider || !session?.attemptId) return;
    try {
      await fetch(`${this.backendApiUrl}/auth/runner-sessions/revoke`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${this.getLaunchAuthToken()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          provider: session.provider,
          attempt_id: session.attemptId,
          purpose: 'visible-auth',
        }),
        signal: AbortSignal.timeout(5000),
      });
    } catch (_error) {
      // Session TTL and launch rotation are the fail-closed fallback if the local
      // backend vanished before this best-effort process-exit revocation.
    }
  }

  async publishDiagnosticsReady(session) {
    const diagnostics = session?.diagnosticsReady;
    if (!this.getLaunchAuthToken || !diagnostics || session.diagnosticsPublished) return;
    session.diagnosticsPublished = true;
    const request = {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.getLaunchAuthToken()}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        source: 'desktop_visible_auth',
        provider: diagnostics.provider,
        sync_id: diagnostics.syncId,
        attempt_id: diagnostics.attemptId,
        add_flow: diagnostics.addFlow,
        stage: 'runner finalized',
        result: diagnostics.result,
        level: 'error',
        message: diagnostics.message,
        last_output: diagnostics.lastOutput,
        details: { runner_log_complete: true },
      }),
    };
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const response = await fetch(
          `${this.backendApiUrl}/settings/support-logs/client-event`,
          { ...request, signal: AbortSignal.timeout(5000) },
        );
        if (response.ok) return;
      } catch (_error) {
        // A short retry covers backend reloads without publishing an incomplete archive.
      }
      if (attempt < 2) {
        await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)));
      }
    }
    session.diagnosticsPublished = false;
  }

  getLocaleEnvMode(visibleAuth) {
    return visibleAuth?.localeEnvMode === 'inherit' ? 'inherit' : 'managed_canadian';
  }

  getLocaleEnvOverrides(visibleAuth) {
    return this.getLocaleEnvMode(visibleAuth) === 'inherit' ? {} : DESKTOP_AUTH_LOCALE_ENV;
  }

  getRecordHarEnabled(visibleAuth, _request = {}) {
    return visibleAuth?.recordHar === true;
  }

  resolveVisibleAuthTimeoutSeconds(request = {}, visibleAuth = null) {
    const requested = this.finiteNumberOrNull(request?.timeoutSeconds);
    if (requested !== null) {
      return requested > 0 ? requested : 0;
    }
    const configured = this.finiteNumberOrNull(visibleAuth?.timeoutSeconds);
    if (configured !== null) {
      return configured > 0 ? configured : 0;
    }
    return 0;
  }

  resolveTechnicalStallSeconds(request = {}, visibleAuth = null) {
    const requested = this.finiteNumberOrNull(request?.technicalStallSeconds);
    if (requested !== null) {
      return requested > 0 ? requested : 0;
    }
    const configured = this.finiteNumberOrNull(visibleAuth?.technicalStallSeconds);
    if (configured !== null) {
      return configured > 0 ? configured : 0;
    }
    return DEFAULT_VISIBLE_AUTH_STALL_WATCHDOG_SECONDS;
  }

  finiteNumberOrNull(value) {
    if (value === undefined || value === null || value === '') {
      return null;
    }
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }

  normalizeCaptureLevel(value) {
    const normalized = String(value || '').trim().toLowerCase();
    // Two levels only; anything unrecognized falls back to the always-on
    // redacted_rich baseline (never off — a HAR-capable provider always
    // produces at least a redacted-rich HAR).
    return normalized === 'developer_local' ? 'developer_local' : 'redacted_rich';
  }

  createSessionLogPath(provider, attemptId) {
    const root = path.resolve(this.visibleAuthLogDir);
    try {
      fs.mkdirSync(root, { recursive: true, mode: 0o700 });
    } catch (_) {
      return '';
    }
    const providerSlug = String(provider || 'provider').replace(/[^a-z0-9_-]/gi, '_').slice(0, 64);
    const attemptSlug = String(attemptId || 'unknown').replace(/[^a-z0-9_-]/gi, '_').slice(0, 128);
    const candidate = path.resolve(root, `${providerSlug}-${attemptSlug}.log`);
    const relative = path.relative(root, candidate);
    if (!relative || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
      return '';
    }
    return candidate;
  }

  sessionLogPathIsConfined(logPath) {
    if (!logPath) return false;
    const root = path.resolve(this.visibleAuthLogDir);
    const candidate = path.resolve(logPath);
    const relative = path.relative(root, candidate);
    return Boolean(relative) && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
  }

  appendSessionLog(session, source, message) {
    if (!this.sessionLogPathIsConfined(session?.logPath)) {
      return;
    }
    let descriptor = -1;
    try {
      const timestamp = new Date().toISOString();
      const flags = fs.constants.O_APPEND
        | fs.constants.O_CREAT
        | fs.constants.O_WRONLY
        | (fs.constants.O_NOFOLLOW || 0);
      descriptor = fs.openSync(session.logPath, flags, 0o600);
      fs.writeFileSync(descriptor, `${timestamp} ${source} ${String(message || '')}\n`);
    } catch (_) {
      // Ignore broker-side logging failures and keep the session alive.
    } finally {
      if (descriptor >= 0) fs.closeSync(descriptor);
    }
  }

  loadProviderCatalog() {
    try {
      return JSON.parse(fs.readFileSync(this.providerCatalogPath, 'utf8'));
    } catch (_) {
      return {};
    }
  }

  getVisibleAuthConfig(metadata) {
    return (
      metadata?.frontend?.scraperAuth?.desktopVisibleAuth ||
      metadata?.backend?.runtimeState?.desktopVisibleAuth ||
      metadata?.desktop?.visibleAuth ||
      null
    );
  }

  getArtifactKinds(metadata) {
    return metadata?.backend?.runtimeState?.artifactKinds || [];
  }

  normalizeProvider(provider) {
    return String(provider || '').trim().toLowerCase();
  }

  errorSnapshot(provider, message, status = 'error') {
    return {
      provider,
      attemptId: null,
      status,
      message,
      startedAt: null,
      finishedAt: null,
      artifactKinds: [],
      running: false,
    };
  }

  snapshot(session) {
    return {
      provider: session.provider,
      attemptId: session.attemptId,
      status: session.status,
      message: session.message,
      startedAt: session.startedAt,
      finishedAt: session.finishedAt,
      lastOutputAt: session.lastOutputAt ? new Date(session.lastOutputAt).toISOString() : null,
      lastHeartbeatAt: session.lastHeartbeatAt ? new Date(session.lastHeartbeatAt).toISOString() : null,
      artifactKinds: session.artifactKinds,
      resultData: session.resultData,
      technicalStallSeconds: session.technicalStallSeconds,
      running: Boolean(session.process && session.process.exitCode === null),
    };
  }
}

module.exports = { VisibleAuthBroker };
