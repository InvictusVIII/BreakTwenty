import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  INCOMPLETE_ADD_CLEANUP_RETRY_MS,
  cleanupPendingInstitutionAddOnStartup,
  clearPendingInstitutionAdd,
  confirmPendingInstitutionAdd,
  hasInterruptedInstitutionAdd,
  markInterruptedInstitutionAdd,
  getPendingInstitutionAddProviders,
  markPendingInstitutionAdd,
  startIncompleteInstitutionAddCleanup,
} from './incompleteInstitutionAdds';

function jsonResponse(payload, { ok = true } = {}) {
  return { ok, json: vi.fn().mockResolvedValue(payload) };
}

function deferred() {
  let resolve;
  const promise = new Promise((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('pending institution-add ownership', () => {
  it('lets only the current attempt clear a provider marker', () => {
    const firstAttempt = markPendingInstitutionAdd('wise');
    const currentAttempt = markPendingInstitutionAdd('wise');

    clearPendingInstitutionAdd('wise', firstAttempt);
    expect(getPendingInstitutionAddProviders()).toEqual(['wise']);

    clearPendingInstitutionAdd('wise', currentAttempt);
    expect(getPendingInstitutionAddProviders()).toEqual([]);
  });

  it('does not let stale cleanup completion clear a newer attempt', async () => {
    const cleanupRequest = deferred();
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(cleanupRequest.promise));
    const firstAttempt = markPendingInstitutionAdd('wise');
    const stop = startIncompleteInstitutionAddCleanup('wise', { attemptId: firstAttempt });
    markPendingInstitutionAdd('wise');

    cleanupRequest.resolve(jsonResponse({ removed_institutions: 1, skipped_institutions: 0 }));
    await flushPromises();

    expect(getPendingInstitutionAddProviders()).toEqual(['wise']);
    stop();
  });

  it('retries cleanup until the backend removes the incomplete institution', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(jsonResponse({ removed_institutions: 0, skipped_institutions: 0 }))
      .mockResolvedValueOnce(jsonResponse({ removed_institutions: 1, skipped_institutions: 0 })));
    const attemptId = markPendingInstitutionAdd('questrade');
    const onAttempt = vi.fn();
    const stop = startIncompleteInstitutionAddCleanup('questrade', { attemptId, onAttempt });
    await flushPromises();

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(getPendingInstitutionAddProviders()).toEqual(['questrade']);

    await vi.advanceTimersByTimeAsync(INCOMPLETE_ADD_CLEANUP_RETRY_MS);
    await flushPromises();

    expect(fetch).toHaveBeenCalledTimes(2);
    expect(onAttempt).toHaveBeenNthCalledWith(1, 'questrade', 1);
    expect(onAttempt).toHaveBeenNthCalledWith(2, 'questrade', 2);
    expect(getPendingInstitutionAddProviders()).toEqual([]);
    stop();
  });

  it('tracks interrupted add auth separately from pending cleanup markers', () => {
    const attemptId = markPendingInstitutionAdd('bmo');

    markInterruptedInstitutionAdd('bmo');

    expect(hasInterruptedInstitutionAdd('bmo')).toBe(true);
    clearPendingInstitutionAdd('bmo', attemptId);
    expect(hasInterruptedInstitutionAdd('bmo')).toBe(true);
  });

  it('can clear an interrupted add marker after the first successful cleanup response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({ removed_institutions: 0, skipped_institutions: 0 }),
    ));
    const attemptId = markPendingInstitutionAdd('bmo');
    const stop = startIncompleteInstitutionAddCleanup('bmo', {
      attemptId,
      clearOnFirstOk: true,
    });
    await flushPromises();

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(getPendingInstitutionAddProviders()).toEqual([]);
    stop();
  });

  it('settles a stale startup marker after one authoritative backend cleanup', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({ removed_institutions: 0, skipped_institutions: 0 }),
    ));
    markPendingInstitutionAdd('moomoo');

    await expect(cleanupPendingInstitutionAddOnStartup('moomoo')).resolves.toEqual({
      removed_institutions: 0,
      skipped_institutions: 0,
    });

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(getPendingInstitutionAddProviders()).toEqual([]);
  });

  it('does not let startup cleanup clear a newer add attempt', async () => {
    const cleanupRequest = deferred();
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(cleanupRequest.promise));
    markPendingInstitutionAdd('moomoo');
    const cleanup = cleanupPendingInstitutionAddOnStartup('moomoo');
    markPendingInstitutionAdd('moomoo');

    cleanupRequest.resolve(jsonResponse({ removed_institutions: 0, skipped_institutions: 0 }));
    await cleanup;

    expect(getPendingInstitutionAddProviders()).toEqual(['moomoo']);
  });

  it('confirms an institution with the current numeric identity and source sync', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      status: 'ok',
      confirmed_institutions: 1,
    })));

    await expect(
      confirmPendingInstitutionAdd('bank/name', '42', 'sync-7', 'attempt-7'),
    ).resolves.toBe(true);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/institutions/provider/bank%2Fname/add-confirm'),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          institution_id: 42,
          source_sync_id: 'sync-7',
          attempt_id: 'attempt-7',
        }),
      },
    );
  });
});
