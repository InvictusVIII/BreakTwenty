import {
  persistVisibilityScope,
  reconcileVisibilityScope,
  scopeWithSelectedAccounts,
  selectedAccountIdsFromScope,
} from './visibilityScope';

const institutions = [{
  id: 1,
  hidden: false,
  accounts: [
    { id: 10, hidden: false },
    { id: 11, hidden: true },
  ],
}];

describe('visibility scope persistence', () => {
  it('checks every institution and account response', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({ status: 'ok' }),
    });
    await persistVisibilityScope({ apiBase: '/api', institutions, fetchImpl });

    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(fetchImpl).toHaveBeenNthCalledWith(1, '/api/institutions/1/hidden', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ hidden: false }),
    }));
  });

  it('waits for every update and rejects partial HTTP failure', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ status: 'ok' }) })
      .mockResolvedValueOnce({ ok: false, status: 500, json: vi.fn().mockResolvedValue({ detail: 'Account write failed' }) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ status: 'ok' }) });

    await expect(persistVisibilityScope({ apiBase: '/api', institutions, fetchImpl }))
      .rejects.toThrow('Account write failed');
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it('rejects a semantic failure returned with HTTP 200', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({ status: 'error', message: 'Not found' }),
    });

    await expect(persistVisibilityScope({ apiBase: '/api', institutions, fetchImpl }))
      .rejects.toThrow('unexpected payload');
  });

  it('only writes changed visibility rows when previous institutions are supplied', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({ status: 'ok' }),
    });
    const nextInstitutions = [{
      id: 1,
      hidden: false,
      accounts: [
        { id: 10, hidden: true },
        { id: 11, hidden: true },
      ],
    }];

    await persistVisibilityScope({
      apiBase: '/api',
      institutions: nextInstitutions,
      previousInstitutions: institutions,
      fetchImpl,
    });

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledWith('/api/accounts/10/hidden', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ hidden: true }),
    }));
  });

  it('times out a hung visibility update', async () => {
    vi.useFakeTimers();
    try {
      const fetchImpl = vi.fn(() => new Promise(() => {}));

      const promise = persistVisibilityScope({
        apiBase: '/api',
        institutions,
        fetchImpl,
        timeoutMs: 25,
      });
      await Promise.resolve();
      vi.advanceTimersByTime(25);

      await expect(promise).rejects.toThrow('timed out');
    } finally {
      vi.useRealTimers();
    }
  });

  it('derives authoritative hidden flags from selected account ids', () => {
    expect(scopeWithSelectedAccounts(institutions, new Set([11]))).toEqual([{
      id: 1,
      hidden: false,
      accounts: [
        { id: 10, hidden: true },
        { id: 11, hidden: false },
      ],
    }]);
    expect(scopeWithSelectedAccounts(institutions, new Set())).toEqual([{
      id: 1,
      hidden: true,
      accounts: [
        { id: 10, hidden: true },
        { id: 11, hidden: true },
      ],
    }]);
  });

  it('derives selected account ids from authoritative scope flags', () => {
    expect([...selectedAccountIdsFromScope(institutions)]).toEqual([10]);
  });

  it('refreshes root data before returning the authoritative scope after a partial write', async () => {
    const calls = [];
    const authoritative = [{ id: 2, hidden: true, accounts: [] }];
    const result = await reconcileVisibilityScope({
      onDataChange: async () => { calls.push('data'); },
      fetchAllScopeInstitutions: async () => {
        calls.push('scope');
        return authoritative;
      },
    });

    expect(calls).toEqual(['data', 'scope']);
    expect(result).toBe(authoritative);
  });

  it('returns null when authoritative scope reconciliation fails', async () => {
    await expect(reconcileVisibilityScope({
      onDataChange: async () => { throw new Error('root failed'); },
      fetchAllScopeInstitutions: async () => { throw new Error('scope failed'); },
    })).resolves.toBeNull();
  });
});
