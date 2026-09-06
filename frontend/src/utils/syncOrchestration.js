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
