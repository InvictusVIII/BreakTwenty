import { API } from '../config';
import { ADD_INSTITUTION_SYNC_REQUEST_TIMEOUT_MS } from './syncRequests';
import { persistentStorage } from './persistentStorage';

const PENDING_ADD_PREFIX = 'breaktwenty_pending_add_provider_';
const INTERRUPTED_ADD_PREFIX = 'breaktwenty_interrupted_add_provider_';

export const INCOMPLETE_ADD_CLEANUP_RETRY_MS = 5000;
export const STARTUP_INCOMPLETE_ADD_CLEANUP_TIMEOUT_MS = 5000;
export const INCOMPLETE_ADD_CLEANUP_RETRY_COUNT = Math.ceil(
  (ADD_INSTITUTION_SYNC_REQUEST_TIMEOUT_MS + (2 * 60 * 1000)) / INCOMPLETE_ADD_CLEANUP_RETRY_MS,
);

function getPendingAddKey(provider) {
  return `${PENDING_ADD_PREFIX}${provider}`;
}

function getInterruptedAddKey(provider) {
  return `${INTERRUPTED_ADD_PREFIX}${provider}`;
}

function getPendingAddMarker(provider) {
  if (!provider) return null;
  try {
    return JSON.parse(persistentStorage.getItem(getPendingAddKey(provider)) || 'null');
  } catch (_) {
    return null;
  }
}

function createAttemptId(provider) {
  if (window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `${provider}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function markerMatchesAttempt(provider, attemptId) {
  const marker = getPendingAddMarker(provider);
  if (!marker) return false;
  return !attemptId || marker.attemptId === attemptId;
}

export function markPendingInstitutionAdd(provider) {
  if (!provider) return '';
  const attemptId = createAttemptId(provider);
  persistentStorage.setItem(getPendingAddKey(provider), JSON.stringify({
    provider,
    attemptId,
    startedAt: Date.now(),
  }));
  return attemptId;
}

export function markInterruptedInstitutionAdd(provider) {
  if (!provider) return;
  sessionStorage.setItem(getInterruptedAddKey(provider), String(Date.now()));
}

export function clearInterruptedInstitutionAdd(provider) {
  if (!provider) return;
  sessionStorage.removeItem(getInterruptedAddKey(provider));
}

export function hasInterruptedInstitutionAdd(provider) {
  if (!provider) return false;
  return sessionStorage.getItem(getInterruptedAddKey(provider)) !== null;
}

export function clearPendingInstitutionAdd(provider, attemptId = '') {
  if (!provider) return;
  if (attemptId && !markerMatchesAttempt(provider, attemptId)) return;
  persistentStorage.removeItem(getPendingAddKey(provider));
}

export function getPendingInstitutionAddProviders() {
  const providers = [];
  for (let index = 0; index < persistentStorage.length; index += 1) {
    const key = persistentStorage.key(index);
    if (!key || !key.startsWith(PENDING_ADD_PREFIX)) continue;
    const provider = key.slice(PENDING_ADD_PREFIX.length);
    if (provider) providers.push(provider);
  }
  return Array.from(new Set(providers));
}

export async function cleanupIncompleteInstitutionAdd(provider, { signal } = {}) {
  if (!provider) return null;
  const response = await fetch(`${API}/institutions/provider/${encodeURIComponent(provider)}/state`, {
    method: 'DELETE',
    signal,
  }).catch(() => {});
  if (!response?.ok) return null;
  return response.json().catch(() => ({ status: 'ok' }));
}

export async function cleanupPendingInstitutionAddOnStartup(provider) {
  const attemptId = getPendingAddMarker(provider)?.attemptId || '';
  if (!provider || !attemptId) return null;

  const controller = new AbortController();
  const timeoutId = setTimeout(
    () => controller.abort(),
    STARTUP_INCOMPLETE_ADD_CLEANUP_TIMEOUT_MS,
  );
  try {
    const cleanupResult = await cleanupIncompleteInstitutionAdd(provider, {
      signal: controller.signal,
    });
    if (cleanupResult && markerMatchesAttempt(provider, attemptId)) {
      clearPendingInstitutionAdd(provider, attemptId);
    }
    return cleanupResult;
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function confirmPendingInstitutionAddResult(
  provider,
  institutionId,
  sourceSyncId = '',
  attemptId = '',
) {
  if (!provider || !Number(institutionId)) return {};
  const response = await fetch(`${API}/institutions/provider/${encodeURIComponent(provider)}/add-confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      institution_id: Number(institutionId),
      source_sync_id: sourceSyncId || undefined,
      attempt_id: attemptId || undefined,
    }),
  }).catch(() => {});
  if (!response?.ok) return {};
  return response.json().catch(() => ({}));
}

export async function confirmPendingInstitutionAdd(
  provider,
  institutionId,
  sourceSyncId = '',
  attemptId = '',
) {
  const data = await confirmPendingInstitutionAddResult(
    provider,
    institutionId,
    sourceSyncId,
    attemptId,
  );
  return data.status === 'ok' && Number(data.confirmed_institutions || 0) > 0;
}

export function startIncompleteInstitutionAddCleanup(provider, { attemptId = '', onAttempt, clearOnFirstOk = false } = {}) {
  if (!provider) return () => {};

  const cleanupAttemptId = attemptId || getPendingAddMarker(provider)?.attemptId || '';
  let cancelled = false;
  let attempts = 0;
  let cleanupTimer = null;

  const runCleanup = async () => {
    if (!markerMatchesAttempt(provider, cleanupAttemptId)) return;
    attempts += 1;
    const cleanupResult = await cleanupIncompleteInstitutionAdd(provider);
    if (cancelled) return;
    if (onAttempt) {
      await onAttempt(provider, attempts);
    }
    if (cleanupResult && clearOnFirstOk) {
      clearPendingInstitutionAdd(provider, cleanupAttemptId);
      return;
    }
    if (cleanupResult?.removed_institutions > 0 || cleanupResult?.skipped_institutions > 0) {
      clearPendingInstitutionAdd(provider, cleanupAttemptId);
      return;
    }
    if (cancelled) return;
    if (attempts >= INCOMPLETE_ADD_CLEANUP_RETRY_COUNT) {
      clearPendingInstitutionAdd(provider, cleanupAttemptId);
      return;
    }
    cleanupTimer = setTimeout(runCleanup, INCOMPLETE_ADD_CLEANUP_RETRY_MS);
  };

  void runCleanup();

  return () => {
    cancelled = true;
    if (cleanupTimer) clearTimeout(cleanupTimer);
  };
}
