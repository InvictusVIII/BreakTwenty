import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchWithTimeout, isClientNetworkFailure } from './syncRequests';

function abortableRequest(signal) {
  return new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => {
      const error = new Error('aborted');
      error.name = 'AbortError';
      reject(error);
    }, { once: true });
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('fetchWithTimeout', () => {
  it('returns the response and removes upstream abort ownership after settlement', async () => {
    vi.useFakeTimers();
    const upstream = new AbortController();
    const removeListener = vi.spyOn(upstream.signal, 'removeEventListener');
    const response = { ok: true };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));

    await expect(fetchWithTimeout('/sync', { signal: upstream.signal }, 1_000))
      .resolves.toBe(response);

    expect(fetch.mock.calls[0][1].signal).not.toBe(upstream.signal);
    expect(removeListener).toHaveBeenCalledWith('abort', expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
  });

  it('aborts at the deadline and reports a sync timeout', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn((_url, { signal }) => abortableRequest(signal)));

    const request = fetchWithTimeout('/sync', {}, 1_000);
    const assertion = expect(request).rejects.toThrow('Sync timed out');
    await vi.advanceTimersByTimeAsync(1_000);

    await assertion;
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('preserves caller cancellation as an AbortError', async () => {
    vi.useFakeTimers();
    const upstream = new AbortController();
    vi.stubGlobal('fetch', vi.fn((_url, { signal }) => abortableRequest(signal)));

    const request = fetchWithTimeout('/sync', { signal: upstream.signal }, 1_000);
    upstream.abort();

    await expect(request).rejects.toMatchObject({
      name: 'AbortError',
      message: 'Sync request was cancelled',
    });
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('isClientNetworkFailure', () => {
  it.each([
    [new TypeError('Failed to fetch'), true],
    [new Error('Network error while syncing'), true],
    [Object.assign(new Error('cancelled'), { name: 'AbortError' }), true],
    [new Error('Provider rejected the credentials'), false],
  ])('classifies %s', (error, expected) => {
    expect(isClientNetworkFailure(error)).toBe(expected);
  });
});
