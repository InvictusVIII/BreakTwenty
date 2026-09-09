import { API } from '../config';
import {
  DEFAULT_SYNC_REQUEST_TIMEOUT_MS,
  fetchWithTimeout,
} from './syncRequests';
import { subscribeBatchEvents } from './serverEvents';
import { APP_BRAND_NAME } from '../constants/brand';
import { persistentStorage } from './persistentStorage';

const TERMINAL_BATCH_STATUSES = new Set(['done', 'error', 'not_found', 'blocked']);
const SYNC_BATCH_RECONCILE_INTERVAL_MS = 1500;
const SYNC_BATCH_MONITOR_TIMEOUT_MS = 30 * 60 * 1000;
const SYNC_NETWORK_BLOCKED_CODE = 'dns_resolution_temporarily_unavailable';
const ACTIVE_SYNC_BATCH_STATUSES = new Set(['queued', 'running']);
const MONITORED_SYNC_BATCH_IDS_STORAGE_KEY = 'breaktwenty_monitored_sync_batch_ids_v1';
const MAX_MONITORED_SYNC_BATCH_IDS = 100;
const SAFE_SYNC_BATCH_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

function normalizedBatchId(value) {
  const batchId = String(value || '').trim();
  return SAFE_SYNC_BATCH_ID_RE.test(batchId) ? batchId : '';
}

function monitoredBatchStorage(storage) {
  if (storage) return storage;
  return persistentStorage;
}

export function loadMonitoredSyncBatchIds(storage = null) {
  const target = monitoredBatchStorage(storage);
  if (!target) return [];
  try {
    const parsed = JSON.parse(target.getItem(MONITORED_SYNC_BATCH_IDS_STORAGE_KEY) || '[]');
    if (!Array.isArray(parsed)) return [];
    return [...new Set(parsed.map(normalizedBatchId).filter(Boolean))]
      .slice(-MAX_MONITORED_SYNC_BATCH_IDS);
  } catch (_error) {
    return [];
  }
}

function saveMonitoredSyncBatchIds(batchIds, storage = null) {
  const target = monitoredBatchStorage(storage);
  if (!target) return;
  const normalized = [...new Set(
    (Array.isArray(batchIds) ? batchIds : []).map(normalizedBatchId).filter(Boolean),
  )].slice(-MAX_MONITORED_SYNC_BATCH_IDS);
  try {
    if (normalized.length === 0) {
      target.removeItem(MONITORED_SYNC_BATCH_IDS_STORAGE_KEY);
    } else {
      target.setItem(MONITORED_SYNC_BATCH_IDS_STORAGE_KEY, JSON.stringify(normalized));
    }
  } catch (_error) {
    // Durable backend state remains authoritative when browser storage is unavailable.
  }
}

export function rememberMonitoredSyncBatchId(batchId, storage = null) {
  const normalized = normalizedBatchId(batchId);
  if (!normalized) return;
  saveMonitoredSyncBatchIds(
    [...loadMonitoredSyncBatchIds(storage), normalized],
    storage,
  );
}

export function forgetMonitoredSyncBatchId(batchId, storage = null) {
  const normalized = normalizedBatchId(batchId);
  if (!normalized) return;
  saveMonitoredSyncBatchIds(
    loadMonitoredSyncBatchIds(storage).filter((candidate) => candidate !== normalized),
    storage,
  );
}

async function parseSyncBatchResponse(resp, fallbackMessage) {
  const payload = await resp.json().catch(() => null);
  if (!resp.ok) {
    throw new Error(payload?.message || payload?.detail || fallbackMessage);
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error(fallbackMessage);
  }
  return payload;
}

export function mergeInstitutionAccounts(currentAccounts, institutionId, nextInstitutionAccounts) {
  const id = Number(institutionId || 0);
  if (!id || !Array.isArray(nextInstitutionAccounts)) {
    return currentAccounts;
  }
  const current = Array.isArray(currentAccounts) ? currentAccounts : [];
  const firstIndex = current.findIndex((account) => Number(account.institution_id || 0) === id);
  const accountsWithoutInstitution = current.filter((account) => Number(account.institution_id || 0) !== id);
  if (firstIndex < 0) {
    return [...accountsWithoutInstitution, ...nextInstitutionAccounts];
  }
  const insertAt = current
    .slice(0, firstIndex)
    .filter((account) => Number(account.institution_id || 0) !== id)
    .length;
  return [
    ...accountsWithoutInstitution.slice(0, insertAt),
    ...nextInstitutionAccounts,
    ...accountsWithoutInstitution.slice(insertAt),
  ];
}

export async function startSyncBatch(connections, mode, { signal } = {}) {
  const resp = await fetchWithTimeout(`${API}/sync/batch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ connections, mode }),
    signal,
  }, DEFAULT_SYNC_REQUEST_TIMEOUT_MS);
  const payload = await parseSyncBatchResponse(resp, 'Failed to start sync batch.');
  if (payload.status === 'started' && payload.batch_id) {
    rememberMonitoredSyncBatchId(payload.batch_id);
  }
  return payload;
}

export async function fetchSyncBatch(batchId, { signal } = {}) {
  const normalizedId = normalizedBatchId(batchId);
  if (!normalizedId) throw new Error('Failed to read sync batch status.');
  const resp = await fetchWithTimeout(
    `${API}/sync/batch/${encodeURIComponent(normalizedId)}`,
    { signal },
    DEFAULT_SYNC_REQUEST_TIMEOUT_MS,
  );
  const payload = await parseSyncBatchResponse(resp, 'Failed to read sync batch status.');
  return payload.batch_id ? payload : { ...payload, batch_id: normalizedId };
}

export async function fetchActiveSyncBatches({ signal } = {}) {
  const resp = await fetchWithTimeout(
    `${API}/sync/batches/active`,
    { signal },
    DEFAULT_SYNC_REQUEST_TIMEOUT_MS,
  );
  const payload = await parseSyncBatchResponse(resp, 'Failed to read active sync batches.');
  if (!Array.isArray(payload.batches)) {
    throw new Error('Failed to read active sync batches.');
  }
  return payload.batches;
}

export function reconcileActiveSyncBatches(currentBatches, nextBatch) {
  const current = Array.isArray(currentBatches) ? currentBatches : [];
  const batchId = String(nextBatch?.batch_id || '').trim();
  if (!batchId) return current;
  const withoutBatch = current.filter(
    (batch) => String(batch?.batch_id || '').trim() !== batchId,
  );
  const status = String(nextBatch?.status || '').trim().toLowerCase();
  if (!ACTIVE_SYNC_BATCH_STATUSES.has(status)) {
    return withoutBatch;
  }
  return [nextBatch, ...withoutBatch];
}

export function isTerminalSyncBatch(batch) {
  return TERMINAL_BATCH_STATUSES.has(String(batch?.status || '').trim().toLowerCase());
}

export function getActiveSyncBatchInstitutionIds(batches) {
  const institutionIds = new Set();
  getActiveSyncBatchConnectionStates(batches).forEach((result, institutionId) => {
    const status = String(result?.status || '').trim().toLowerCase();
    if (status === 'pending' || status === 'syncing') {
      institutionIds.add(institutionId);
    }
  });
  return institutionIds;
}

export function getActiveSyncBatchConnectionStates(batches) {
  const states = new Map();
  (Array.isArray(batches) ? batches : []).forEach((batch) => {
    const batchStatus = String(batch?.status || '').trim().toLowerCase();
    if (!ACTIVE_SYNC_BATCH_STATUSES.has(batchStatus)) {
      return;
    }
    Object.entries(batch?.results || {}).forEach(([connectionKey, result]) => {
      const institutionId = Number(result?.institution_id || 0);
      const status = String(result?.status || '').trim().toLowerCase();
      if (institutionId <= 0 || !status || states.has(institutionId)) {
        return;
      }
      states.set(institutionId, {
        ...(result || {}),
        status,
        institution_id: institutionId,
        batch_id: String(batch?.batch_id || '').trim(),
        batch_status: batchStatus,
        connection_key: connectionKey,
      });
    });
  });
  return states;
}

export function hasActiveManualSyncBatch(batches) {
  return (Array.isArray(batches) ? batches : []).some((batch) => (
    ACTIVE_SYNC_BATCH_STATUSES.has(String(batch?.status || '').trim().toLowerCase())
    && String(batch?.mode || '').trim().toLowerCase() === 'manual'
  ));
}

export function getSyncNetworkBlocker(payload) {
  const candidate = payload?.blocker || payload;
  if (
    String(candidate?.code || '') !== SYNC_NETWORK_BLOCKED_CODE
    && String(payload?.status || '') !== 'network_blocked'
  ) {
    return null;
  }
  return {
    code: SYNC_NETWORK_BLOCKED_CODE,
    title: 'Sync paused',
    message: String(candidate?.message || '').trim()
      || `${APP_BRAND_NAME} couldn't reach your financial providers because of a temporary connection problem. Check your internet connection and try again.`,
  };
}

function applyResults(batch, onConnectionResult) {
  if (!batch || !onConnectionResult) return;
  const results = batch.results || {};
  Object.entries(results).forEach(([connectionKey, result]) => {
    if (String(result?.status || '') === 'network_blocked') return;
    try {
      onConnectionResult(connectionKey, result || {});
    } catch (err) {
      console.error(`syncBatch: connection handler for ${connectionKey} threw`, err);
    }
  });
}

export async function runSyncBatchUntilDone({
  connections,
  mode,
  onConnectionResult,
  signal,
  timeoutMs = SYNC_BATCH_MONITOR_TIMEOUT_MS,
}) {
  const cancelledResult = (batchId = null) => ({
    status: 'error',
    code: 'sync_batch_cancelled',
    ...(batchId ? { batch_id: batchId } : {}),
    message: 'Sync batch monitoring was cancelled.',
  });
  if (signal?.aborted) {
    return cancelledResult();
  }
  let started;
  try {
    started = await startSyncBatch(connections, mode, { signal });
  } catch (error) {
    if (signal?.aborted) return cancelledResult();
    throw error;
  }
  if (started.status !== 'started' || !started.batch_id) {
    return { status: 'error', message: started.message || 'Failed to start sync batch.' };
  }
  const batchId = started.batch_id;
  if (signal?.aborted) {
    return cancelledResult(batchId);
  }
  return new Promise((resolve) => {
    let settled = false;
    let reconcileTimer = null;
    let deadlineTimer = null;
    let activeReconcileController = null;
    let unsubscribe = () => {};
    const finish = (batch) => {
      if (settled) return;
      settled = true;
      const clientMonitorCode = String(batch?.code || '').trim().toLowerCase();
      if (
        isTerminalSyncBatch(batch)
        && batch?.batch_id
        && clientMonitorCode !== 'sync_batch_timeout'
        && clientMonitorCode !== 'sync_batch_cancelled'
      ) {
        forgetMonitoredSyncBatchId(batch.batch_id);
      }
      if (reconcileTimer) clearTimeout(reconcileTimer);
      if (deadlineTimer) clearTimeout(deadlineTimer);
      activeReconcileController?.abort();
      signal?.removeEventListener('abort', handleAbort);
      unsubscribe();
      resolve(batch);
    };
    const handleAbort = () => finish(cancelledResult(batchId));
    const handleBatch = (batch) => {
      if (settled || !batch) return;
      applyResults(batch, onConnectionResult);
      const status = String(batch?.status || '').toLowerCase();
      if (TERMINAL_BATCH_STATUSES.has(status)) {
        finish(batch);
      }
    };
    const reconcile = async () => {
      const controller = new AbortController();
      activeReconcileController = controller;
      try {
        handleBatch(await fetchSyncBatch(batchId, { signal: controller.signal }));
      } catch (_) {
        // SSE may still finish the batch; retry the authoritative snapshot after a short delay.
      } finally {
        if (activeReconcileController === controller) {
          activeReconcileController = null;
        }
      }
      if (!settled) {
        reconcileTimer = setTimeout(reconcile, SYNC_BATCH_RECONCILE_INTERVAL_MS);
      }
    };
    unsubscribe = subscribeBatchEvents(batchId, handleBatch);
    signal?.addEventListener('abort', handleAbort, { once: true });
    deadlineTimer = setTimeout(() => finish({
      status: 'error',
      code: 'sync_batch_timeout',
      batch_id: batchId,
      message: 'Sync batch monitoring timed out.',
    }), Math.max(timeoutMs, 0));
    reconcile();
  });
}
