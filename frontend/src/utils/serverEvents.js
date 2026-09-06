import { API } from '../config';
import { isPlainObject, readJsonResponse } from './apiResponse';

const SERVER_EVENT_TYPES = Object.freeze({
  SYNC_ACTIVITY: 'sync_activity',
  TRANSACTION_IMPORT_STATUS: 'transaction_import_status',
  SYNC_BATCH: 'sync_batch',
});

const eventSubscribers = new Map();
const batchSubscribers = new Map();

let eventSource = null;

function hasSubscribers() {
  const hasGlobalSubscribers = Array.from(eventSubscribers.values()).some((bucket) => bucket.size > 0);
  const hasBatchSubscribers = Array.from(batchSubscribers.values()).some((bucket) => bucket.size > 0);
  return hasGlobalSubscribers || hasBatchSubscribers;
}

function isTransactionImportStatusPayload(payload) {
  return isPlainObject(payload) && Array.isArray(payload.institutions);
}

function ensureBucket(map, key) {
  let bucket = map.get(key);
  if (!bucket) {
    bucket = new Set();
    map.set(key, bucket);
  }
  return bucket;
}

function notifySubscribers(eventType, record) {
  const bucket = eventSubscribers.get(eventType);
  if (!bucket || bucket.size === 0) return;
  bucket.forEach((handler) => {
    try {
      handler(record);
    } catch (err) {
      console.error(`serverEvents: handler for ${eventType} threw`, err);
    }
  });
}

function notifyBatchSubscribers(record) {
  const batchId = String(record?.batch_id || '').trim();
  if (!batchId) return;
  const bucket = batchSubscribers.get(batchId);
  if (!bucket || bucket.size === 0) return;
  bucket.forEach((handler) => {
    try {
      handler(record.payload || {});
    } catch (err) {
      console.error('serverEvents: batch handler threw', err);
    }
  });
}

function dispatchMessage(rawData) {
  if (!rawData) return;
  let record = null;
  try {
    record = JSON.parse(rawData);
  } catch (_) {
    return;
  }
  const eventType = String(record?.type || '').trim();
  if (!eventType) return;
  if (eventType === SERVER_EVENT_TYPES.SYNC_BATCH) {
    notifyBatchSubscribers(record);
  }
  notifySubscribers(eventType, record);
}

function attachListeners(source) {
  source.onmessage = (event) => {
    dispatchMessage(event.data);
  };
  source.onerror = (err) => {
    if (import.meta.env.DEV) {
      console.warn('serverEvents: stream error (EventSource will auto-reconnect)', err);
    }
  };
}

function ensureEventSource() {
  if (eventSource) return eventSource;
  if (typeof window === 'undefined' || typeof EventSource === 'undefined') {
    return null;
  }
  eventSource = new EventSource(`${API}/events/stream`);
  attachListeners(eventSource);
  return eventSource;
}

function closeEventSourceIfIdle() {
  if (!eventSource) return;
  if (hasSubscribers()) return;
  eventSource.close();
  eventSource = null;
}

export function reconnectServerEvents({ timeoutMs = 10_000 } = {}) {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (!hasSubscribers()) {
    return Promise.resolve({ status: 'idle' });
  }
  const source = ensureEventSource();
  if (!source) {
    return Promise.reject(new Error('Server events are unavailable.'));
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeoutId);
      source.removeEventListener?.('open', handleOpen);
      if (error) reject(error);
      else resolve({ status: 'connected' });
    };
    const handleOpen = () => finish(null);
    const timeoutId = window.setTimeout(() => {
      finish(new Error('Server event reconnection timed out.'));
    }, Math.max(0, timeoutMs));
    source.addEventListener?.('open', handleOpen);
    if (source.readyState === EventSource.OPEN) finish(null);
  });
}

export function subscribeServerEvent(eventType, handler) {
  if (typeof handler !== 'function' || !eventType) {
    return () => {};
  }
  ensureEventSource();
  const bucket = ensureBucket(eventSubscribers, eventType);
  bucket.add(handler);
  return () => {
    bucket.delete(handler);
    if (bucket.size === 0) eventSubscribers.delete(eventType);
    closeEventSourceIfIdle();
  };
}

export function subscribeBatchEvents(batchId, handler) {
  const key = String(batchId || '').trim();
  if (!key || typeof handler !== 'function') {
    return () => {};
  }
  ensureEventSource();
  const bucket = ensureBucket(batchSubscribers, key);
  bucket.add(handler);
  return () => {
    bucket.delete(handler);
    if (bucket.size === 0) batchSubscribers.delete(key);
    closeEventSourceIfIdle();
  };
}

export function awaitTransactionImportJob({ provider, jobId, terminalStatuses, timeoutMs = 30 * 60 * 1000, appearanceGraceMs = 30 * 1000 }) {
  const targetProvider = String(provider || '').trim().toLowerCase();
  const targetJobId = String(jobId || '').trim();
  if (!targetProvider || !targetJobId) {
    return Promise.resolve();
  }
  const terminal = new Set(Array.from(terminalStatuses || ['complete', 'failed', 'auth_required', 'canceled']));
  return new Promise((resolve) => {
    let settled = false;
    let sawMatchingJob = false;
    const appearanceDeadline = Date.now() + appearanceGraceMs;
    const overallDeadline = Date.now() + timeoutMs;
    const statusController = new AbortController();
    let eventRevision = 0;
    let appearanceTimer = null;
    let timeoutTimer = null;
    let unsubscribe = () => {};
    const finish = () => {
      if (settled) return;
      settled = true;
      if (appearanceTimer) clearTimeout(appearanceTimer);
      if (timeoutTimer) clearTimeout(timeoutTimer);
      statusController.abort();
      unsubscribe();
      resolve();
    };
    const handle = (record) => {
      const payload = record?.payload || {};
      const institutions = Array.isArray(payload.institutions) ? payload.institutions : [];
      const providerInstitutions = institutions.filter(
        (inst) => String(inst?.provider || '').toLowerCase() === targetProvider,
      );
      if (providerInstitutions.length === 0) {
        if (sawMatchingJob || Date.now() >= appearanceDeadline) finish();
        return;
      }
      const institution = providerInstitutions.find(
        (inst) => String(inst?.transaction_import_job_id || '').trim() === targetJobId,
      );
      if (!institution) {
        if (sawMatchingJob || Date.now() >= appearanceDeadline) finish();
        return;
      }
      const status = String(institution.transaction_import_job_status || '').trim().toLowerCase();
      sawMatchingJob = true;
      if (appearanceTimer) {
        clearTimeout(appearanceTimer);
        appearanceTimer = null;
      }
      if (terminal.has(status)) finish();
    };
    appearanceTimer = setTimeout(() => {
      if (!sawMatchingJob) finish();
    }, Math.max(appearanceDeadline - Date.now(), 0));
    timeoutTimer = setTimeout(finish, Math.max(overallDeadline - Date.now(), 0));
    unsubscribe = subscribeServerEvent(SERVER_EVENT_TYPES.TRANSACTION_IMPORT_STATUS, (record) => {
      eventRevision += 1;
      handle(record);
    });
    const snapshotEventRevision = eventRevision;
    fetch(`${API}/accounts/transaction-import-status`, { signal: statusController.signal })
      .then((resp) => readJsonResponse(resp, {
        label: 'Transaction import status',
        validate: isTransactionImportStatusPayload,
      }))
      .then((payload) => {
        if (settled || eventRevision !== snapshotEventRevision) return;
        handle({ payload });
      })
      .catch(() => {});
  });
}

export { SERVER_EVENT_TYPES };
