import { afterEach, describe, expect, it, vi } from 'vitest';

class MockEventSource {
  static instances = [];

  static OPEN = 1;

  constructor(url) {
    this.url = url;
    this.closed = false;
    this.readyState = 0;
    this.onmessage = null;
    this.onerror = null;
    this.listeners = new Map();
    MockEventSource.instances.push(this);
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || new Set();
    handlers.add(handler);
    this.listeners.set(type, handlers);
  }

  removeEventListener(type, handler) {
    this.listeners.get(type)?.delete(handler);
  }

  close() {
    this.closed = true;
  }

  emit(record) {
    this.onmessage?.({ data: JSON.stringify(record) });
  }

  open() {
    this.readyState = MockEventSource.OPEN;
    this.listeners.get('open')?.forEach((handler) => handler());
  }
}

function statusPayload(institution) {
  return { institutions: [institution] };
}

async function loadServerEvents() {
  vi.resetModules();
  MockEventSource.instances = [];
  vi.stubGlobal('EventSource', MockEventSource);
  return import('./serverEvents');
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('reconnectServerEvents', () => {
  it('replaces the stream while retaining existing subscribers', async () => {
    const {
      reconnectServerEvents,
      SERVER_EVENT_TYPES,
      subscribeServerEvent,
    } = await loadServerEvents();
    const handler = vi.fn();
    const unsubscribe = subscribeServerEvent(SERVER_EVENT_TYPES.SYNC_ACTIVITY, handler);
    const original = MockEventSource.instances[0];

    const reconnect = reconnectServerEvents({ timeoutMs: 1_000 });
    const replacement = MockEventSource.instances[1];
    expect(original.closed).toBe(true);
    replacement.open();
    await expect(reconnect).resolves.toEqual({ status: 'connected' });

    replacement.emit({
      type: SERVER_EVENT_TYPES.SYNC_ACTIVITY,
      payload: { active: [] },
    });
    expect(handler).toHaveBeenCalledTimes(1);
    unsubscribe();
    expect(replacement.closed).toBe(true);
  });
});

describe('awaitTransactionImportJob', () => {
  it('rejects malformed initial status snapshots and waits for the appearance deadline', async () => {
    vi.useFakeTimers();
    const { awaitTransactionImportJob } = await loadServerEvents();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ institutions: {} }),
    })));

    const promise = awaitTransactionImportJob({
      provider: 'moomoo',
      jobId: 'malformed-snapshot-job',
      timeoutMs: 60_000,
      appearanceGraceMs: 1_000,
    });
    let resolved = false;
    promise.then(() => { resolved = true; });
    await flushPromises();

    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1_000);
    await promise;
    expect(MockEventSource.instances[0].closed).toBe(true);
  });

  it('resolves when the matched active job disappears from the status payload', async () => {
    const { awaitTransactionImportJob, SERVER_EVENT_TYPES } = await loadServerEvents();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      json: () => Promise.resolve(statusPayload({
        provider: 'moomoo',
        transaction_import_job_id: 'moomoo-job-1',
        transaction_import_job_status: 'running',
      })),
    })));

    const promise = awaitTransactionImportJob({
      provider: 'moomoo',
      jobId: 'moomoo-job-1',
      timeoutMs: 60_000,
      appearanceGraceMs: 30_000,
    });
    let resolved = false;
    promise.then(() => { resolved = true; });
    await flushPromises();

    expect(resolved).toBe(false);
    expect(MockEventSource.instances).toHaveLength(1);

    MockEventSource.instances[0].emit({
      type: SERVER_EVENT_TYPES.TRANSACTION_IMPORT_STATUS,
      payload: statusPayload({
        provider: 'moomoo',
        transaction_import_job_id: 'moomoo-job-1',
        transaction_import_job_status: 'running',
      }),
    });

    MockEventSource.instances[0].emit({
      type: SERVER_EVENT_TYPES.TRANSACTION_IMPORT_STATUS,
      payload: statusPayload({
        provider: 'moomoo',
        transaction_import_job_id: null,
        transaction_import_job_status: null,
      }),
    });

    await promise;
    expect(resolved).toBe(true);
    expect(MockEventSource.instances[0].closed).toBe(true);
  });

  it('resolves at the appearance deadline when no job appears and no SSE event arrives', async () => {
    vi.useFakeTimers();
    const { awaitTransactionImportJob } = await loadServerEvents();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      json: () => Promise.resolve(statusPayload({
        provider: 'moomoo',
        transaction_import_job_id: null,
        transaction_import_job_status: null,
      })),
    })));

    const promise = awaitTransactionImportJob({
      provider: 'moomoo',
      jobId: 'moomoo-job-2',
      timeoutMs: 60_000,
      appearanceGraceMs: 1_000,
    });
    let resolved = false;
    promise.then(() => { resolved = true; });
    await flushPromises();

    expect(resolved).toBe(false);

    vi.advanceTimersByTime(1_001);
    await promise;
    expect(resolved).toBe(true);
    expect(MockEventSource.instances[0].closed).toBe(true);
  });

  it('cancels the appearance timer after the requested job appears', async () => {
    vi.useFakeTimers();
    const { awaitTransactionImportJob, SERVER_EVENT_TYPES } = await loadServerEvents();
    let resolveStatusRequest;
    vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => {
      resolveStatusRequest = resolve;
    })));

    const promise = awaitTransactionImportJob({
      provider: 'moomoo',
      jobId: 'moomoo-job-before-grace',
      timeoutMs: 60_000,
      appearanceGraceMs: 1_000,
    });
    let resolved = false;
    promise.then(() => { resolved = true; });

    MockEventSource.instances[0].emit({
      type: SERVER_EVENT_TYPES.TRANSACTION_IMPORT_STATUS,
      payload: statusPayload({
        provider: 'moomoo',
        transaction_import_job_id: 'moomoo-job-before-grace',
        transaction_import_job_status: 'running',
      }),
    });
    resolveStatusRequest({
      ok: true,
      json: () => Promise.resolve(statusPayload({
        provider: 'moomoo',
        transaction_import_job_id: null,
        transaction_import_job_status: null,
      })),
    });
    await flushPromises();
    await vi.advanceTimersByTimeAsync(1_001);
    expect(resolved).toBe(false);

    MockEventSource.instances[0].emit({
      type: SERVER_EVENT_TYPES.TRANSACTION_IMPORT_STATUS,
      payload: statusPayload({
        provider: 'moomoo',
        transaction_import_job_id: 'moomoo-job-before-grace',
        transaction_import_job_status: 'complete',
      }),
    });

    await promise;
    expect(resolved).toBe(true);
  });

  it('matches the requested provider job in a multi-provider status payload', async () => {
    const { awaitTransactionImportJob, SERVER_EVENT_TYPES } = await loadServerEvents();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      json: () => Promise.resolve({
        institutions: [
          { provider: 'wise', transaction_import_job_id: null },
          {
            provider: 'moomoo',
            transaction_import_job_id: 'moomoo-job-3',
            transaction_import_job_status: 'running',
          },
        ],
      }),
    })));

    const promise = awaitTransactionImportJob({
      provider: 'moomoo',
      jobId: 'moomoo-job-3',
      timeoutMs: 60_000,
      appearanceGraceMs: 30_000,
    });
    let resolved = false;
    promise.then(() => { resolved = true; });
    await flushPromises();

    expect(resolved).toBe(false);

    MockEventSource.instances[0].emit({
      type: SERVER_EVENT_TYPES.TRANSACTION_IMPORT_STATUS,
      payload: {
        institutions: [
          { provider: 'moomoo', transaction_import_job_id: null },
          {
            provider: 'moomoo',
            transaction_import_job_id: 'moomoo-job-3',
            transaction_import_job_status: 'complete',
          },
        ],
      },
    });

    await promise;
    expect(resolved).toBe(true);
  });

  it('releases its request, timers, and subscription at the overall timeout', async () => {
    vi.useFakeTimers();
    const { awaitTransactionImportJob } = await loadServerEvents();
    let requestSignal;
    vi.stubGlobal('fetch', vi.fn((_url, { signal }) => {
      requestSignal = signal;
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(statusPayload({
          provider: 'moomoo',
          transaction_import_job_id: 'long-running-job',
          transaction_import_job_status: 'running',
        })),
      });
    }));

    const promise = awaitTransactionImportJob({
      provider: 'moomoo',
      jobId: 'long-running-job',
      timeoutMs: 2_000,
      appearanceGraceMs: 500,
    });
    let resolved = false;
    promise.then(() => { resolved = true; });
    await flushPromises();

    await vi.advanceTimersByTimeAsync(501);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1_499);
    await promise;

    expect(requestSignal.aborted).toBe(true);
    expect(MockEventSource.instances[0].closed).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('sync batch subscriptions', () => {
  it('shares one stream and closes it only after the last subscriber leaves', async () => {
    const {
      subscribeBatchEvents,
      subscribeServerEvent,
      SERVER_EVENT_TYPES,
    } = await loadServerEvents();
    const unsubscribeGlobal = subscribeServerEvent(SERVER_EVENT_TYPES.SYNC_ACTIVITY, vi.fn());
    const unsubscribeBatch = subscribeBatchEvents('batch-shared', vi.fn());

    expect(MockEventSource.instances).toHaveLength(1);
    unsubscribeGlobal();
    expect(MockEventSource.instances[0].closed).toBe(false);

    unsubscribeBatch();
    expect(MockEventSource.instances[0].closed).toBe(true);
  });

  it('routes only the canonical top-level batch identity', async () => {
    const { subscribeBatchEvents, SERVER_EVENT_TYPES } = await loadServerEvents();
    const handler = vi.fn();
    const unsubscribe = subscribeBatchEvents('batch-1', handler);

    MockEventSource.instances[0].emit({
      type: SERVER_EVENT_TYPES.SYNC_BATCH,
      batch_id: 'batch-1',
      payload: { batch_id: 'batch-1', status: 'running' },
    });
    MockEventSource.instances[0].emit({
      type: SERVER_EVENT_TYPES.SYNC_BATCH,
      payload: { batch_id: 'batch-1', status: 'running' },
    });

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith({ batch_id: 'batch-1', status: 'running' });
    unsubscribe();
  });
});
