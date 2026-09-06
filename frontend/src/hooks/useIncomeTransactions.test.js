import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import useIncomeTransactions from './useIncomeTransactions';

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return { ok, status, json: vi.fn().mockResolvedValue(payload) };
}

function deferred() {
  let resolve;
  const promise = new Promise((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

describe('useIncomeTransactions', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('does not request Income transactions until the tab is active', async () => {
    fetch.mockResolvedValue(jsonResponse({ transactions: [], total: 0 }));
    const { rerender, result } = renderHook(
      ({ active }) => useIncomeTransactions({ active, currency: 'CAD' }),
      { initialProps: { active: false } },
    );

    expect(fetch).not.toHaveBeenCalled();
    expect(result.current.incomeLoading).toBe(false);

    rerender({ active: true });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.incomeTransactions).toEqual([]));

    rerender({ active: false });
    rerender({ active: true });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('lets only the latest currency request own the result', async () => {
    const cadRequest = deferred();
    const usdRequest = deferred();
    fetch
      .mockReturnValueOnce(cadRequest.promise)
      .mockReturnValueOnce(usdRequest.promise);
    const { rerender, result } = renderHook(
      ({ currency }) => useIncomeTransactions({ active: true, currency }),
      { initialProps: { currency: 'CAD' } },
    );

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const cadSignal = fetch.mock.calls[0][1].signal;
    rerender({ currency: 'USD' });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(cadSignal.aborted).toBe(true);

    await act(async () => {
      usdRequest.resolve(jsonResponse({ transactions: [{ id: 'usd' }], total: 1 }));
    });
    await waitFor(() => expect(result.current.incomeTransactions).toEqual([{ id: 'usd' }]));

    await act(async () => {
      cadRequest.resolve(jsonResponse({ transactions: [{ id: 'cad' }], total: 1 }));
    });
    expect(result.current.incomeTransactions).toEqual([{ id: 'usd' }]);
  });

  it('restarts pending work when the account resource key changes', async () => {
    const oldAccountsRequest = deferred();
    const newAccountsRequest = deferred();
    fetch
      .mockReturnValueOnce(oldAccountsRequest.promise)
      .mockReturnValueOnce(newAccountsRequest.promise);
    const { rerender, result } = renderHook(
      ({ resourceKey }) => useIncomeTransactions({ active: true, currency: 'CAD', resourceKey }),
      { initialProps: { resourceKey: 'accounts-v1' } },
    );

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const oldSignal = fetch.mock.calls[0][1].signal;
    rerender({ resourceKey: 'accounts-v2' });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(oldSignal.aborted).toBe(true);
    expect(result.current.incomeTransactions).toBeNull();

    await act(async () => {
      newAccountsRequest.resolve(jsonResponse({ transactions: [{ id: 'current' }], total: 1 }));
    });
    await waitFor(() => expect(result.current.incomeTransactions).toEqual([{ id: 'current' }]));

    await act(async () => {
      oldAccountsRequest.resolve(jsonResponse({ transactions: [{ id: 'stale' }], total: 1 }));
    });
    expect(result.current.incomeTransactions).toEqual([{ id: 'current' }]);
  });

  it('keeps failures retryable instead of caching them as an empty result', async () => {
    fetch
      .mockResolvedValueOnce(jsonResponse(
        { detail: 'Income service unavailable' },
        { ok: false, status: 503 },
      ))
      .mockResolvedValueOnce(jsonResponse({ transactions: [], total: 0 }));
    const { result, rerender } = renderHook(
      () => useIncomeTransactions({ active: true, currency: 'CAD' }),
    );

    await waitFor(() => expect(result.current.incomeError).toBe('Income service unavailable'));
    expect(result.current.incomeTransactions).toBeNull();
    expect(result.current.incomeLoading).toBe(false);

    act(() => result.current.retryIncomeTransactions());
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.incomeTransactions).toEqual([]));
    expect(result.current.incomeError).toBe('');

    rerender();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('aborts the active request when its owner unmounts', async () => {
    const request = deferred();
    fetch.mockReturnValue(request.promise);
    const { unmount } = renderHook(
      () => useIncomeTransactions({ active: true, currency: 'CAD' }),
    );

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const signal = fetch.mock.calls[0][1].signal;

    unmount();

    expect(signal.aborted).toBe(true);
    await act(async () => {
      request.resolve(jsonResponse({ transactions: [{ id: 'late' }], total: 1 }));
    });
  });
});
