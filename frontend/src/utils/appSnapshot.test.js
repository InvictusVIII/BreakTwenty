import {
  createLatestRequestCoordinator,
  fetchAppSnapshot,
  shouldRefreshSettledTransactionImports,
} from './appSnapshot';

const validPayloads = {
  '/networth': {
    current: {
      total_assets: 0,
      total_liabilities: 0,
      net_worth: 0,
      currency: 'CAD',
      date: null,
    },
    history: [],
  },
  '/accounts': [],
  '/institutions': [],
  '/institutions/all': [],
  '/accounts/transaction-import-status': { institutions: [] },
  '/sync/activity': { active: [] },
  '/sync/batches/active': { batches: [] },
};

function successfulFetch(overrides = {}) {
  return vi.fn(async (url) => {
    const path = new URL(url).pathname;
    const payload = Object.prototype.hasOwnProperty.call(overrides, path)
      ? overrides[path]
      : validPayloads[path];
    return {
      ok: true,
      status: 200,
      json: async () => payload,
    };
  });
}

describe('app snapshot loading', () => {
  it('loads and validates every root snapshot endpoint', async () => {
    const fetchImpl = successfulFetch();
    const snapshot = await fetchAppSnapshot({ apiBase: 'http://app.test', fetchImpl });

    expect(snapshot).toMatchObject({
      accounts: [],
      institutions: [],
      allInstitutions: [],
      transactionImportStatus: { institutions: [] },
      syncActivity: { active: [] },
      activeSyncBatches: { batches: [] },
    });
    expect(snapshot).not.toHaveProperty('allocation');
    expect(fetchImpl).toHaveBeenCalledTimes(7);
    expect(fetchImpl.mock.calls.some(([url]) => new URL(url).pathname === '/allocation')).toBe(false);
  });

  it('rejects malformed successful payloads instead of committing false-empty data', async () => {
    await expect(fetchAppSnapshot({
      apiBase: 'http://app.test',
      fetchImpl: successfulFetch({ '/accounts': { detail: 'wrong shape' } }),
    })).rejects.toMatchObject({ message: expect.stringContaining('unexpected payload') });

    await expect(fetchAppSnapshot({
      apiBase: 'http://app.test',
      fetchImpl: successfulFetch({
        '/networth': { current: { net_worth: 0 }, history: [] },
      }),
    })).rejects.toMatchObject({ message: expect.stringContaining('unexpected payload') });

    await expect(fetchAppSnapshot({
      apiBase: 'http://app.test',
      fetchImpl: successfulFetch({
        '/networth': {
          ...validPayloads['/networth'],
          history: [{ date: '2026-08-13', net_worth: 0 }],
        },
      }),
    })).rejects.toMatchObject({ message: expect.stringContaining('unexpected payload') });
  });

  it('rejects non-2xx JSON with the server detail', async () => {
    const fetchImpl = successfulFetch();
    fetchImpl.mockImplementation(async (url) => {
      if (new URL(url).pathname === '/institutions') {
        return { ok: false, status: 500, json: async () => ({ detail: 'Institutions unavailable' }) };
      }
      const path = new URL(url).pathname;
      return { ok: true, status: 200, json: async () => validPayloads[path] };
    });

    await expect(fetchAppSnapshot({ apiBase: 'http://app.test', fetchImpl }))
      .rejects.toMatchObject({ message: 'Institutions unavailable', status: 500 });
  });

  it('aborts sibling requests on endpoint failure and removes its upstream listener', async () => {
    const upstreamController = new AbortController();
    const addListener = vi.spyOn(upstreamController.signal, 'addEventListener');
    const removeListener = vi.spyOn(upstreamController.signal, 'removeEventListener');
    const requestSignals = [];
    const fetchImpl = vi.fn((url, { signal }) => {
      requestSignals.push(signal);
      if (new URL(url).pathname === '/accounts') {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: async () => ({ detail: 'Accounts unavailable' }),
        });
      }
      return new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });

    await expect(fetchAppSnapshot({
      apiBase: 'http://app.test',
      fetchImpl,
      signal: upstreamController.signal,
    })).rejects.toMatchObject({ message: 'Accounts unavailable', status: 503 });

    expect(fetchImpl).toHaveBeenCalledTimes(7);
    expect(new Set(requestSignals).size).toBe(1);
    expect(requestSignals[0].aborted).toBe(true);
    const upstreamListener = addListener.mock.calls.find(([type]) => type === 'abort')?.[1];
    expect(removeListener).toHaveBeenCalledWith('abort', upstreamListener);
  });

  it('fails a stalled initial snapshot instead of leaving the app loader forever', async () => {
    vi.useFakeTimers();
    const fetchImpl = vi.fn((_url, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), { once: true });
    }));

    const snapshot = fetchAppSnapshot({
      apiBase: 'http://app.test',
      fetchImpl,
      timeoutMs: 1000,
    });
    const rejection = expect(snapshot).rejects.toThrow('backend did not respond');
    await vi.advanceTimersByTimeAsync(1000);

    await rejection;
    vi.useRealTimers();
  });
});

describe('latest-request coordination', () => {
  it('aborts the previous request and gives loading ownership only to the latest request', () => {
    const coordinator = createLatestRequestCoordinator();
    const first = coordinator.begin();
    const second = coordinator.begin();

    expect(first.signal.aborted).toBe(true);
    expect(first.isCurrent()).toBe(false);
    expect(first.finish()).toBe(false);
    expect(second.signal.aborted).toBe(false);
    expect(second.isCurrent()).toBe(true);
    expect(second.finish()).toBe(true);
  });

  it('cancels the active request and revokes its ownership', () => {
    const coordinator = createLatestRequestCoordinator();
    const request = coordinator.begin();

    coordinator.cancel();

    expect(request.signal.aborted).toBe(true);
    expect(request.isCurrent()).toBe(false);
    expect(request.finish()).toBe(false);
  });
});

describe('transaction-import settlement', () => {
  it('decides active-to-terminal refresh synchronously from refs and payloads', () => {
    expect(shouldRefreshSettledTransactionImports({
      previousActiveKeys: new Set(['questrade:7']),
      nextActiveKeys: new Set(),
      previousSignature: 'running',
      nextSignature: 'complete',
    })).toBe(true);
    expect(shouldRefreshSettledTransactionImports({
      previousActiveKeys: new Set(['questrade:7']),
      nextActiveKeys: new Set(['questrade:7']),
      previousSignature: 'running',
      nextSignature: 'running-new-timestamp',
    })).toBe(false);
  });
});
