import { describe, expect, it, vi } from 'vitest';

import {
  AUTO_SYNC_COOLDOWN_MS,
  AUTO_SYNC_LAST_TRIGGER_STORAGE_KEY,
  recordAutoSyncTrigger,
  shouldRunStartupAutoSync,
} from './autoSyncCadence';

describe('autosync cadence', () => {
  it('enforces a strict six-hour startup cooldown', () => {
    const lastTriggeredAt = 1_000_000;

    expect(shouldRunStartupAutoSync(
      String(lastTriggeredAt),
      lastTriggeredAt + AUTO_SYNC_COOLDOWN_MS - 1,
    )).toBe(false);
    expect(shouldRunStartupAutoSync(
      String(lastTriggeredAt),
      lastTriggeredAt + AUTO_SYNC_COOLDOWN_MS,
    )).toBe(true);
  });

  it('runs when no valid prior trigger was recorded', () => {
    expect(shouldRunStartupAutoSync(null, 1_000_000)).toBe(true);
    expect(shouldRunStartupAutoSync('invalid', 1_000_000)).toBe(true);
  });

  it('persists the trigger before autosync work is launched', () => {
    const storage = { setItem: vi.fn() };

    expect(recordAutoSyncTrigger(storage, 1_234_567)).toBe(1_234_567);
    expect(storage.setItem).toHaveBeenCalledWith(
      AUTO_SYNC_LAST_TRIGGER_STORAGE_KEY,
      '1234567',
    );
  });
});
