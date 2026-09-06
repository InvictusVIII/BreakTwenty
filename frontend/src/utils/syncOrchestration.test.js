import { describe, expect, it, vi } from 'vitest';

import {
  admitAutoSyncRun,
  startIndependentSyncLanes,
} from './syncOrchestration';

describe('admitAutoSyncRun', () => {
  it('records the cooldown before admitted provider work can begin', () => {
    const order = [];
    const storage = {
      setItem: vi.fn(() => order.push('record')),
    };
    const admission = admitAutoSyncRun({
      acquireLease: () => {
        order.push('lease');
        return 'lease-1';
      },
      releaseLease: vi.fn(),
      storage,
      nowMs: 1_234_567,
    });

    order.push('select-provider');
    order.push('contact-provider');

    expect(admission).toEqual({ lease: 'lease-1', runId: 1_234_567 });
    expect(order).toEqual(['lease', 'record', 'select-provider', 'contact-provider']);
  });

  it('releases the lease when the cooldown cannot be recorded', () => {
    const releaseLease = vi.fn();

    expect(() => admitAutoSyncRun({
      acquireLease: () => 'lease-2',
      releaseLease,
      storage: null,
      nowMs: 1_234_567,
    })).toThrow('Auto-sync cooldown storage is unavailable.');
    expect(releaseLease).toHaveBeenCalledWith('lease-2');
  });
});

describe('startIndependentSyncLanes', () => {
  it('starts Moomoo before the public batch and lets each lane settle independently', async () => {
    const order = [];
    let finishMoomoo;
    const moomooCompletion = new Promise((resolve) => {
      finishMoomoo = resolve;
    });

    const { moomooPromise, batchPromise } = startIndependentSyncLanes({
      startMoomoo: () => {
        order.push('moomoo-start');
        return moomooCompletion;
      },
      startBatch: () => {
        order.push('batch-start');
        return { status: 'blocked' };
      },
    });

    expect(order).toEqual(['moomoo-start', 'batch-start']);
    await expect(batchPromise).resolves.toEqual({ status: 'blocked' });

    let moomooSettled = false;
    moomooPromise.then(() => { moomooSettled = true; });
    await Promise.resolve();
    expect(moomooSettled).toBe(false);

    finishMoomoo({ status: 'ok' });
    await expect(moomooPromise).resolves.toEqual({ status: 'ok' });
  });

  it('omits lanes that have no work', () => {
    expect(startIndependentSyncLanes({})).toEqual({
      moomooPromise: null,
      batchPromise: null,
    });
  });
});
