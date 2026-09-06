const ACTIVE_SYNC_STATUSES = new Set(['queued', 'running']);

export function getActiveSyncActivityKeys(syncActivity) {
  const activities = Array.isArray(syncActivity?.active) ? syncActivity.active : [];
  const keys = new Set();
  activities.forEach((activity) => {
    const status = String(activity?.status || '').trim().toLowerCase();
    if (!ACTIVE_SYNC_STATUSES.has(status)) {
      return;
    }
    const key = String(activity?.activity_id || '').trim();
    if (key) {
      keys.add(key);
    }
  });
  return keys;
}

export function hasSettledSyncActivity(previousKeys, nextKeys) {
  const next = nextKeys instanceof Set ? nextKeys : new Set(nextKeys || []);
  return Array.from(previousKeys || []).some((key) => !next.has(key));
}
