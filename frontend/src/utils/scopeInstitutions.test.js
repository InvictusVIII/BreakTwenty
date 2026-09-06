import { fetchScopeInstitutions } from './scopeInstitutions';

function response(payload, { ok = true, status = 200 } = {}) {
  return { ok, status, json: vi.fn().mockResolvedValue(payload) };
}

describe('scope institution loading', () => {
  it('loads every institution with accounts from one scoped payload', async () => {
    const fetchImpl = vi.fn(async () => response([
      {
        id: 7,
        name: 'North Bank',
        provider: 'north',
        key: 'institution-7',
        accounts: [{ id: 11, name: 'Chequing' }],
        accountIds: [11],
      },
    ]));

    await expect(fetchScopeInstitutions({ apiBase: 'http://app.test', fetchImpl })).resolves.toEqual([
      {
        id: 7,
        name: 'North Bank',
        provider: 'north',
        key: 'institution-7',
        accounts: [{ id: 11, name: 'Chequing', institution: 'North Bank', institution_id: 7, provider: 'north' }],
        accountIds: [11],
      },
    ]);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledWith('http://app.test/institutions/scope', { signal: undefined });
  });

  it('rejects a failed scope request instead of authoritatively replacing it with empty scope', async () => {
    const fetchImpl = vi.fn(async () => response(
      { detail: 'Accounts unavailable' },
      { ok: false, status: 500 },
    ));

    await expect(fetchScopeInstitutions({ apiBase: 'http://app.test', fetchImpl }))
      .rejects.toMatchObject({ message: 'Accounts unavailable', status: 500 });
  });

  it('rejects malformed institution payloads', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response({ detail: 'not an array' }));

    await expect(fetchScopeInstitutions({ apiBase: 'http://app.test', fetchImpl }))
      .rejects.toMatchObject({ message: expect.stringContaining('unexpected payload') });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
