import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { MdVisibility, MdVisibilityOff } from 'react-icons/md';
import { renderBrandMarkedText, renderBrandText } from './BrandName';
import InstitutionLogo from './InstitutionLogo';
import InstitutionSyncProgress from './InstitutionSyncProgress';
import { API } from '../config';
import { APP_BRAND_NAME } from '../constants/brand';
import {
  API_PROVIDER_CONFIG,
  getBackgroundSyncEndpoint,
  getInstitutionInterruptedMessage,
  getInstitutionSuccessMessage,
} from '../constants/providers';
import {
  ADD_INSTITUTION_SYNC_REQUEST_TIMEOUT_MS,
  USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS,
  fetchWithTimeout,
} from '../utils/syncRequests';
import {
  clearPendingInstitutionAdd,
  confirmPendingInstitutionAdd,
  markPendingInstitutionAdd,
  startIncompleteInstitutionAddCleanup,
} from '../utils/incompleteInstitutionAdds';
import {
  cancelDesktopVisibleAuth,
  getDesktopVisibleAuthStatus,
  isDesktopShell,
  launchDesktopVisibleAuth,
} from '../utils/desktopBridge';

const API_SYNC_PROGRESS_HINT_DELAY_MS = 1200;
const SUCCESS_DISPLAY_MS = 1500;
const LONG_SYNC_API_PROVIDERS = new Set(['questrade']);
const PROVIDER_SYNC_REQUEST_TIMEOUT_MS = {
  ibkr: 3 * 60 * 1000,
};

function buildMoomooEnglishPassportUrl(authorizationUrl) {
  try {
    const authorization = new URL(String(authorizationUrl || ''));
    if (
      authorization.origin !== 'https://webapi.moomoo.com'
      || authorization.pathname !== '/oauth2/authorize/confirm'
      || authorization.username
      || authorization.password
      || authorization.hash
    ) {
      return '';
    }
    const passport = new URL('https://passport.moomoo.com/');
    passport.searchParams.set('lang', 'en-us');
    passport.searchParams.set('type', 'login');
    passport.searchParams.set('target', authorization.href);
    return passport.href;
  } catch (_error) {
    return '';
  }
}

function buildCredentialValues(fields = [], settings = {}) {
  const values = {};
  fields.forEach((field) => {
    values[field.key] = settings[field.key] || field.defaultValue || '';
  });
  return values;
}

function ApiAuthModal({ institution, onClose, onSuccess, onResultUpdate }) {
  const config = API_PROVIDER_CONFIG[institution.provider];
  const syncEndpoint = getBackgroundSyncEndpoint(institution.provider);
  const isNewInstitution = Boolean(institution.isNew);
  const syncRequestTimeoutMs = PROVIDER_SYNC_REQUEST_TIMEOUT_MS[institution.provider]
    || (
      isNewInstitution
        || LONG_SYNC_API_PROVIDERS.has(institution.provider)
        ? ADD_INSTITUTION_SYNC_REQUEST_TIMEOUT_MS
        : USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS
    );
  const credentialDefaults = useMemo(() => buildCredentialValues(config?.fields), [config]);
  const [credentialsState, setCredentialsState] = useState(() => ({
    provider: institution.provider,
    values: buildCredentialValues(config?.fields),
  }));
  const credentials = credentialsState.provider === institution.provider
    ? credentialsState.values
    : credentialDefaults;
  const setCredentials = useCallback((nextCredentials) => {
    setCredentialsState((previous) => {
      const baseValues = previous.provider === institution.provider
        ? previous.values
        : credentialDefaults;
      const values = typeof nextCredentials === 'function'
        ? nextCredentials(baseValues)
        : nextCredentials;
      return { provider: institution.provider, values };
    });
  }, [credentialDefaults, institution.provider]);
  const [fieldVisibility, setFieldVisibility] = useState({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [step, setStep] = useState('form');
  const [moomooOAuthAuthorizationUrl, setMoomooOAuthAuthorizationUrl] = useState('');
  const syncProgressTimeoutRef = useRef(null);
  const successCloseTimeoutRef = useRef(null);
  const interruptedCloseTimeoutRef = useRef(null);
  const completedRef = useRef(false);
  const closedRef = useRef(false);
  const interruptedRef = useRef(false);
  const pendingAddAttemptIdRef = useRef('');
  const pendingCredentialReplacementRef = useRef('');
  const moomooOAuthFlowIdRef = useRef('');
  const moomooOAuthBrowserAttemptIdRef = useRef('');
  const connectionIdRef = useRef(Number(institution?.id || 0) || null);
  const canSubmitCredentials = step === 'form' && !saving;
  const desktopShell = isDesktopShell();
  const useMoomooCloudOAuth = institution.provider === 'moomoo';
  const canCancelMoomooOAuth = useMoomooCloudOAuth && [
    'moomoo_oauth_preparing',
    'moomoo_oauth_waiting',
  ].includes(step);
  const canCloseModal = canSubmitCredentials || canCancelMoomooOAuth || step === 'error';

  const cancelMoomooOAuthKeepalive = useCallback(() => {
    const browserAttemptId = moomooOAuthBrowserAttemptIdRef.current;
    moomooOAuthBrowserAttemptIdRef.current = '';
    if (browserAttemptId) {
      void cancelDesktopVisibleAuth({
        provider: 'moomoo',
        attemptId: browserAttemptId,
      }).catch(() => {});
    }
    const flowId = moomooOAuthFlowIdRef.current;
    moomooOAuthFlowIdRef.current = '';
    if (!flowId) return;
    void fetch(
      `${API}/auth/moomoo/oauth/${encodeURIComponent(flowId)}/cancel`,
      { method: 'POST', keepalive: true },
    ).catch(() => {});
  }, []);

  const rollbackCredentialReplacementKeepalive = useCallback(() => {
    const replacementId = pendingCredentialReplacementRef.current;
    pendingCredentialReplacementRef.current = '';
    if (!replacementId) return;
    void fetch(
      `${API}/settings/credential-replacements/${encodeURIComponent(replacementId)}/rollback`,
      { method: 'POST', keepalive: true },
    ).catch(() => {});
  }, []);

  const clearSyncProgressTimeout = () => {
    if (syncProgressTimeoutRef.current) {
      clearTimeout(syncProgressTimeoutRef.current);
      syncProgressTimeoutRef.current = null;
    }
  };

  useEffect(() => {
    let cancelled = false;
    completedRef.current = false;
    closedRef.current = false;
    interruptedRef.current = false;

    if (!config) return undefined;

    fetch(
      connectionIdRef.current
        ? `${API}/settings?institution_id=${encodeURIComponent(connectionIdRef.current)}`
        : `${API}/settings`
    )
      .then(r => r.json())
      .then(() => {
        if (cancelled) {
          return;
        }
        // This add/reauth flow deliberately starts replacement inputs blank even when
        // the settings response includes saved values. InstitutionSettingsModal owns
        // the masked saved-value display and user-controlled reveal behavior.
        setCredentials(buildCredentialValues(config.fields));
      })
      .catch(() => {});

    return () => {
      cancelled = true;
      closedRef.current = true;
      clearSyncProgressTimeout();
      if (successCloseTimeoutRef.current) clearTimeout(successCloseTimeoutRef.current);
      if (interruptedCloseTimeoutRef.current) clearTimeout(interruptedCloseTimeoutRef.current);
      cancelMoomooOAuthKeepalive();
      rollbackCredentialReplacementKeepalive();
    };
  }, [
    config,
    cancelMoomooOAuthKeepalive,
    rollbackCredentialReplacementKeepalive,
    setCredentials,
  ]);

  useEffect(() => {
    if (institution.provider && isNewInstitution) {
      pendingAddAttemptIdRef.current = markPendingInstitutionAdd(institution.provider);
    }
  }, [institution.provider, isNewInstitution]);

  if (!config) return null;

  const clearIncompleteNewInstitutionState = () => {
    if (!institution.provider || !isNewInstitution || completedRef.current) return;
    startIncompleteInstitutionAddCleanup(institution.provider, {
      attemptId: pendingAddAttemptIdRef.current,
    });
  };

  const handleModalClose = () => {
    if (!canCloseModal) return;
    if (step !== 'error' && isNewInstitution) {
      interruptedRef.current = true;
      clearSyncProgressTimeout();
      cancelMoomooOAuthKeepalive();
      clearIncompleteNewInstitutionState();
      setSaving(false);
      setError('');
      setStep('interrupted');
      interruptedCloseTimeoutRef.current = setTimeout(() => {
        interruptedCloseTimeoutRef.current = null;
        if (closedRef.current) return;
        closedRef.current = true;
        onClose();
      }, SUCCESS_DISPLAY_MS);
      return;
    }
    closedRef.current = true;
    clearSyncProgressTimeout();
    cancelMoomooOAuthKeepalive();
    clearIncompleteNewInstitutionState();
    onClose();
  };

  const confirmSuccessfulAdd = async (data = {}) => {
    if (!isNewInstitution) return true;
    return confirmPendingInstitutionAdd(
      institution.provider,
      connectionIdRef.current,
      data?.sync_id || '',
      data?.attempt_id || '',
    );
  };

  const finishSuccessfulSync = () => {
    completedRef.current = true;
    clearPendingInstitutionAdd(institution.provider, pendingAddAttemptIdRef.current);
    setStep('success');
    successCloseTimeoutRef.current = setTimeout(() => {
      successCloseTimeoutRef.current = null;
      if (closedRef.current) return;
      closedRef.current = true;
      if (onSuccess) onSuccess();
      onClose();
    }, SUCCESS_DISPLAY_MS);
  };

  const showSyncFailure = (message, status = 'error') => {
    if (onResultUpdate) {
      onResultUpdate(institution?.provider, status, message);
    }
    setStep(isNewInstitution ? 'error' : 'form');
    setError(message);
  };

  const settleCredentialReplacement = async (action) => {
    const replacementId = pendingCredentialReplacementRef.current;
    if (!replacementId) return;
    const response = await fetch(
      `${API}/settings/credential-replacements/${encodeURIComponent(replacementId)}/${action}`,
      { method: 'POST' },
    );
    const data = await response.json();
    if (!response.ok || data.status !== 'ok') {
      throw new Error(
        data.message
          || data.detail
          || `Credential replacement could not be ${action === 'commit' ? 'committed' : 'rolled back'}.`,
      );
    }
    if (pendingCredentialReplacementRef.current === replacementId) {
      pendingCredentialReplacementRef.current = '';
    }
  };

  const rollbackCredentialReplacement = async (message) => {
    try {
      await settleCredentialReplacement('rollback');
      return message;
    } catch (restoreError) {
      return `${message} ${restoreError?.message || 'Previous credentials could not be restored.'}`;
    }
  };

  const handleSyncResponse = async (syncResp) => {
    const syncData = await syncResp.json();
    clearSyncProgressTimeout();
    if (closedRef.current) return;
    if (syncData.status === 'ok') {
      await settleCredentialReplacement('commit');
      const confirmed = await confirmSuccessfulAdd(syncData);
      if (!confirmed) {
        if (closedRef.current) return;
        showSyncFailure(
          `${institution.name} synced, but ${APP_BRAND_NAME} could not finalize the add. Please try again.`,
        );
        return;
      }
      finishSuccessfulSync();
    } else if (syncData.status === 'already_syncing') {
      const message = syncData.message || `${institution.name} sync is already in progress.`;
      if (isNewInstitution) {
        showSyncFailure(message, 'already_syncing');
      } else {
        await settleCredentialReplacement('rollback');
        setStep('syncing');
        if (onResultUpdate) {
          onResultUpdate(institution?.provider, 'already_syncing', message);
        }
        setError('');
      }
    } else {
      const message = syncData.message || 'Sync failed \u2014 please check your credentials.';
      const resolvedStatus = syncData.status === 'network_error' ? 'network_error' : 'error';
      showSyncFailure(
        await rollbackCredentialReplacement(message),
        resolvedStatus,
      );
    }
  };

  const handleMoomooCloudOAuth = async () => {
    setSaving(true);
    setError('');
    setMoomooOAuthAuthorizationUrl('');
    setStep('moomoo_oauth_preparing');
    const authorizationWindow = desktopShell ? null : window.open('about:blank', '_blank');
    if (authorizationWindow) authorizationWindow.opener = null;
    try {
      const startResponse = await fetch(`${API}/auth/moomoo/oauth/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          add_flow: isNewInstitution,
          institution_id: connectionIdRef.current || undefined,
        }),
      });
      const startData = await startResponse.json();
      if (!startResponse.ok || startData.status !== 'waiting') {
        throw new Error(startData.message || startData.detail || 'Could not start Moomoo authorization.');
      }
      const flowId = String(startData.flow_id || '');
      const syncId = String(startData.sync_id || '');
      const attemptId = String(startData.attempt_id || '');
      const authorizationUrl = String(startData.authorization_url || '');
      const browserAuthorizationUrl = desktopShell
        ? authorizationUrl
        : buildMoomooEnglishPassportUrl(authorizationUrl);
      if (!flowId || !syncId || !attemptId || !authorizationUrl || !browserAuthorizationUrl) {
        throw new Error('Moomoo authorization did not return a complete browser handoff.');
      }
      moomooOAuthFlowIdRef.current = flowId;
      setMoomooOAuthAuthorizationUrl(browserAuthorizationUrl);
      setStep('moomoo_oauth_waiting');
      if (authorizationWindow) {
        authorizationWindow.location.replace(browserAuthorizationUrl);
      } else if (desktopShell) {
        let captureLevel = 'redacted_rich';
        if (import.meta.env.DEV) {
          try {
            const diagnosticsResponse = await fetch(
              `${API}/settings/dev-diagnostics/capture-level`,
            );
            if (diagnosticsResponse.ok) {
              const diagnosticsData = await diagnosticsResponse.json();
              if (typeof diagnosticsData?.capture_level === 'string') {
                captureLevel = diagnosticsData.capture_level;
              }
            }
          } catch (_) {
            /* dev-only diagnostics — silent on transport errors */
          }
        }
        const browserResult = await launchDesktopVisibleAuth({
          provider: 'moomoo',
          authorizationUrl,
          syncId,
          attemptId,
          addFlow: isNewInstitution,
          institutionId: connectionIdRef.current || undefined,
          timeoutSeconds: Number(startData.expires_in || 600),
          captureLevel,
        });
        if (!browserResult?.attemptId || ['error', 'failed', 'cancelled'].includes(browserResult?.status)) {
          throw new Error(browserResult?.message || 'Could not open Moomoo authorization in the secure browser.');
        }
        if (browserResult.attemptId !== attemptId) {
          throw new Error('Moomoo authorization returned an unexpected browser attempt identity.');
        }
        moomooOAuthBrowserAttemptIdRef.current = browserResult.attemptId;
      }

      const expiresAt = Date.now() + (Number(startData.expires_in || 600) * 1000);
      let statusData = null;
      while (!closedRef.current && Date.now() < expiresAt) {
        const statusResponse = await fetch(
          `${API}/auth/moomoo/oauth/${encodeURIComponent(flowId)}/status`,
        );
        statusData = await statusResponse.json();
        if (!statusResponse.ok || statusData.status === 'error') {
          throw new Error(statusData.message || statusData.detail || 'Moomoo authorization did not finish.');
        }
        if (statusData.status === 'authorized') break;
        if (desktopShell && moomooOAuthBrowserAttemptIdRef.current) {
          const browserStatus = await getDesktopVisibleAuthStatus({
            provider: 'moomoo',
            attemptId: moomooOAuthBrowserAttemptIdRef.current,
          });
          if (['error', 'failed', 'cancelled'].includes(browserStatus?.status)) {
            throw new Error(browserStatus?.message || 'Moomoo authorization window closed before authorization finished.');
          }
        }
        await new Promise((resolve) => setTimeout(resolve, 750));
      }
      if (closedRef.current) return;
      if (statusData?.status !== 'authorized') {
        throw new Error('Timed out waiting for Moomoo browser authorization.');
      }
      if (authorizationWindow && !authorizationWindow.closed) authorizationWindow.close();
      moomooOAuthBrowserAttemptIdRef.current = '';

      const completeResponse = await fetch(
        `${API}/auth/moomoo/oauth/${encodeURIComponent(flowId)}/complete`,
        { method: 'POST' },
      );
      const completeData = await completeResponse.json();
      if (!completeResponse.ok || completeData.status !== 'ok') {
        throw new Error(completeData.message || completeData.detail || 'Could not save Moomoo authorization.');
      }
      moomooOAuthFlowIdRef.current = '';
      connectionIdRef.current = Number(completeData.institution_id || 0) || null;
      setStep('syncing');
      const syncResp = await fetchWithTimeout(
        `${API}${syncEndpoint}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            add_flow: isNewInstitution,
            sync_id: completeData.sync_id || syncId,
            attempt_id: completeData.attempt_id || attemptId,
            institution_id: connectionIdRef.current || undefined,
          }),
        },
        syncRequestTimeoutMs,
      );
      await handleSyncResponse(syncResp);
    } catch (err) {
      if (authorizationWindow && !authorizationWindow.closed) authorizationWindow.close();
      cancelMoomooOAuthKeepalive();
      clearSyncProgressTimeout();
      if (closedRef.current || interruptedRef.current) return;
      showSyncFailure(err?.message || 'Moomoo authorization failed.');
    } finally {
      if (!closedRef.current) setSaving(false);
    }
  };

  const handleSaveAndSync = async () => {
    if (useMoomooCloudOAuth) {
      await handleMoomooCloudOAuth();
      return;
    }
    const emptyFields = config.fields.filter(f => !credentials[f.key]?.trim());
    if (emptyFields.length > 0) {
      setError(`${emptyFields[0].label} cannot be empty.`);
      return;
    }
    setSaving(true);
    setError('');
    try {
      const settingsResp = await fetch(`${API}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...credentials,
          add_flow: isNewInstitution,
          institution_id: connectionIdRef.current || undefined,
        }),
      });
      const settingsData = await settingsResp.json();
      if (!settingsResp.ok || settingsData.status !== 'ok') {
        throw new Error(settingsData.message || 'Could not save institution credentials.');
      }
      if (Number(settingsData.institution_id || 0) > 0) {
        connectionIdRef.current = Number(settingsData.institution_id);
      }
      pendingCredentialReplacementRef.current = String(
        settingsData.credential_replacement_id || '',
      );
      if (closedRef.current) {
        rollbackCredentialReplacementKeepalive();
        return;
      }
      clearSyncProgressTimeout();
      syncProgressTimeoutRef.current = setTimeout(() => {
        if (closedRef.current) return;
        syncProgressTimeoutRef.current = null;
        setStep('syncing');
      }, API_SYNC_PROGRESS_HINT_DELAY_MS);
      const syncResp = await fetchWithTimeout(
        `${API}${syncEndpoint}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            add_flow: isNewInstitution,
            institution_id: connectionIdRef.current || undefined,
          }),
        },
        syncRequestTimeoutMs,
      );
      if (closedRef.current) return;
      if (!syncResp) {
        await settleCredentialReplacement('rollback');
        return;
      }
      await handleSyncResponse(syncResp);
    } catch (err) {
      clearSyncProgressTimeout();
      if (closedRef.current || interruptedRef.current) return;
      const message = err?.message || 'Failed to save or sync.';
      showSyncFailure(await rollbackCredentialReplacement(message));
    } finally {
      if (!closedRef.current) {
        setSaving(false);
      }
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && canSubmitCredentials) handleSaveAndSync();
    if (e.key === 'Escape' && canCloseModal) handleModalClose();
  };

  return (
    <div className="modal-overlay">
      <div className="modal-content" onClick={(e) => e.stopPropagation()} onKeyDown={handleKeyDown}>
        <div className="modal-header">
          <InstitutionLogo name={institution.name} size={36} />
          <h3 className="modal-title">{institution.name}</h3>
          {canCloseModal && step !== 'error' && (
            <button className="modal-close" onClick={handleModalClose}>&#10005;</button>
          )}
        </div>

        {step === 'form' && (
          <div className="modal-body">
            <div className="settings-instructions">
              {useMoomooCloudOAuth ? (
                <>
                  <p className="modal-desc">{renderBrandText('Connect Moomoo through its secure, read-only authorization page. BreakTwenty never receives your Moomoo password.', 'moomoo-oauth-description')}</p>
                  <h4>What you need to approve:</h4>
                  <ol>
                    <li>{renderBrandMarkedText("Click **Continue to Moomoo** and sign in on Moomoo's own page", 'moomoo-oauth-continue')}</li>
                    <li>{renderBrandMarkedText('Under **Authorized Accounts**, select every Moomoo account you want to connect. The dropdown supports multiple account selections', 'moomoo-oauth-account')}</li>
                    <li>Check <strong className="moomoo-required-scope">Accounts &amp; Orders</strong>. {renderBrandText('This is required to import the account, balances, holdings, and transaction history into BreakTwenty', 'moomoo-oauth-required-scope')}</li>
                    <li>{renderBrandMarkedText('Leave **Trade Execution** unchecked. BreakTwenty never places trades', 'moomoo-oauth-trading')}</li>
                    <li>{renderBrandMarkedText("Accept Moomoo's agreement and click **Confirm**. BreakTwenty continues automatically and closes the secure browser when finished", 'moomoo-oauth-confirm')}</li>
                  </ol>
                </>
              ) : config.description && <p className="modal-desc">{renderBrandText(config.description, 'api-auth-description')}</p>}
              {!useMoomooCloudOAuth && config.instructionsTitle && <h4>{renderBrandText(config.instructionsTitle, 'api-auth-instructions-title')}</h4>}
              {!useMoomooCloudOAuth && config.instructionsSteps && (
                <ol>
                  {config.instructionsSteps.map((s, i) => (
                    <li key={i}>
                      {s.link ? (
                        <>
                          {renderBrandMarkedText(s.text, `api-auth-step-${i}`)}
                          <a href={s.link} target="_blank" rel="noreferrer" className="modal-inline-link">
                            {renderBrandMarkedText(s.linkText, `api-auth-step-${i}-link`)}
                          </a>
                        </>
                      ) : (
                        renderBrandMarkedText(s.text, `api-auth-step-${i}`)
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </div>
            {!useMoomooCloudOAuth && config.fields.map(field => {
              const isSecret = field.secret !== false;
              const isVisible = !isSecret || fieldVisibility[field.key] || false;
              return (
                <div key={field.key} className="settings-field">
                  <label>{field.label}</label>
                  <div className="modal-field-shell">
                    {field.multiline ? (
                      <textarea
                        className={`modal-mono-field modal-input-with-toggle ${isSecret ? '' : 'modal-input-no-toggle'}`.trim()}
                        value={credentials[field.key] || ''}
                        onChange={(e) => setCredentials((prev) => ({ ...prev, [field.key]: e.target.value }))}
                        rows={6}
                        style={{ WebkitTextSecurity: isVisible ? 'none' : 'disc' }}
                      />
                    ) : (
                      <input
                        className={`modal-input-with-toggle ${isSecret ? '' : 'modal-input-no-toggle'}`.trim()}
                        type={isVisible ? (field.type === 'number' ? 'number' : 'text') : 'password'}
                        value={credentials[field.key] || ''}
                        onChange={(e) => setCredentials((prev) => ({ ...prev, [field.key]: e.target.value }))}
                        autoFocus={config.fields.indexOf(field) === 0}
                      />
                    )}
                    {isSecret && (
                      <button
                        type="button"
                        onClick={() => setFieldVisibility(prev => ({ ...prev, [field.key]: !isVisible }))}
                        className="modal-secret-toggle"
                        data-tooltip={isVisible ? 'Hide' : 'Show'}
                      >
                        {isVisible ? <MdVisibility size={18} /> : <MdVisibilityOff size={18} />}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
            {error && <p className="modal-error-block">{renderBrandText(error, 'api-auth-error')}</p>}
            <button className="btn-primary modal-btn app-control-root" onClick={handleSaveAndSync} disabled={saving}>
              <span className="app-control-label">{saving
                ? (useMoomooCloudOAuth ? 'Opening Moomoo...' : 'Saving & Syncing...')
                : (useMoomooCloudOAuth ? 'Continue to Moomoo' : 'Save & Sync')}</span>
            </button>
            <p className="modal-privacy">
              {useMoomooCloudOAuth
                ? renderBrandText('Your OAuth refresh token is encrypted in BreakTwenty and is used only for read-only syncing.', 'moomoo-oauth-privacy')
                : config.privacyText || (config.fields.some(field => field.secret !== false)
                ? 'Your API keys are stored locally, are not sent to the developer, and are used only for syncing.'
                : 'Your connection settings are stored locally, are not sent to the developer, and are used only for syncing.')}
            </p>
          </div>
        )}

        {step === 'moomoo_oauth_preparing' && (
          <InstitutionSyncProgress
            title="Preparing Moomoo authorization..."
            subtitle={renderBrandText('BreakTwenty is creating a secure, one-time browser authorization request.', 'moomoo-oauth-preparing')}
          />
        )}

        {step === 'moomoo_oauth_waiting' && (
          <InstitutionSyncProgress
            title="Authorize Moomoo in the secure browser..."
            subtitle={(
              <>
                {renderBrandMarkedText("Select every account you want to sync, check **Accounts & Orders**, leave **Trade Execution** unchecked, accept Moomoo's agreement, and click **Confirm**. BreakTwenty will continue automatically.", 'moomoo-oauth-waiting')}
                {!desktopShell && moomooOAuthAuthorizationUrl && (
                  <> If the page did not open, <a href={moomooOAuthAuthorizationUrl} target="_blank" rel="noreferrer" className="modal-inline-link">open Moomoo authorization</a>.</>
                )}
              </>
            )}
          />
        )}

        {step === 'syncing' && (
          <InstitutionSyncProgress
            title="Connecting your accounts..."
            subtitle={renderBrandText(institution.provider === 'moomoo'
              ? `${APP_BRAND_NAME} is now pulling Moomoo accounts, holdings, and current balances. Transaction history will continue in the background.`
              : `If your credentials are valid, ${APP_BRAND_NAME} is now pulling accounts, holdings, and current balances. Transaction history will continue in the background.`, 'api-auth-sync-progress')}
          />
        )}

        {step === 'success' && (
          <div className="modal-body modal-center">
            <div className="modal-success-icon">✓</div>
            <p className="modal-2fa-text">
              {getInstitutionSuccessMessage(institution, isNewInstitution ? 'add' : 'sync')}
            </p>
          </div>
        )}

        {step === 'interrupted' && (
          <div className="modal-body modal-center">
            <div className="modal-error-mark">✕</div>
            <p className="modal-2fa-text">
              {getInstitutionInterruptedMessage(institution, isNewInstitution ? 'add' : 'sync')}
            </p>
          </div>
        )}

        {step === 'error' && (
          <div className="modal-body modal-center">
            <div className="modal-error-mark">✕</div>
            <p className="modal-2fa-text">
              {renderBrandText(error || 'Something went wrong', 'api-auth-terminal-error')}
            </p>
            <button className="btn-primary modal-btn app-control-root" onClick={handleModalClose}>
              <span className="app-control-label">Close</span>
            </button>
          </div>
        )}

      </div>
    </div>
  );
}

export default ApiAuthModal;
