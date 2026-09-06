import { isPlainObject, readJsonResponse } from './apiResponse';

export const APP_SNAPSHOT_TIMEOUT_MS = 15 * 1000;

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function isNetWorthPoint(value, { current = false } = {}) {
  return (
    isPlainObject(value)
    && (!current || typeof value.currency === 'string' && value.currency.trim() !== '')
    && (value.date === null || typeof value.date === 'string')
    && isFiniteNumber(value.total_assets)
    && isFiniteNumber(value.total_liabilities)
    && isFiniteNumber(value.net_worth)
  );
}

const SNAPSHOT_ENDPOINTS = [
  ['networth', '/networth', (value) => (
    isPlainObject(value)
    && isNetWorthPoint(value.current, { current: true })
    && Array.isArray(value.history)
    && value.history.every((point) => isNetWorthPoint(point))
  )],
  ['accounts', '/accounts', Array.isArray],
  ['institutions', '/institutions', Array.isArray],
  ['allInstitutions', '/institutions/all', Array.isArray],
  ['transactionImportStatus', '/accounts/transaction-import-status', (value) => (
    isPlainObject(value) && Array.isArray(value.institutions)
  )],
  ['syncActivity', '/sync/activity', (value) => (
    isPlainObject(value) && Array.isArray(value.active)
  )],
  ['activeSyncBatches', '/sync/batches/active', (value) => (
    isPlainObject(value) && Array.isArray(value.batches)
  )],
];

export async function fetchAppSnapshot({
  apiBase,
  fetchImpl = fetch,
  signal,
  timeoutMs = APP_SNAPSHOT_TIMEOUT_MS,
} = {}) {
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort(new Error('BreakTwenty backend did not respond.'));
  }, Math.max(0, timeoutMs));
  const abortFromUpstream = () => controller.abort(signal?.reason);
  if (signal?.aborted) {
    abortFromUpstream();
  } else {
    signal?.addEventListener('abort', abortFromUpstream, { once: true });
  }

  try {
    const entries = await Promise.all(SNAPSHOT_ENDPOINTS.map(async ([key, path, validate]) => {
      const response = await fetchImpl(`${apiBase}${path}`, { signal: controller.signal });
      const payload = await readJsonResponse(response, {
        label: `App snapshot ${key}`,
        validate,
      });
      return [key, payload];
    }));
    return Object.fromEntries(entries);
  } catch (error) {
    if (!controller.signal.aborted) controller.abort(error);
    if (timedOut) {
      throw new Error('BreakTwenty backend did not respond.', { cause: error });
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
    signal?.removeEventListener('abort', abortFromUpstream);
  }
}

export function createLatestRequestCoordinator() {
  let generation = 0;
  let activeController = null;

  return {
    begin() {
      activeController?.abort();
      const controller = new AbortController();
      const requestGeneration = ++generation;
      activeController = controller;
      return {
        signal: controller.signal,
        isCurrent: () => requestGeneration === generation,
        finish: () => {
          if (requestGeneration !== generation) return false;
          activeController = null;
          return true;
        },
      };
    },
    cancel() {
      generation += 1;
      activeController?.abort();
      activeController = null;
    },
  };
}

export function shouldRefreshSettledTransactionImports({
  previousActiveKeys,
  nextActiveKeys,
  previousSignature,
  nextSignature,
}) {
  if (previousSignature === nextSignature) return false;
  const nextKeys = nextActiveKeys instanceof Set ? nextActiveKeys : new Set(nextActiveKeys || []);
  return Array.from(previousActiveKeys || []).some((key) => !nextKeys.has(key));
}
