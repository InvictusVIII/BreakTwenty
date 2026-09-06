import {
  afterEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

import {
  fetchActiveSyncBatches,
  fetchSyncBatch,
  forgetMonitoredSyncBatchId,
  getActiveSyncBatchConnectionStates,
  getActiveSyncBatchInstitutionIds,
  getSyncNetworkBlocker,
  hasActiveManualSyncBatch,
  isTerminalSyncBatch,
  loadMonitoredSyncBatchIds,
  reconcileActiveSyncBatches,
  rememberMonitoredSyncBatchId,
  runSyncBatchUntilDone,
  startSyncBatch,
} from './syncBatch';

function jsonResponse(payload, { ok = true } = {}) {
  return {
    ok,
    json: async () => payload,
  };
}

class MockEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.closed = false;
    this.onmessage = null;
    MockEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  emit(record) {
    this.onmessage?.({ data: JSON.stringify(record) });
  }
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  globalThis.localStorage?.clear();
});

describe('runSyncBatchUntilDone', () => {
  it('reconciles terminal status when the completion SSE event is missed', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'started', batch_id: 'batch-1' }))
      .mockResolvedValueOnce(jsonResponse({ status: 'running', batch_id: 'batch-1', results: {} }))
      .mockResolvedValueOnce(jsonResponse({
        status: 'done',
        batch_id: 'batch-1',
        results: {
          'connection:42': {
            status: 'ok', provider: 'questrade', institution_id: 42,
          },
        },
      }));
    vi.stubGlobal('fetch', fetchMock);
    const onConnectionResult = vi.fn();

    const completion = runSyncBatchUntilDone({
      connections: [{ provider: 'questrade', institution_id: 42 }],
      mode: 'auto',
      onConnectionResult,
    });
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersToNextTimerAsync();

    await expect(completion).resolves.toMatchObject({
      status: 'done',
      batch_id: 'batch-1',
    });
    expect(loadMonitoredSyncBatchIds()).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(onConnectionResult).toHaveBeenCalledWith('connection:42', {
      status: 'ok', provider: 'questrade', institution_id: 42,
    });
  });

  it('returns a typed error and releases polling when the monitor deadline expires', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'started', batch_id: 'batch-timeout' }))
      .mockResolvedValueOnce(jsonResponse({ status: 'running', batch_id: 'batch-timeout', results: {} }));
    vi.stubGlobal('fetch', fetchMock);

    const completion = runSyncBatchUntilDone({
      connections: [{ provider: 'wise', institution_id: 7 }],
      mode: 'manual',
      timeoutMs: 1_000,
    });
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1_000);

    await expect(completion).resolves.toEqual({
      status: 'error',
      code: 'sync_batch_timeout',
      batch_id: 'batch-timeout',
      message: 'Sync batch monitoring timed out.',
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(loadMonitoredSyncBatchIds()).toEqual(['batch-timeout']);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('still reaches the monitor deadline when every reconciliation request fails', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'started', batch_id: 'batch-offline' }))
      .mockRejectedValue(new Error('offline'));
    vi.stubGlobal('fetch', fetchMock);

    const completion = runSyncBatchUntilDone({
      connections: [{ provider: 'wise', institution_id: 7 }],
      mode: 'manual',
      timeoutMs: 3_100,
    });
    await vi.advanceTimersByTimeAsync(3_100);

    await expect(completion).resolves.toMatchObject({
      status: 'error',
      code: 'sync_batch_timeout',
      batch_id: 'batch-offline',
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(loadMonitoredSyncBatchIds()).toEqual(['batch-offline']);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancels active reconciliation and releases polling when the caller aborts', async () => {
    vi.useFakeTimers();
    let reconcileSignal = null;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'started', batch_id: 'batch-cancel' }))
      .mockImplementationOnce((_url, options) => new Promise((_resolve, reject) => {
        reconcileSignal = options.signal;
        options.signal.addEventListener('abort', () => {
          const error = new Error('aborted');
          error.name = 'AbortError';
          reject(error);
        }, { once: true });
      }));
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    const completion = runSyncBatchUntilDone({
      connections: [{ provider: 'wise', institution_id: 7 }],
      mode: 'manual',
      signal: controller.signal,
    });
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();
    await vi.advanceTimersByTimeAsync(0);

    await expect(completion).resolves.toEqual({
      status: 'error',
      code: 'sync_batch_cancelled',
      batch_id: 'batch-cancel',
      message: 'Sync batch monitoring was cancelled.',
    });
    expect(reconcileSignal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('finishes from the canonical batch SSE event and applies only actionable results', async () => {
    MockEventSource.instances = [];
    vi.stubGlobal('EventSource', MockEventSource);
    let reconcileSignal;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'started', batch_id: 'batch-sse' }))
      .mockImplementationOnce((_url, { signal }) => new Promise((_resolve, reject) => {
        reconcileSignal = signal;
        signal.addEventListener('abort', () => {
          const error = new Error('aborted');
          error.name = 'AbortError';
          reject(error);
        }, { once: true });
      }));
    vi.stubGlobal('fetch', fetchMock);
    const onConnectionResult = vi.fn();

    const completion = runSyncBatchUntilDone({
      connections: [{ provider: 'questrade', institution_id: 42 }],
      mode: 'manual',
      onConnectionResult,
    });

    await vi.waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    MockEventSource.instances[0].emit({
      type: 'sync_batch',
      batch_id: 'batch-sse',
      payload: {
        status: 'done',
        batch_id: 'batch-sse',
        results: {
          'connection:42': { status: 'ok', institution_id: 42 },
          'connection:99': {
            status: 'network_blocked',
            code: 'dns_resolution_temporarily_unavailable',
          },
        },
      },
    });

    await expect(completion).resolves.toMatchObject({ status: 'done', batch_id: 'batch-sse' });
    expect(onConnectionResult).toHaveBeenCalledTimes(1);
    expect(onConnectionResult).toHaveBeenCalledWith(
      'connection:42',
      { status: 'ok', institution_id: 42 },
    );
    expect(reconcileSignal.aborted).toBe(true);
    expect(MockEventSource.instances[0].closed).toBe(true);
  });
});

describe('sync batch response validation', () => {
  it('rejects non-success start and polling responses with the server detail', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ detail: 'Batch start unavailable' }, { ok: false }))
      .mockResolvedValueOnce(jsonResponse({ message: 'Batch lookup unavailable' }, { ok: false }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(startSyncBatch([], 'auto')).rejects.toThrow('Batch start unavailable');
    await expect(fetchSyncBatch('batch-failed')).rejects.toThrow('Batch lookup unavailable');
  });

  it('requires the canonical active-batch envelope', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ batches: [{ batch_id: 'batch-1' }] }))
      .mockResolvedValueOnce(jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await expect(fetchActiveSyncBatches()).resolves.toEqual([{ batch_id: 'batch-1' }]);
    await expect(fetchActiveSyncBatches()).rejects.toThrow('Failed to read active sync batches.');
  });
});

describe('durable active batch state', () => {
  const runningBatch = {
    batch_id: 'batch-running',
    mode: 'manual',
    status: 'running',
    results: {
      'connection:42': { institution_id: 42, status: 'syncing' },
      'connection:43': { institution_id: 43, status: 'pending' },
      'connection:44': { institution_id: 44, status: 'ok' },
    },
  };

  it('upserts active snapshots and removes terminal batches', () => {
    expect(reconcileActiveSyncBatches([], runningBatch)).toEqual([runningBatch]);
    expect(reconcileActiveSyncBatches([runningBatch], {
      ...runningBatch,
      status: 'done',
    })).toEqual([]);
  });

  it('derives active connection and manual-batch state from server snapshots', () => {
    expect(getActiveSyncBatchInstitutionIds([runningBatch])).toEqual(new Set([42, 43]));
    expect(hasActiveManualSyncBatch([runningBatch])).toBe(true);
    expect(hasActiveManualSyncBatch([{ ...runningBatch, mode: 'auto' }])).toBe(false);
    expect(isTerminalSyncBatch({ status: 'done' })).toBe(true);
    expect(isTerminalSyncBatch(runningBatch)).toBe(false);
  });

  it('keeps per-connection active batch statuses, including terminal results in a running batch', () => {
    const states = getActiveSyncBatchConnectionStates([
      {
        ...runningBatch,
        results: {
          'connection:42': { institution_id: 42, provider: 'wise', status: 'syncing' },
          'connection:43': { institution_id: 43, provider: 'rbc', status: 'auth_required' },
          'connection:44': { institution_id: 44, provider: 'coinbase', status: 'ok' },
        },
      },
      {
        batch_id: 'finished-batch',
        status: 'done',
        results: {
          'connection:45': { institution_id: 45, provider: 'td', status: 'ok' },
        },
      },
    ]);

    expect([...states.keys()]).toEqual([42, 43, 44]);
    expect(states.get(42)).toMatchObject({
      status: 'syncing',
      institution_id: 42,
      batch_id: 'batch-running',
      batch_status: 'running',
      connection_key: 'connection:42',
    });
    expect(states.get(43)).toMatchObject({ status: 'auth_required', institution_id: 43 });
    expect(states.get(44)).toMatchObject({ status: 'ok', institution_id: 44 });
  });

  it('persists only bounded safe batch ids for terminal recovery after reload', () => {
    const values = new Map();
    const storage = {
      getItem: (key) => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key),
    };

    rememberMonitoredSyncBatchId('syncbatch-a1b2c3d4e5f6', storage);
    rememberMonitoredSyncBatchId('../unsafe', storage);
    rememberMonitoredSyncBatchId('syncbatch-fedcba654321', storage);
    expect(loadMonitoredSyncBatchIds(storage)).toEqual([
      'syncbatch-a1b2c3d4e5f6',
      'syncbatch-fedcba654321',
    ]);

    forgetMonitoredSyncBatchId('syncbatch-a1b2c3d4e5f6', storage);
    expect(loadMonitoredSyncBatchIds(storage)).toEqual(['syncbatch-fedcba654321']);
  });
});

describe('getSyncNetworkBlocker', () => {
  it('normalizes a blocked batch into the global Accounts notice', () => {
    const blocker = getSyncNetworkBlocker({
      status: 'blocked',
      blocker: {
        code: 'dns_resolution_temporarily_unavailable',
        message: 'Resolver unavailable.',
      },
    });

    expect(blocker).toEqual({
      code: 'dns_resolution_temporarily_unavailable',
      title: 'Sync paused',
      message: 'Resolver unavailable.',
    });
  });

  it('does not turn provider network errors into a global DNS notice', () => {
    expect(getSyncNetworkBlocker({
      status: 'network_error',
      message: 'Connection failed',
    })).toBeNull();
  });

  it('normalizes a per-connection network-blocked result', () => {
    expect(getSyncNetworkBlocker({
      status: 'network_blocked',
      code: 'dns_resolution_temporarily_unavailable',
      message: 'Resolver unavailable for this connection.',
    })).toEqual({
      code: 'dns_resolution_temporarily_unavailable',
      title: 'Sync paused',
      message: 'Resolver unavailable for this connection.',
    });
  });
});
