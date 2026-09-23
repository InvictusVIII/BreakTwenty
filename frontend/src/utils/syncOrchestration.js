import { recordAutoSyncTrigger } from './autoSyncCadence';

export function admitAutoSyncRun({
  acquireLease,
  releaseLease,
  storage,
  nowMs,
}) {
  const lease = acquireLease();
  if (!lease) return null;

  try {
    return {
      lease,
      runId: recordAutoSyncTrigger(storage, nowMs === undefined ? Date.now() : nowMs),
    };
  } catch (error) {
    releaseLease(lease);
    throw error;
  }
}

export function startIndependentSyncLanes({ startMoomoo, startBatch }) {
  const moomooPromise = typeof startMoomoo === 'function'
    ? Promise.resolve(startMoomoo())
    : null;
  const batchPromise = typeof startBatch === 'function'
    ? Promise.resolve(startBatch())
    : null;

  return { moomooPromise, batchPromise };
}

export async function fetchLiveAutoSyncInstitutions({ fetchImpl, apiBase }) {
  if (typeof fetchImpl !== 'function') {
    throw new TypeError('Live autosync fetch is unavailable.');
  }
  const response = await fetchImpl(`${apiBase}/institutions/all`);
  const payload = await response.json().catch(() => null);
  if (!response.ok || !Array.isArray(payload)) {
    throw new Error('Live institutions could not be loaded for autosync.');
  }
  return payload;
}
