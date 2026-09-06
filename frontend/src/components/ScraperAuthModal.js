import React, { useEffect, useRef, useState } from 'react';
import { MdSync } from 'react-icons/md';
import { renderBrandText } from './BrandName';
import InstitutionLogo from './InstitutionLogo';
import InstitutionSyncProgress from './InstitutionSyncProgress';
import { API } from '../config';
import { APP_BRAND_NAME } from '../constants/brand';
import {
  getBackgroundSyncEndpoint,
  getDesktopVisibleAuthConfig,
  getInstitutionInterruptedMessage,
  getInstitutionSuccessMessage,
  getScraperAuthConfig,
  getUserInitiatedSyncEndpoint,
  isDesktopVisibleAuthProvider,
  SCRAPER_ENDPOINTS as PROVIDER_ENDPOINTS,
  SCRAPER_FIELD_LABELS as PROVIDER_LABELS,
} from '../constants/providers';
import {
  ADD_INSTITUTION_SYNC_REQUEST_TIMEOUT_MS,
  PUSH_APPROVAL_TIMEOUT_MS,
  USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS,
  fetchWithTimeout,
} from '../utils/syncRequests';
import {
  clearPendingInstitutionAdd,
  hasInterruptedInstitutionAdd,
  confirmPendingInstitutionAdd,
  markInterruptedInstitutionAdd,
  markPendingInstitutionAdd,
  startIncompleteInstitutionAddCleanup,
} from '../utils/incompleteInstitutionAdds';
import {
  cancelDesktopVisibleAuth,
  getDesktopVisibleAuthStatus,
  launchDesktopVisibleAuth,
} from '../utils/desktopBridge';
import {
  DEFAULT_USER_TIMEZONE,
  getBrowserTimezone,
} from '../utils/timezone';

const AUTH_ATTEMPT_CLOSE_DELAY_MS = 1500;
const SUCCESS_DISPLAY_MS = 1500;
const PUSH_POLL_INITIAL_DELAY_MS = 3000;
const PUSH_POLL_INTERVAL_MS = 2000;
const PUSH_POST_APPROVAL_SYNC_HINT_DELAY_MS = 5000;
const SMS_POST_VERIFICATION_SYNC_HINT_DELAY_MS = 1200;
const VISIBLE_AUTH_ATTEMPT_CLEANUP_TIMEOUT_MS = 10000;

const getSkipAutoLoginOnceKey = (provider) => `breaktwenty_skip_autologin_once_${provider}`;
const SECURE_BROWSER_CLOSED_MESSAGE = 'Login window was closed before secure login finished. Start sync again and keep the bank browser window open until the app says it is done.';

const getDesktopVisibleAuthTimezone = (savedTimezone, timezoneConfigured) => {
  const normalizedSavedTimezone = String(savedTimezone || '').trim();
  if (
    normalizedSavedTimezone
    && (timezoneConfigured || normalizedSavedTimezone !== DEFAULT_USER_TIMEZONE)
  ) {
    return normalizedSavedTimezone;
  }
  return getBrowserTimezone() || normalizedSavedTimezone || DEFAULT_USER_TIMEZONE;
};

const normalizeSecureBrowserErrorMessage = (msg) => {
  const normalizedText = String(msg || '').trim();
  const lowered = normalizedText.toLowerCase();
  if (
    lowered.includes('targetclosederror') ||
    lowered.includes('target page') ||
    lowered.includes('target closed') ||
    lowered.includes('has been closed')
  ) {
    return SECURE_BROWSER_CLOSED_MESSAGE;
  }
  return normalizedText;
};

function ScraperAuthModal({
  institution,
  userTimezone = DEFAULT_USER_TIMEZONE,
  userTimezoneConfigured = true,
  onClose,
  onSuccess,
  onDataChange,
  onResultUpdate,
  skipAutoLogin = false,
}) {
  const [step, setStep] = useState('credentials');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [smsCode, setSmsCode] = useState('');
  const [verificationDialog, setVerificationDialog] = useState(null);
  const [message, setMessage] = useState('');
  const [bridgeMessage, setBridgeMessage] = useState('');
  const [bridgeSubMessage, setBridgeSubMessage] = useState('');
  const [pushPhase, setPushPhase] = useState('approval');
  const [loading, setLoading] = useState(false);
  const [isSilentSyncLocked, setIsSilentSyncLocked] = useState(false);
  const pollTimeoutRef = useRef(null);
  const bridgePollTimeoutRef = useRef(null);
  const bridgeSyncRetryTimeoutRef = useRef(null);
  const verificationTimeoutRef = useRef(null);
  const pushSyncHintTimeoutRef = useRef(null);
  const smsSyncHintTimeoutRef = useRef(null);
  const errorReturnTimeoutRef = useRef(null);
  const attemptCloseTimeoutRef = useRef(null);
  const successCloseTimeoutRef = useRef(null);
  const interruptedCloseTimeoutRef = useRef(null);
  const pushApprovalDeadlineRef = useRef(null);
  const terminateVerificationAttemptRef = useRef(null);
  const handleLoginRef = useRef(null);
  const launchDesktopVisibleAuthFlowRef = useRef(null);
  const showInterruptedResultRef = useRef(null);
  const closedRef = useRef(false);
  const autoLoginStartedRef = useRef(false);
  const credentialsEditedRef = useRef(false);
  const usingSavedCredentialsRef = useRef(false);
  const terminatingRef = useRef(false);
  const completedRef = useRef(false);
  const connectionIdRef = useRef(Number(institution?.id || 0) || null);
  const pushPollingFinalizedRef = useRef(false);
  const pushPollInFlightRef = useRef(false);
  const desktopVisibleAuthActiveRef = useRef({});
  const desktopVisibleAuthAttemptIdRef = useRef('');
  const desktopVisibleAuthSyncIdRef = useRef('');
  const pendingAddAttemptIdRef = useRef('');

  const endpoints = PROVIDER_ENDPOINTS[institution.provider];
  const labels = PROVIDER_LABELS[institution.provider] || { username: 'Username', password: 'Password' };
  const authConfig = getScraperAuthConfig(institution.provider);
  const isNewInstitution = Boolean(institution.isNew);
  const desktopVisibleAuthConfig = getDesktopVisibleAuthConfig(institution.provider);
  const desktopVisibleAuthEnabled = isDesktopVisibleAuthProvider(institution.provider);
  const supportsDesktopVisibleAuthAdd = desktopVisibleAuthConfig?.addFlow === true;
  const supportsDesktopVisibleAuthManual = desktopVisibleAuthConfig?.manualFlow === true;
  const isDesktopVisibleAuthFlow =
    desktopVisibleAuthEnabled &&
    (
      (isNewInstitution && supportsDesktopVisibleAuthAdd) ||
      (!isNewInstitution && supportsDesktopVisibleAuthManual)
    );
  const requireCredentialsBeforeDesktopVisibleAuth =
    isDesktopVisibleAuthFlow && desktopVisibleAuthConfig?.requireCredentialsBeforeLaunch === true;
  const syncEndpoint = isDesktopVisibleAuthFlow
    ? getUserInitiatedSyncEndpoint(institution.provider)
    : getBackgroundSyncEndpoint(institution.provider);
  const isVerificationDialogBlocking =
    verificationDialog?.status === 'verifying' || verificationDialog?.status === 'success';

  const showVerificationDialog = (status, dialogMessage, dialogSubMessage = '', closeAction = 'dismiss') => {
    setVerificationDialog({
      status,
      message: dialogMessage,
      subMessage: dialogSubMessage,
      closeAction,
    });
  };

  const dismissVerificationDialog = () => {
    const closeAction = verificationDialog?.closeAction;
    setVerificationDialog(null);
    if (closeAction === 'close_modal') {
      void handleModalClose({ reportInterrupted: false });
    }
  };

  const notifySyncSuccess = () => {
    completedRef.current = true;
    clearPendingInstitutionAdd(institution?.provider, pendingAddAttemptIdRef.current);
    if (onResultUpdate && institution?.provider) {
      onResultUpdate(institution.provider, 'ok', null);
    }
  };

  const getInstitutionContextPayload = () => ({
    institution_id: connectionIdRef.current || undefined,
    add_flow: isNewInstitution,
  });

  const confirmSuccessfulAdd = async (data = {}) => {
    if (!isNewInstitution) return true;
    return confirmPendingInstitutionAdd(
      institution.provider,
      connectionIdRef.current,
      data?.sync_id || desktopVisibleAuthSyncIdRef.current || '',
    );
  };

  const finishOkResult = async (data, { dialog = false } = {}) => {
    if (closedRef.current) return;
    const confirmed = await confirmSuccessfulAdd(data);
    if (closedRef.current) return;
    if (!confirmed) {
      if (dialog) setVerificationDialog(null);
      setStep('error');
      setMessage(`${institution.name} synced, but ${APP_BRAND_NAME} could not finalize the add. Please try again.`);
      return;
    }
    notifySyncSuccess();
    if (onDataChange) onDataChange();
    setVerificationDialog(null);
    setStep('success');
    successCloseTimeoutRef.current = setTimeout(() => {
      successCloseTimeoutRef.current = null;
      if (closedRef.current) return;
      closedRef.current = true;
      onSuccess();
      onClose();
    }, SUCCESS_DISPLAY_MS);
  };

  const authFlowSyncTimeoutMs = isNewInstitution
    ? ADD_INSTITUTION_SYNC_REQUEST_TIMEOUT_MS
    : USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS;
  const clearPollTimeout = () => {
    if (pollTimeoutRef.current) {
      clearTimeout(pollTimeoutRef.current);
      pollTimeoutRef.current = null;
    }
  };
  const clearBridgePollTimeout = () => {
    if (bridgePollTimeoutRef.current) {
      clearTimeout(bridgePollTimeoutRef.current);
      bridgePollTimeoutRef.current = null;
    }
  };
  const clearBridgeSyncRetryTimeout = () => {
    if (bridgeSyncRetryTimeoutRef.current) {
      clearTimeout(bridgeSyncRetryTimeoutRef.current);
      bridgeSyncRetryTimeoutRef.current = null;
    }
  };
  const clearVerificationTimeout = () => {
    if (verificationTimeoutRef.current) {
      clearTimeout(verificationTimeoutRef.current);
      verificationTimeoutRef.current = null;
    }
  };
  const clearPushSyncHintTimeout = () => {
    if (pushSyncHintTimeoutRef.current) {
      clearTimeout(pushSyncHintTimeoutRef.current);
      pushSyncHintTimeoutRef.current = null;
    }
  };
  const clearSmsSyncHintTimeout = () => {
    if (smsSyncHintTimeoutRef.current) {
      clearTimeout(smsSyncHintTimeoutRef.current);
      smsSyncHintTimeoutRef.current = null;
    }
  };
  const schedulePushSyncHint = () => {
    clearPushSyncHintTimeout();
    pushSyncHintTimeoutRef.current = setTimeout(() => {
      pushSyncHintTimeoutRef.current = null;
      if (closedRef.current || terminatingRef.current || pushPollingFinalizedRef.current) return;
      setPushPhase('syncing');
    }, PUSH_POST_APPROVAL_SYNC_HINT_DELAY_MS);
  };
  const scheduleSmsSyncHint = () => {
    clearSmsSyncHintTimeout();
    smsSyncHintTimeoutRef.current = setTimeout(() => {
      smsSyncHintTimeoutRef.current = null;
      if (closedRef.current || terminatingRef.current) return;
      showVerificationDialog(
        'verifying',
        'Connecting your accounts...',
        `If code was successfully verified, ${APP_BRAND_NAME} is now pulling accounts, holdings, and current balances. Transaction history will continue in the background.`,
      );
    }, SMS_POST_VERIFICATION_SYNC_HINT_DELAY_MS);
  };
  const autoLoginBlockKey = authConfig.autoLoginBlockKey || '';
  const clearProviderAutoLoginBlock = () => {
    if (autoLoginBlockKey) {
      sessionStorage.removeItem(autoLoginBlockKey);
    }
  };
  const markProviderAutoLoginBlocked = () => {
    if (autoLoginBlockKey) {
      sessionStorage.setItem(autoLoginBlockKey, '1');
    }
  };
  useEffect(() => {
    const provider = String(institution.provider || '').trim();
    const addFlow = isNewInstitution;
    const discardAttempt = (attemptId) => {
      const normalizedAttemptId = String(attemptId || '').trim();
      if (!provider || !normalizedAttemptId) return;
      void fetchWithTimeout(
        `${API}/sync/visible-auth-attempt-cleanup`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            provider,
            attempt_id: normalizedAttemptId,
            add_flow: addFlow,
          }),
        },
        VISIBLE_AUTH_ATTEMPT_CLEANUP_TIMEOUT_MS,
      ).catch(() => {});
    };
    closedRef.current = false;
    terminatingRef.current = false;
    completedRef.current = false;
    credentialsEditedRef.current = false;
    pushPollingFinalizedRef.current = false;
    pushPollInFlightRef.current = false;
    desktopVisibleAuthActiveRef.current = {};
    desktopVisibleAuthAttemptIdRef.current = '';
    pushApprovalDeadlineRef.current = null;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) {
        setPushPhase('approval');
      }
    });
    return () => {
      const attemptId = String(desktopVisibleAuthAttemptIdRef.current || '').trim();
      const attemptWasActive = Boolean(desktopVisibleAuthActiveRef.current[provider]);
      cancelled = true;
      closedRef.current = true;
      clearPollTimeout();
      clearBridgePollTimeout();
      clearBridgeSyncRetryTimeout();
      clearVerificationTimeout();
      clearPushSyncHintTimeout();
      clearSmsSyncHintTimeout();
      if (errorReturnTimeoutRef.current) clearTimeout(errorReturnTimeoutRef.current);
      if (attemptCloseTimeoutRef.current) clearTimeout(attemptCloseTimeoutRef.current);
      if (successCloseTimeoutRef.current) clearTimeout(successCloseTimeoutRef.current);
      if (interruptedCloseTimeoutRef.current) clearTimeout(interruptedCloseTimeoutRef.current);
      errorReturnTimeoutRef.current = null;
      attemptCloseTimeoutRef.current = null;
      successCloseTimeoutRef.current = null;
      interruptedCloseTimeoutRef.current = null;
      if (isDesktopVisibleAuthFlow && attemptId) {
        if (attemptWasActive) {
          void cancelDesktopVisibleAuth({ provider, attemptId }).catch(() => {});
        }
        discardAttempt(attemptId);
      }
      desktopVisibleAuthActiveRef.current = {};
      desktopVisibleAuthAttemptIdRef.current = '';
      desktopVisibleAuthSyncIdRef.current = '';
    };
  // Mount-scoped ownership: a provider change replaces the modal rather than
  // transferring an in-flight desktop attempt between institutions.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (institution.provider && isNewInstitution) {
      pendingAddAttemptIdRef.current = markPendingInstitutionAdd(institution.provider);
    }
  }, [institution.provider, isNewInstitution]);
  // Load saved credentials on mount
  const [hasSavedCreds, setHasSavedCreds] = useState(false);
  useEffect(() => {
    if (!institution.provider || isDesktopVisibleAuthFlow || isNewInstitution) return;
    fetch(`${API}/credentials/${institution.provider}/status?institution_id=${encodeURIComponent(institution.id)}`)
      .then(r => r.json())
      .then(data => {
        if (closedRef.current || credentialsEditedRef.current || autoLoginStartedRef.current) return;
        if (data.status === "ok" && data.has_saved_credentials === true) {
          const providerAutoLoginBlocked =
            autoLoginBlockKey && sessionStorage.getItem(autoLoginBlockKey) === '1';
          const skipProviderAutoLoginOnce =
            sessionStorage.getItem(getSkipAutoLoginOnceKey(institution.provider)) === '1';
          if (providerAutoLoginBlocked) {
            setHasSavedCreds(false);
            setStep('credentials');
          } else if (skipProviderAutoLoginOnce) {
            sessionStorage.removeItem(getSkipAutoLoginOnceKey(institution.provider));
          } else if (!skipAutoLogin && !autoLoginStartedRef.current) {
            setHasSavedCreds(true);
            setStep('auto_login');
          }
        }
      }).catch(() => {});
  }, [institution.id, institution.provider, isDesktopVisibleAuthFlow, isNewInstitution, autoLoginBlockKey, skipAutoLogin]);
  const showErrorAndReturn = (msg, { returnToCredentials = true } = {}) => {
    clearPollTimeout();
    clearPushSyncHintTimeout();
    clearSmsSyncHintTimeout();
    if (closedRef.current) return;
    setBridgeMessage('');
    setBridgeSubMessage('');
    setStep("error");
    setMessage(msg);
    if (returnToCredentials) {
      if (errorReturnTimeoutRef.current) clearTimeout(errorReturnTimeoutRef.current);
      errorReturnTimeoutRef.current = setTimeout(() => {
        errorReturnTimeoutRef.current = null;
        if (closedRef.current) return;
        setStep("credentials");
        setMessage("");
      }, 2500);
    }
  };
  const terminateVerificationAttempt = async (msg) => {
    clearPollTimeout();
    clearVerificationTimeout();
    clearPushSyncHintTimeout();
    clearSmsSyncHintTimeout();
    if (closedRef.current) return;
    terminatingRef.current = true;
    setSmsCode('');
    setStep('error');
    setMessage(msg || "Verification failed. Please try again.");
    if (autoLoginBlockKey) {
      markProviderAutoLoginBlocked();
    } else {
      sessionStorage.setItem(getSkipAutoLoginOnceKey(institution.provider), '1');
    }
    if (closedRef.current) return;
    if (attemptCloseTimeoutRef.current) clearTimeout(attemptCloseTimeoutRef.current);
    attemptCloseTimeoutRef.current = setTimeout(() => {
      attemptCloseTimeoutRef.current = null;
      if (!closedRef.current) {
        void handleModalClose();
      }
    }, AUTH_ATTEMPT_CLOSE_DELAY_MS);
  };
  const persistScraperCredentials = async ({ preservePendingSession = false } = {}) => {
    if (!institution.provider) return;
    await fetch(`${API}/credentials/${institution.provider}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        password,
        preserve_pending_session: preservePendingSession,
        institution_id: connectionIdRef.current || undefined,
      }),
    }).catch(() => {});
  };
  const hasMissingPendingSessionMessage = (msg) =>
    typeof msg === 'string' && msg.toLowerCase().includes('pending auth session');
  const clearIncompleteNewInstitutionState = () => {
    if (!institution.provider || !isNewInstitution || completedRef.current) return;
    startIncompleteInstitutionAddCleanup(institution.provider, {
      attemptId: pendingAddAttemptIdRef.current,
      clearOnFirstOk: true,
    });
  };
  const discardDesktopVisibleAuthAttempt = (attemptId = desktopVisibleAuthAttemptIdRef.current) => {
    const provider = String(institution.provider || '').trim();
    const normalizedAttemptId = String(attemptId || '').trim();
    if (!provider || !normalizedAttemptId) {
      return Promise.resolve();
    }
    return fetchWithTimeout(
      `${API}/sync/visible-auth-attempt-cleanup`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider,
          attempt_id: normalizedAttemptId,
          add_flow: isNewInstitution,
        }),
      },
      VISIBLE_AUTH_ATTEMPT_CLEANUP_TIMEOUT_MS,
    ).catch(() => {});
  };
  const logDesktopVisibleAuthTerminalEvent = ({
    result,
    message,
    details = {},
    attemptId = desktopVisibleAuthAttemptIdRef.current,
    syncId = desktopVisibleAuthSyncIdRef.current,
  } = {}) => {
    const provider = String(institution.provider || '').trim();
    const normalizedAttemptId = String(attemptId || '').trim();
    if (!provider || !normalizedAttemptId) {
      return Promise.resolve();
    }
    return fetchWithTimeout(
      `${API}/settings/support-logs/client-event`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source: 'desktop_visible_auth',
          provider,
          sync_id: syncId || undefined,
          add_flow: isNewInstitution,
          attempt_id: normalizedAttemptId,
          stage: 'sync result',
          result,
          level: result === 'cancelled' ? 'warning' : 'error',
          message,
          last_output: message,
          details,
        }),
      },
      VISIBLE_AUTH_ATTEMPT_CLEANUP_TIMEOUT_MS,
    ).catch(() => {});
  };
  const setDesktopVisibleAuthActive = (provider, active) => {
    desktopVisibleAuthActiveRef.current = {
      ...desktopVisibleAuthActiveRef.current,
      [provider]: active,
    };
  };
  const showDesktopVisibleAuthError = (msg, config = desktopVisibleAuthConfig) => {
    if (closedRef.current) return;
    const safeMessage =
      normalizeSecureBrowserErrorMessage(msg) || `${config?.displayName || institution.name} secure login failed.`;
    if (safeMessage === SECURE_BROWSER_CLOSED_MESSAGE) {
      showInterruptedResult();
      return;
    }
    const attemptId = desktopVisibleAuthAttemptIdRef.current;
    void discardDesktopVisibleAuthAttempt(attemptId);
    clearBridgePollTimeout();
    clearBridgeSyncRetryTimeout();
    setDesktopVisibleAuthActive(institution.provider, false);
    desktopVisibleAuthAttemptIdRef.current = '';
    desktopVisibleAuthSyncIdRef.current = '';
    if (isDesktopVisibleAuthFlow && institution.provider) {
      sessionStorage.setItem(getSkipAutoLoginOnceKey(institution.provider), '1');
    }
    autoLoginStartedRef.current = true;
    setIsSilentSyncLocked(false);
    setBridgeMessage('');
    setBridgeSubMessage('');
    setStep('error');
    setMessage(safeMessage);
    if (onResultUpdate) {
      onResultUpdate(institution?.provider, 'auth_required', safeMessage || `${config?.displayName || institution.name} secure login failed.`);
    }
  };
  const retryDesktopVisibleAuthSync = async (attempt = 0, config = desktopVisibleAuthConfig) => {
    if (closedRef.current || terminatingRef.current) return;

    if (attempt === 0) setIsSilentSyncLocked(true);
    setStep('desktop_visible_auth_finishing');

    try {
      if (!syncEndpoint) {
        throw new Error(`${config?.displayName || institution.name} sync endpoint is not configured.`);
      }
      const resp = await fetchWithTimeout(
        `${API}${syncEndpoint}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sync_id: desktopVisibleAuthSyncIdRef.current || undefined,
            attempt_id: desktopVisibleAuthAttemptIdRef.current || undefined,
            ...getInstitutionContextPayload(),
          }),
        },
        authFlowSyncTimeoutMs,
      );
      const data = await resp.json();
      if (Number(data?.institution_id || 0) > 0) {
        connectionIdRef.current = Number(data.institution_id);
      }
      if (closedRef.current || terminatingRef.current) return;
      if (data.sync_id) {
        desktopVisibleAuthSyncIdRef.current = data.sync_id;
      }

      if (data.status === 'ok') {
        setDesktopVisibleAuthActive(institution.provider, false);
        setBridgeMessage('');
        setBridgeSubMessage('');
        setIsSilentSyncLocked(false);
        await finishOkResult(data);
        return;
      }

      if (data.status === 'already_syncing' && attempt < (config?.syncRetryAttempts ?? 30)) {
        bridgeSyncRetryTimeoutRef.current = setTimeout(() => {
          void retryDesktopVisibleAuthSync(attempt + 1, config);
        }, config?.syncRetryMs ?? 2000);
        return;
      }

      showDesktopVisibleAuthError(data.message || `${config?.displayName || institution.name} sync did not recover after secure login.`, config);
    } catch (err) {
      showDesktopVisibleAuthError(err.message || `${config?.displayName || institution.name} sync failed after secure login.`, config);
    }
  };
  const pollDesktopVisibleAuthStatus = async (config = desktopVisibleAuthConfig) => {
    if (closedRef.current || terminatingRef.current) return;

    try {
      const data = await getDesktopVisibleAuthStatus({
        provider: institution.provider,
        attemptId: desktopVisibleAuthAttemptIdRef.current || undefined,
      });
      if (closedRef.current || terminatingRef.current) return;

      if (data.status === 'running') {
        setDesktopVisibleAuthActive(institution.provider, true);
        setStep(config?.waitStep || 'desktop_visible_auth_wait');
        setBridgeMessage(data.message || 'Secure browser is open...');
        setBridgeSubMessage(`Sign in to ${config?.displayName || institution.name} in the secure browser window, then complete any bank prompt.`);
        bridgePollTimeoutRef.current = setTimeout(() => {
          void pollDesktopVisibleAuthStatus(config);
        }, config?.statusPollMs ?? 2000);
        return;
      }

      const hasStructuredDesktopVisibleAuthSuccess = Boolean(
        data.resultData?.synced ||
        (data.resultData?.authenticated && data.resultData?.responseStatus === 'handoff_ready')
      );

      if (data.status === 'succeeded' || hasStructuredDesktopVisibleAuthSuccess) {
        setDesktopVisibleAuthActive(institution.provider, false);
        if (data.resultData?.syncId) {
          desktopVisibleAuthSyncIdRef.current = data.resultData.syncId;
        }
        if (data.resultData?.synced) {
          setBridgeMessage('');
          setBridgeSubMessage('');
          setIsSilentSyncLocked(false);
          await finishOkResult({ ...data, status: 'ok' });
          return;
        }
        await retryDesktopVisibleAuthSync(0, config);
        return;
      }

      if (data.status === 'failed') {
        const failureMessage = data.message || `${config?.displayName || institution.name} secure login failed.`;
        if (normalizeSecureBrowserErrorMessage(failureMessage) === SECURE_BROWSER_CLOSED_MESSAGE) {
          showInterruptedResult();
          return;
        }
        showDesktopVisibleAuthError(failureMessage, config);
        return;
      }

      if (data.status === 'cancelled') {
        showInterruptedResult();
        return;
      }

      bridgePollTimeoutRef.current = setTimeout(() => {
        void pollDesktopVisibleAuthStatus(config);
      }, config?.statusPollMs ?? 2000);
    } catch (err) {
      showDesktopVisibleAuthError(err.message || `${config?.displayName || institution.name} secure login status check failed.`, config);
    }
  };

  const launchDesktopVisibleAuthFlow = async (config = desktopVisibleAuthConfig) => {
    if (closedRef.current || !config) return;

    desktopVisibleAuthAttemptIdRef.current = '';
    desktopVisibleAuthSyncIdRef.current = '';
    setDesktopVisibleAuthActive(institution.provider, false);
    setLoading(true);
    setMessage('');
    setStep(config.waitStep || 'desktop_visible_auth_wait');
    setBridgeMessage('Opening secure browser...');
    setBridgeSubMessage(
      isNewInstitution
        ? `A secure browser window is opening. Sign in there as you normally would to add ${config.displayName || institution.name} to ${APP_BRAND_NAME}.`
        : `A secure browser window is opening. Sign in there as you normally would to refresh ${config.displayName || institution.name}.`
    );

    try {
      const recordHar = config.recordHar === true;
      let captureLevel = 'redacted_rich';
      if (import.meta.env.DEV) {
        try {
          const resp = await fetch(`${API}/settings/dev-diagnostics/capture-level`);
          if (resp.ok) {
            const body = await resp.json();
            if (body?.status === 'ok' && typeof body.capture_level === 'string') {
              captureLevel = body.capture_level;
            }
          }
        } catch (_) {
          /* dev-only diagnostics — silent on transport errors */
        }
      }
      if (closedRef.current) return;
      const data = await launchDesktopVisibleAuth({
        provider: institution.provider,
        addFlow: isNewInstitution,
        institutionId: connectionIdRef.current || undefined,
        userId: 1,
        userTimezone: getDesktopVisibleAuthTimezone(userTimezone, userTimezoneConfigured),
        timeoutSeconds: 0,
        technicalStallSeconds: config.technicalStallSeconds,
        captureLevel,
        recordHar,
      });
      if (closedRef.current) {
        const lateAttemptId = String(data?.attemptId || '').trim();
        if (lateAttemptId) {
          const lateAttemptWasActive = (
            data?.requestStatus === 'launched'
            || data?.requestStatus === 'already_running'
            || data?.status === 'running'
          );
          if (lateAttemptWasActive) {
            try {
              await cancelDesktopVisibleAuth({
                provider: institution.provider,
                attemptId: lateAttemptId,
              });
            } catch (_) {
              // Best-effort ownership cleanup for a launch that resolved after teardown.
            }
          }
          await discardDesktopVisibleAuthAttempt(lateAttemptId);
        }
        return;
      }

      if (data.attemptId) {
        desktopVisibleAuthAttemptIdRef.current = data.attemptId;
      }

      if (data.requestStatus === 'launched' || data.requestStatus === 'already_running' || data.status === 'running') {
        setDesktopVisibleAuthActive(institution.provider, true);
        setBridgeMessage('Preparing secure browser...');
        setBridgeSubMessage(`${APP_BRAND_NAME} is preparing the secure browser. The bank login window will appear automatically.`);
        await pollDesktopVisibleAuthStatus(config);
        return;
      }

      showDesktopVisibleAuthError(data.message || `Failed to launch ${config.displayName || institution.name} secure login.`, config);
    } catch (err) {
      showDesktopVisibleAuthError(err.message || `Failed to launch ${config.displayName || institution.name} secure login.`, config);
    } finally {
      if (!closedRef.current) setLoading(false);
    }
  };
  const showInterruptedResult = () => {
    if (closedRef.current || terminatingRef.current) return;
    terminatingRef.current = true;
    if (isNewInstitution && institution.provider) {
      markInterruptedInstitutionAdd(institution.provider);
    }
    const desktopVisibleAuthAttemptId = desktopVisibleAuthAttemptIdRef.current;
    if (isDesktopVisibleAuthFlow && desktopVisibleAuthAttemptId) {
      void discardDesktopVisibleAuthAttempt(desktopVisibleAuthAttemptId);
    }
    desktopVisibleAuthAttemptIdRef.current = '';
    desktopVisibleAuthSyncIdRef.current = '';
    setDesktopVisibleAuthActive(institution.provider, false);
    clearPollTimeout();
    clearBridgePollTimeout();
    clearBridgeSyncRetryTimeout();
    clearVerificationTimeout();
    clearPushSyncHintTimeout();
    clearSmsSyncHintTimeout();
    setVerificationDialog(null);
    setLoading(false);
    setIsSilentSyncLocked(false);
    setBridgeMessage('');
    setBridgeSubMessage('');
    setStep('interrupted');
    setMessage(getInstitutionInterruptedMessage(institution, isNewInstitution ? 'add' : 'sync'));
    clearIncompleteNewInstitutionState();
    interruptedCloseTimeoutRef.current = setTimeout(() => {
      interruptedCloseTimeoutRef.current = null;
      if (closedRef.current) return;
      closedRef.current = true;
      onClose();
    }, AUTH_ATTEMPT_CLOSE_DELAY_MS);
  };
  const handleModalClose = async ({ reportInterrupted = true } = {}) => {
    if (isSilentSyncLocked || isVerificationDialogBlocking) return;
    clearBridgePollTimeout();
    clearBridgeSyncRetryTimeout();
    clearPushSyncHintTimeout();
    clearSmsSyncHintTimeout();
    setVerificationDialog(null);
    const desktopVisibleAuthAttemptId = desktopVisibleAuthAttemptIdRef.current;
    if (isDesktopVisibleAuthFlow && desktopVisibleAuthActiveRef.current[institution.provider]) {
      void logDesktopVisibleAuthTerminalEvent({
        result: 'cancelled',
        message: `${desktopVisibleAuthConfig?.displayName || institution.name} secure login was cancelled.`,
        details: { cancelled_from: 'modal_close' },
        attemptId: desktopVisibleAuthAttemptId,
      });
      try {
        await cancelDesktopVisibleAuth({
          provider: institution.provider,
          attemptId: desktopVisibleAuthAttemptIdRef.current || undefined,
        });
      } catch (_) {
        // Best-effort desktop helper cancellation only.
      }
      showInterruptedResult();
      return;
    }
    if (isDesktopVisibleAuthFlow) {
      await discardDesktopVisibleAuthAttempt(desktopVisibleAuthAttemptId);
    }
    desktopVisibleAuthAttemptIdRef.current = '';
    autoLoginStartedRef.current = false;
    if (reportInterrupted && step !== 'error') {
      showInterruptedResult();
      return;
    }
    clearIncompleteNewInstitutionState();
    onClose();
  };

  const startPolling = () => {
    clearPollTimeout();
    clearPushSyncHintTimeout();
    pushPollingFinalizedRef.current = false;
    pushApprovalDeadlineRef.current = Date.now() + PUSH_APPROVAL_TIMEOUT_MS;
    setPushPhase('approval');
    schedulePushSyncHint();

    const poll = async () => {
      if (closedRef.current || terminatingRef.current || pushPollingFinalizedRef.current) return;
      try {
        pushPollInFlightRef.current = true;
        const resp = await fetchWithTimeout(
          `${API}${endpoints.twofa}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(getInstitutionContextPayload()),
          },
          authFlowSyncTimeoutMs,
        );
        const data = await resp.json();
        if (Number(data?.institution_id || 0) > 0) {
          connectionIdRef.current = Number(data.institution_id);
        }
        pushPollInFlightRef.current = false;
        if (closedRef.current || terminatingRef.current || pushPollingFinalizedRef.current) return;

        if (data.status === 'ok') {
          if (isNewInstitution) {
            await persistScraperCredentials({ preservePendingSession: true });
          }
          pushPollingFinalizedRef.current = true;
          clearPollTimeout();
          clearVerificationTimeout();
          clearPushSyncHintTimeout();
          await finishOkResult(data);
          return;
        } else if (data.status === 'waiting' || data.status === 'already_syncing') {
          if (!pushPollingFinalizedRef.current) {
            pollTimeoutRef.current = setTimeout(poll, PUSH_POLL_INTERVAL_MS);
          }
        } else if (data.status === 'error' || data.status === 'network_error') {
          if (onDataChange) onDataChange();
          if (hasMissingPendingSessionMessage(data.message)) {
            await terminateVerificationAttempt(data.message || "Verification session is no longer available.");
            return;
          }
          showErrorAndReturn(data.message || "2FA failed");
        } else {
          if (onResultUpdate) {
            const resolvedStatus = data.status === 'network_error'
              ? 'network_error'
              : data.status === 'auth_required' || data.status === 'different_profile_detected'
                ? 'auth_required'
                : 'error';
            onResultUpdate(institution?.provider, resolvedStatus, data.message || 'Verification failed');
          }
          if (onDataChange) onDataChange();
          if (hasMissingPendingSessionMessage(data.message)) {
            await terminateVerificationAttempt(data.message || "Verification session is no longer available.");
            return;
          }
          showErrorAndReturn(data.message || "Verification failed");
        }
      } catch (err) {
        pushPollInFlightRef.current = false;
        showErrorAndReturn(err.message);
      }
    };

    pollTimeoutRef.current = setTimeout(poll, PUSH_POLL_INITIAL_DELAY_MS);
  };

  const handleLogin = async ({ useSavedCredentials = false } = {}) => {
    if ((!username || !password) && !useSavedCredentials) return;
    usingSavedCredentialsRef.current = useSavedCredentials;
    autoLoginStartedRef.current = true;

    if (!endpoints) return;
    if (closedRef.current) return;
    setLoading(true);
    setMessage('');
    setBridgeMessage('');
    setBridgeSubMessage('');
    setStep('auto_login');
    try {
      clearProviderAutoLoginBlock();
      if (authConfig.clearCredentialsBeforeNewLogin && isNewInstitution) {
        if (connectionIdRef.current) {
          await fetch(`${API}/credentials/${institution.provider}?institution_id=${encodeURIComponent(connectionIdRef.current)}`, { method: 'DELETE' });
        }
        if (closedRef.current) return;
      }
      if (isDesktopVisibleAuthFlow) {
        await launchDesktopVisibleAuthFlow(desktopVisibleAuthConfig);
        return;
      }
      const resp = await fetchWithTimeout(
        `${API}${endpoints.login}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ...(useSavedCredentials ? {} : { username, password }),
            ...getInstitutionContextPayload(),
          }),
        },
        authFlowSyncTimeoutMs,
      );
      const data = await resp.json();
      if (Number(data?.institution_id || 0) > 0) {
        connectionIdRef.current = Number(data.institution_id);
      }
      if (closedRef.current) return;
      if (data.status === 'ok' || data.status === '2fa_required') {
        // Save valid scraper credentials only when the provider's auth contract says the pending session can survive it.
        if (!skipAutoLogin && !useSavedCredentials && institution.provider) {
          let shouldPersistAfterLogin =
            !(authConfig.skipPersistOnLogin2fa && data.status === '2fa_required');

          if (isNewInstitution) {
            shouldPersistAfterLogin = data.status === 'ok' && shouldPersistAfterLogin;
          }

          if (shouldPersistAfterLogin) {
            await persistScraperCredentials({
              preservePendingSession: true,
            });
          }
        }
      }
      if (data.status === 'ok') {
        await finishOkResult(data);
      } else if (data.status === '2fa_required') {
        if (data.method === 'sms') {
          setStep('2fa_sms');
        } else {
          setStep('2fa_push');
          startPolling();
        }
      } else {
        if (onDataChange) onDataChange();
        if (onResultUpdate) {
          const resolvedStatus = data.status === 'network_error'
            ? 'network_error'
            : data.status === 'auth_required' || data.status === 'different_profile_detected'
              ? 'auth_required'
              : 'error';
          onResultUpdate(institution?.provider, resolvedStatus, data.message || 'Login failed');
        }
        showErrorAndReturn(
          data.message || 'Login failed',
          { returnToCredentials: data.status !== 'auth_required' },
        );
      }
    } catch (err) {
      if (onResultUpdate) {
        onResultUpdate(institution?.provider, 'error', err.message);
      }
      showErrorAndReturn(err.message);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    terminateVerificationAttemptRef.current = terminateVerificationAttempt;
    handleLoginRef.current = handleLogin;
    launchDesktopVisibleAuthFlowRef.current = launchDesktopVisibleAuthFlow;
    showInterruptedResultRef.current = showInterruptedResult;
  });

  // Auto-login when saved credentials are loaded
  useEffect(() => {
    if (isDesktopVisibleAuthFlow && !requireCredentialsBeforeDesktopVisibleAuth && !autoLoginStartedRef.current) {
      if (isNewInstitution && hasInterruptedInstitutionAdd(institution.provider)) {
        autoLoginStartedRef.current = true;
        showInterruptedResultRef.current?.();
        return;
      }
      const skipProviderAutoLoginOnce =
        institution.provider && sessionStorage.getItem(getSkipAutoLoginOnceKey(institution.provider)) === '1';
      if (skipProviderAutoLoginOnce) {
        sessionStorage.removeItem(getSkipAutoLoginOnceKey(institution.provider));
        return;
      }
      autoLoginStartedRef.current = true;
      void launchDesktopVisibleAuthFlowRef.current?.(desktopVisibleAuthConfig);
      return;
    }
    if (hasSavedCreds && !autoLoginStartedRef.current) {
      autoLoginStartedRef.current = true;
      void handleLoginRef.current?.({ useSavedCredentials: true });
    }
  }, [hasSavedCreds, username, password, isDesktopVisibleAuthFlow, requireCredentialsBeforeDesktopVisibleAuth, desktopVisibleAuthConfig, institution.provider, isNewInstitution]);
  useEffect(() => {
    clearVerificationTimeout();
    if (step !== '2fa_push') return undefined;
    const remainingApprovalMs = Math.max((pushApprovalDeadlineRef.current ?? Date.now()) - Date.now(), 0);

    const handleVerificationTimeout = () => {
      if (closedRef.current || terminatingRef.current || pushPollingFinalizedRef.current) return;
      if (pushPollInFlightRef.current) {
        verificationTimeoutRef.current = setTimeout(handleVerificationTimeout, 1000);
        return;
      }
      clearPollTimeout();
      pushPollingFinalizedRef.current = true;
      pushPollInFlightRef.current = false;
      if (onDataChange) onDataChange();
      void terminateVerificationAttemptRef.current?.("Push approval timed out. Please try again.");
    };

    verificationTimeoutRef.current = setTimeout(handleVerificationTimeout, remainingApprovalMs);

    return () => {
      clearVerificationTimeout();
    };
  }, [step, institution.provider, onDataChange]);
  useEffect(() => {
    if (step === '2fa_push') return undefined;
    clearPushSyncHintTimeout();
    pushApprovalDeadlineRef.current = null;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) {
        setPushPhase('approval');
      }
    });
    return () => {
      cancelled = true;
    };
  }, [step]);
  const handleSmsSubmit = async () => {
    if (!smsCode || !endpoints) return;
    if (closedRef.current) return;
    setMessage('');
    showVerificationDialog('verifying', 'Verifying your code...');
    scheduleSmsSyncHint();
    setLoading(true);
    try {
      const resp = await fetchWithTimeout(
        `${API}${endpoints.twofa}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(
            authConfig.smsPayload === 'credentials_and_code'
              ? {
                  ...(usingSavedCredentialsRef.current ? {} : { username, password }),
                  code: smsCode,
                  ...getInstitutionContextPayload(),
                }
              : { code: smsCode, ...getInstitutionContextPayload() }
          ),
        },
        authFlowSyncTimeoutMs,
      );
      const data = await resp.json();
      if (Number(data?.institution_id || 0) > 0) {
        connectionIdRef.current = Number(data.institution_id);
      }
      clearSmsSyncHintTimeout();
      if (closedRef.current || terminatingRef.current) return;

      if (data.status === 'ok') {
        if ((authConfig.persistCredentialsOnSmsOk || isNewInstitution) && !usingSavedCredentialsRef.current) {
          await persistScraperCredentials({ preservePendingSession: true });
        }
        await finishOkResult(data, { dialog: true });
      } else if (data.status === '2fa_required') {
        setSmsCode('');
        setStep('2fa_sms');
        setMessage(data.message || 'Enter your verification code');
        showVerificationDialog(null);
      } else if ((authConfig.closeOnSmsFailureStatuses || []).includes(data.status)) {
        if (onDataChange) onDataChange();
        showVerificationDialog(
          'error',
          data.message || "Verification failed. Please try again.",
          '',
          'close_modal',
        );
      } else if (data.message && (data.message.includes("Wrong attempt") || data.message.includes("invalid") || data.message.includes("expired") || data.message.includes("OTP") || data.message.includes("Wrong code"))) {
        showVerificationDialog('error', data.message || "2FA failed");
      } else {
        if (onDataChange) onDataChange();
        showVerificationDialog('error', data.message || "Something went wrong, please try again");
      }
    } catch (err) {
      clearSmsSyncHintTimeout();
      if ((authConfig.closeOnSmsFailureStatuses || []).length > 0) {
        if (onDataChange) onDataChange();
        showVerificationDialog(
          'error',
          err.message || "Verification failed. Please try again.",
          '',
          'close_modal',
        );
      } else {
        if (onDataChange) onDataChange();
        showVerificationDialog('error', err.message);
      }
    } finally {
      setLoading(false);
    }
  };

  const desktopVisibleAuthWaitStep = desktopVisibleAuthConfig?.waitStep || 'desktop_visible_auth_wait';
  const isDesktopVisibleAuthModalStep = isDesktopVisibleAuthFlow && (
    step === desktopVisibleAuthWaitStep ||
    step === 'desktop_visible_auth_finishing' ||
    (step === 'credentials' && !requireCredentialsBeforeDesktopVisibleAuth)
  );
  const isExternalAuthModalStep = isDesktopVisibleAuthModalStep;
  const secureBrowserStatus = bridgeMessage || 'Opening secure browser...';
  const showDesktopVisibleAuthCancel = isDesktopVisibleAuthModalStep && step !== 'desktop_visible_auth_finishing';
  const addFlowDesktopVisibleAuthGuidance = {
    intro: `Please enter your credentials and sign in to ${institution.name} as you normally would in online banking.`,
    detail: `Once secure login is complete, ${APP_BRAND_NAME} will add ${institution.name} and finish syncing your accounts.`,
  };
  const manualDesktopVisibleAuthGuidance = {
    intro: `Please sign in to ${institution.name} as you normally would in online banking.`,
    detail: `Once secure login is complete, ${APP_BRAND_NAME} will finish refreshing ${institution.name} in the background.`,
  };
  const showCornerClose =
    step !== 'success' &&
    step !== 'interrupted' &&
    step !== 'error' &&
    step !== '2fa_push' &&
    step !== 'auto_login' &&
    !verificationDialog &&
    !isSilentSyncLocked &&
    !isExternalAuthModalStep;

  const renderDesktopVisibleAuthFlow = () => (
    <div className="modal-body modal-center secure-browser-body">
      <div className="secure-browser-status-row" aria-live="polite">
        <div className="modal-spinner secure-browser-spinner" />
        <div className="secure-browser-status-copy">
          <p className="secure-browser-eyebrow">Secure login in progress</p>
          <p className="modal-2fa-text">{secureBrowserStatus}</p>
        </div>
      </div>
      <div className="secure-browser-guidance">
        <p>{renderBrandText((isNewInstitution ? addFlowDesktopVisibleAuthGuidance : manualDesktopVisibleAuthGuidance).intro, 'desktop-visible-auth-intro')}</p>
        <p>{renderBrandText((isNewInstitution ? addFlowDesktopVisibleAuthGuidance : manualDesktopVisibleAuthGuidance).detail, 'desktop-visible-auth-detail')}</p>
      </div>
      {showDesktopVisibleAuthCancel && (
        <button className="btn-primary modal-btn secure-browser-cancel app-control-root" onClick={() => { void handleModalClose(); }}>
          <span className="app-control-label">Cancel login</span>
        </button>
      )}
    </div>
  );

  const handleKeyDown = (e) => {
    if (verificationDialog) {
      if (e.key === 'Escape' && !isVerificationDialogBlocking) {
        e.preventDefault();
        e.stopPropagation();
        dismissVerificationDialog();
      }
      return;
    }
    if (step === 'interrupted') return;
    if (e.key === 'Escape' && isExternalAuthModalStep) {
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (e.key === 'Enter') {
      if (step === 'credentials') handleLogin();
      if (step === '2fa_sms') handleSmsSubmit();
    }
    if (e.key === 'Escape' && step !== 'auto_login' && step !== '2fa_push') {
      void handleModalClose();
    }
  };

  return (
    <div className={`modal-overlay ${isExternalAuthModalStep ? 'modal-overlay-secure-browser' : ''}`}>
      {verificationDialog ? (
        <div
          className="modal-content modal-dialog-card modal-dialog-wide verification-dialog-card"
          onClick={(e) => e.stopPropagation()}
          onKeyDown={handleKeyDown}
        >
          <div className="modal-header verification-dialog-header">
            <InstitutionLogo name={institution.name} size={32} />
            <h3 className="modal-title">{institution.name}</h3>
            {!isVerificationDialogBlocking && (
              <button className="modal-close" onClick={dismissVerificationDialog}>✕</button>
            )}
          </div>
          <div className="modal-body modal-center verification-dialog-body">
            {verificationDialog.status === 'verifying' && (
              <InstitutionSyncProgress
                title={verificationDialog.message}
                subtitle={renderBrandText(verificationDialog.subMessage, 'verification-dialog-submessage')}
                wrapperClassName="modal-center"
              />
            )}
            {verificationDialog.status === 'success' && <div className="modal-success-icon">✓</div>}
            {verificationDialog.status === 'error' && <div className="modal-error-mark">✕</div>}
            {verificationDialog.status !== 'verifying' && (
              <p className="modal-2fa-text modal-dialog-title">{verificationDialog.message}</p>
            )}
            {verificationDialog.status !== 'verifying' && verificationDialog.subMessage && (
              <p className="modal-dialog-copy">{renderBrandText(verificationDialog.subMessage, 'verification-dialog-copy')}</p>
            )}
            {verificationDialog.status === 'error' && (
              <div className="modal-dialog-actions">
                <button className="btn-primary modal-dialog-action app-control-root" onClick={dismissVerificationDialog}>
                  <span className="app-control-label">Close</span>
                </button>
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className={`modal-content ${isExternalAuthModalStep ? 'modal-content-secure-browser' : ''}`} onClick={(e) => e.stopPropagation()} onKeyDown={handleKeyDown}>
          <div className="modal-header">
            <InstitutionLogo name={institution.name} size={36} />
            <h3 className="modal-title">{institution.name}</h3>
            {showCornerClose && <button className="modal-close" onClick={() => { void handleModalClose(); }}>✕</button>}
          </div>

          {step === 'credentials' && isDesktopVisibleAuthFlow && !requireCredentialsBeforeDesktopVisibleAuth && renderDesktopVisibleAuthFlow()}
          {step === desktopVisibleAuthWaitStep && renderDesktopVisibleAuthFlow()}
          {step === 'desktop_visible_auth_finishing' && (
            <InstitutionSyncProgress
              title="Connecting your accounts..."
              subtitle={renderBrandText(`If secure login completed, ${APP_BRAND_NAME} is now pulling accounts, holdings, and current balances. Transaction history will continue in the background.`, 'desktop-visible-auth-finishing')}
            />
          )}

          {step === 'auto_login' && !isDesktopVisibleAuthModalStep && (
            <div className="modal-body-status">
              <MdSync size={24} className="spin-icon modal-status-icon" />
              <p className="modal-status-copy">{bridgeMessage || 'Logging in...'}</p>
              {bridgeSubMessage && <p className="modal-2fa-sub">{renderBrandText(bridgeSubMessage, 'bridge-submessage')}</p>}
            </div>
          )}
          {step === 'credentials' && (!isDesktopVisibleAuthFlow || requireCredentialsBeforeDesktopVisibleAuth) && (
            <div className="modal-body">
              <p className="modal-desc">Sign in to sync your accounts</p>
              <div className="settings-field">
                <label>{labels.username}</label>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => {
                    credentialsEditedRef.current = true;
                    clearProviderAutoLoginBlock();
                    setUsername(e.target.value);
                  }}
                  placeholder={`Enter your ${labels.username.toLowerCase()}`}
                  autoFocus
                />
              </div>
              <div className="settings-field">
                <label>{labels.password}</label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => {
                    credentialsEditedRef.current = true;
                    clearProviderAutoLoginBlock();
                    setPassword(e.target.value);
                  }}
                  placeholder={`Enter your ${labels.password.toLowerCase()}`}
                />
              </div>
              {message && <p className="modal-error">{renderBrandText(message, 'scraper-auth-message')}</p>}
              <button className="btn-primary modal-btn app-control-root" onClick={handleLogin} disabled={loading || !username || !password}>
                <span className="app-control-label">{loading ? 'Connecting...' : institution.isNew ? `Add ${institution.name}` : 'Save & Sync'}</span>
              </button>
              <p className="modal-privacy">Your credentials are saved locally for future syncs and are not sent to the developer.</p>
            </div>
          )}

          {step === '2fa_push' && (
            pushPhase === 'syncing' ? (
              <InstitutionSyncProgress
                title="Connecting your accounts..."
                subtitle={renderBrandText(`If approval went through, ${APP_BRAND_NAME} is now pulling accounts, holdings, and current balances. Transaction history will continue in the background.`, 'scraper-push-syncing')}
              />
            ) : (
              <div className="modal-body modal-center">
                <div className="modal-spinner" />
                <p className="modal-2fa-text">Approve the notification on your device</p>
                <p className="modal-2fa-sub">Waiting for approval...</p>
              </div>
            )
          )}

          {step === '2fa_sms' && (
            <div className="modal-body">
              <p className="modal-desc">{'Enter your verification code'}</p>
              <div className="settings-field">
                <label>Verification Code</label>
                <input
                  type="text"
                  value={smsCode}
                  onChange={(e) => setSmsCode(e.target.value)}
                  placeholder="Enter code"
                  autoFocus
                  maxLength={8}
                />
              </div>
              {message && !verificationDialog && <p className="modal-error">{renderBrandText(message, 'scraper-sms-message')}</p>}
              <button className="btn-primary modal-btn app-control-root" onClick={handleSmsSubmit} disabled={loading || !smsCode}>
                <span className="app-control-label">Verify</span>
              </button>
            </div>
          )}

          {step === 'error' && (
            <div className="modal-body modal-center">
              <div className="modal-error-mark">✕</div>
              <p className="modal-2fa-text">{message || 'Something went wrong'}</p>
              <button className="btn-primary modal-btn app-control-root" onClick={() => { void handleModalClose(); }}>
                <span className="app-control-label">Close</span>
              </button>
            </div>
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
              <p className="modal-2fa-text">{message}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default ScraperAuthModal;
