export const AUTO_SYNC_COOLDOWN_MS = 6 * 60 * 60 * 1000;
export const AUTO_SYNC_LAST_TRIGGER_STORAGE_KEY = 'breaktwenty_last_auto_sync';

export function shouldRunStartupAutoSync(
  lastAutoSync,
  nowMs = Date.now(),
  cooldownMs = AUTO_SYNC_COOLDOWN_MS,
) {
  const lastAutoSyncAt = Number(lastAutoSync || '0');
  if (!lastAutoSync || !Number.isFinite(lastAutoSyncAt) || lastAutoSyncAt <= 0) {
    return true;
  }
  return Number(nowMs) - lastAutoSyncAt >= cooldownMs;
}

export function recordAutoSyncTrigger(storage, nowMs = Date.now()) {
  if (!storage || typeof storage.setItem !== 'function') {
    throw new TypeError('Auto-sync cooldown storage is unavailable.');
  }
  const triggeredAt = Number(nowMs);
  if (!Number.isFinite(triggeredAt) || triggeredAt <= 0) {
    throw new RangeError('Auto-sync trigger timestamp is invalid.');
  }
  storage.setItem(AUTO_SYNC_LAST_TRIGGER_STORAGE_KEY, String(triggeredAt));
  return triggeredAt;
}
