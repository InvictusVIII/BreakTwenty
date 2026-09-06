import React from 'react';
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ScraperAuthModal from './ScraperAuthModal';

vi.mock('./InstitutionLogo', () => ({
  default: ({ name }) => <span>{name}</span>,
}));

afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  delete window.breaktwentyDesktop;
});

function response(payload, { ok = true } = {}) {
  return {
    ok,
    json: vi.fn().mockResolvedValue(payload),
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

describe('ScraperAuthModal provider flow boundaries', () => {
  it('does not fall back to backend credential routes when desktop visible auth is unavailable', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      json: vi.fn().mockResolvedValue({}),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <ScraperAuthModal
        institution={{ id: 17, name: 'BMO', provider: 'bmo' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
        skipAutoLogin={true}
      />,
    );

    expect(await screen.findByText('Desktop visible auth is not available.')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/sync/bmo/login'))).toBe(false);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/sync/bmo/2fa'))).toBe(false);
  });

  it('keeps saved Wealthsimple credentials out of renderer state during skip-auto-login review', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: vi.fn().mockResolvedValue({
        status: 'ok',
        has_saved_credentials: true,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <ScraperAuthModal
        institution={{ id: 23, name: 'Wealthsimple', provider: 'wealthsimple' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
        skipAutoLogin={true}
      />,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [usernameInput, passwordInput] = document.querySelectorAll('.settings-field input');
    expect(usernameInput.value).toBe('');
    expect(passwordInput.value).toBe('');
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/sync/wealthsimple/login'))).toBe(false);
  });

  it('hands a successful desktop visible-auth attempt back to the current sync contract', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const launch = vi.fn().mockResolvedValue({
      requestStatus: 'launched',
      status: 'running',
      attemptId: 'attempt-17',
    });
    const status = vi.fn().mockResolvedValue({
      status: 'succeeded',
      resultData: {
        authenticated: true,
        responseStatus: 'handoff_ready',
        syncId: 'sync-17',
      },
    });
    const cancel = vi.fn();
    window.breaktwentyDesktop = { visibleAuth: { cancel, launch, status } };
    const onClose = vi.fn();
    const onSuccess = vi.fn();
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return Promise.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      }
      if (String(url).endsWith('/sync/bmo') && options.method === 'POST') {
        return Promise.resolve(response({ status: 'ok', institution_id: 17, sync_id: 'sync-17' }));
      }
      if (String(url).endsWith('/sync/visible-auth-attempt-cleanup')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <ScraperAuthModal
        institution={{ id: 17, name: 'BMO', provider: 'bmo' }}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    );

    await act(async () => {
      await vi.waitFor(() => expect(launch).toHaveBeenCalledOnce());
    });
    expect(launch).toHaveBeenCalledWith(expect.objectContaining({
      provider: 'bmo',
      addFlow: false,
      institutionId: 17,
    }));
    expect(status).toHaveBeenCalledWith({ provider: 'bmo', attemptId: 'attempt-17' });

    await act(async () => {
      await vi.waitFor(() => {
        expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/sync/bmo'))).toBe(true);
      });
    });
    const syncCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/sync/bmo'));
    expect(JSON.parse(syncCall[1].body)).toEqual({
      sync_id: 'sync-17',
      attempt_id: 'attempt-17',
      institution_id: 17,
      add_flow: false,
    });
    expect(cancel).not.toHaveBeenCalled();

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('BMO synced successfully')).toBeInTheDocument();
    expect(screen.getByText('✓')).toBeInTheDocument();
    expect(onSuccess).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });

    expect(onSuccess).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('cancels the active desktop visible-auth attempt with its attempt id', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const launch = vi.fn().mockResolvedValue({
      requestStatus: 'launched',
      status: 'running',
      attemptId: 'attempt-cancel',
    });
    const status = vi.fn().mockResolvedValue({ status: 'running', message: 'Secure browser is running.' });
    const cancel = vi.fn().mockResolvedValue({ status: 'cancelled' });
    window.breaktwentyDesktop = { visibleAuth: { cancel, launch, status } };
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return Promise.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      }
      if (String(url).endsWith('/settings/support-logs/client-event')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      if (String(url).endsWith('/sync/visible-auth-attempt-cleanup')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      if (String(url).endsWith('/institutions/provider/bmo/state') && options.method === 'DELETE') {
        return Promise.resolve(response({ status: 'ok', deleted: true }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    const onClose = vi.fn();

    render(
      <ScraperAuthModal
        institution={{ name: 'BMO', provider: 'bmo', isNew: true }}
        onClose={onClose}
        onSuccess={vi.fn()}
      />,
    );

    await waitFor(() => expect(launch).toHaveBeenCalledOnce());
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel login' }));

    await waitFor(() => {
      expect(cancel).toHaveBeenCalledWith({ provider: 'bmo', attemptId: 'attempt-cancel' });
    });
    expect(fetchMock.mock.calls.some(([url, options]) => (
      String(url).endsWith('/settings/support-logs/client-event')
      && JSON.parse(options.body).attempt_id === 'attempt-cancel'
    ))).toBe(true);
    expect(fetchMock.mock.calls.some(([url, options]) => (
      String(url).endsWith('/sync/visible-auth-attempt-cleanup')
      && JSON.parse(options.body).attempt_id === 'attempt-cancel'
    ))).toBe(true);
    expect(screen.getByText('Adding BMO was interrupted')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('shows interrupted and does not relaunch after the secure browser is closed during add', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const launch = vi.fn().mockResolvedValue({
      requestStatus: 'launched',
      status: 'running',
      attemptId: 'attempt-browser-closed',
    });
    const status = vi.fn().mockResolvedValue({
      status: 'failed',
      message: 'TargetClosedError: Target page, context or browser has been closed',
    });
    const cancel = vi.fn();
    window.breaktwentyDesktop = { visibleAuth: { cancel, launch, status } };
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return Promise.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      }
      if (String(url).endsWith('/sync/visible-auth-attempt-cleanup')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      if (String(url).endsWith('/institutions/provider/bmo/state') && options.method === 'DELETE') {
        return Promise.resolve(response({ status: 'ok', removed_institutions: 0, skipped_institutions: 0 }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const firstView = render(
      <ScraperAuthModal
        institution={{ name: 'BMO', provider: 'bmo', isNew: true }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(await screen.findByText('Adding BMO was interrupted')).toBeInTheDocument();
    expect(launch).toHaveBeenCalledOnce();
    expect(status).toHaveBeenCalledWith({ provider: 'bmo', attemptId: 'attempt-browser-closed' });
    firstView.unmount();

    render(
      <ScraperAuthModal
        institution={{ name: 'BMO', provider: 'bmo', isNew: true }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(await screen.findByText('Adding BMO was interrupted')).toBeInTheDocument();
    expect(launch).toHaveBeenCalledOnce();
  });

  it('retries an already-running handoff sync with the same attempt context', async () => {
    vi.useFakeTimers();
    const launch = vi.fn().mockResolvedValue({
      requestStatus: 'launched',
      status: 'running',
      attemptId: 'attempt-retry',
    });
    const status = vi.fn().mockResolvedValue({
      status: 'succeeded',
      resultData: {
        authenticated: true,
        responseStatus: 'handoff_ready',
        syncId: 'sync-retry',
      },
    });
    const cancel = vi.fn();
    window.breaktwentyDesktop = { visibleAuth: { cancel, launch, status } };
    const onClose = vi.fn();
    const onSuccess = vi.fn();
    let syncAttempt = 0;
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return Promise.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      }
      if (String(url).endsWith('/sync/bmo') && options.method === 'POST') {
        syncAttempt += 1;
        return Promise.resolve(response(syncAttempt === 1
          ? { status: 'already_syncing', sync_id: 'sync-retry' }
          : { status: 'ok', institution_id: 17, sync_id: 'sync-retry' }));
      }
      if (String(url).endsWith('/sync/visible-auth-attempt-cleanup')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <ScraperAuthModal
        institution={{ id: 17, name: 'BMO', provider: 'bmo' }}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    );

    await act(async () => {
      await vi.waitFor(() => expect(syncAttempt).toBe(1));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    await act(async () => {
      await vi.waitFor(() => expect(syncAttempt).toBe(2));
    });

    const syncBodies = fetchMock.mock.calls
      .filter(([url, options]) => String(url).endsWith('/sync/bmo') && options.method === 'POST')
      .map(([, options]) => JSON.parse(options.body));
    expect(syncBodies).toEqual([
      {
        sync_id: 'sync-retry',
        attempt_id: 'attempt-retry',
        institution_id: 17,
        add_flow: false,
      },
      {
        sync_id: 'sync-retry',
        attempt_id: 'attempt-retry',
        institution_id: 17,
        add_flow: false,
      },
    ]);

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('BMO synced successfully')).toBeInTheDocument();
    expect(onSuccess).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });

    expect(onSuccess).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
    expect(cancel).not.toHaveBeenCalled();
  });

  it('cancels and discards an active desktop attempt when the modal unmounts', async () => {
    vi.useRealTimers();
    const launch = vi.fn().mockResolvedValue({
      requestStatus: 'launched',
      status: 'running',
      attemptId: 'attempt-unmount',
    });
    const status = vi.fn().mockResolvedValue({ status: 'running', message: 'Secure browser is running.' });
    const cancel = vi.fn().mockResolvedValue({ status: 'cancelled' });
    window.breaktwentyDesktop = { visibleAuth: { cancel, launch, status } };
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return Promise.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      }
      if (String(url).endsWith('/sync/visible-auth-attempt-cleanup')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const view = render(
      <ScraperAuthModal
        institution={{ id: 17, name: 'BMO', provider: 'bmo' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    await waitFor(() => expect(status).toHaveBeenCalledWith({
      provider: 'bmo',
      attemptId: 'attempt-unmount',
    }));
    view.unmount();

    await waitFor(() => {
      expect(cancel).toHaveBeenCalledWith({ provider: 'bmo', attemptId: 'attempt-unmount' });
    });
    expect(fetchMock.mock.calls.some(([url, options]) => (
      String(url).endsWith('/sync/visible-auth-attempt-cleanup')
      && JSON.parse(options.body).attempt_id === 'attempt-unmount'
    ))).toBe(true);
  });

  it('does not launch after the modal closes during the diagnostics preflight', async () => {
    const diagnostics = deferred();
    const launch = vi.fn();
    window.breaktwentyDesktop = {
      visibleAuth: { cancel: vi.fn(), launch, status: vi.fn() },
    };
    const fetchMock = vi.fn((url) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return diagnostics.promise;
      }
      throw new Error(`Unexpected request: GET ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const view = render(
      <ScraperAuthModal
        institution={{ id: 17, name: 'BMO', provider: 'bmo' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    view.unmount();
    await act(async () => {
      diagnostics.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      await diagnostics.promise;
    });

    expect(launch).not.toHaveBeenCalled();
  });

  it('cancels and discards a desktop attempt that resolves after unmount', async () => {
    const launchResult = deferred();
    const launch = vi.fn(() => launchResult.promise);
    const status = vi.fn();
    const cancel = vi.fn().mockResolvedValue({ status: 'cancelled' });
    window.breaktwentyDesktop = { visibleAuth: { cancel, launch, status } };
    const onResultUpdate = vi.fn();
    const fetchMock = vi.fn((url) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return Promise.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      }
      if (String(url).endsWith('/sync/visible-auth-attempt-cleanup')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      throw new Error(`Unexpected request: GET ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const view = render(
      <ScraperAuthModal
        institution={{ id: 17, name: 'BMO', provider: 'bmo' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
        onResultUpdate={onResultUpdate}
      />,
    );

    await waitFor(() => expect(launch).toHaveBeenCalledOnce());
    view.unmount();
    await act(async () => {
      launchResult.resolve({
        requestStatus: 'launched',
        status: 'running',
        attemptId: 'attempt-late',
      });
      await launchResult.promise;
    });

    await waitFor(() => {
      expect(cancel).toHaveBeenCalledWith({ provider: 'bmo', attemptId: 'attempt-late' });
    });
    expect(status).not.toHaveBeenCalled();
    expect(onResultUpdate).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([url, options]) => (
      String(url).endsWith('/sync/visible-auth-attempt-cleanup')
      && JSON.parse(options.body).attempt_id === 'attempt-late'
    ))).toBe(true);
  });

  it('ignores a desktop launch rejection after unmount', async () => {
    const launchResult = deferred();
    const launch = vi.fn(() => launchResult.promise);
    window.breaktwentyDesktop = {
      visibleAuth: { cancel: vi.fn(), launch, status: vi.fn() },
    };
    const onResultUpdate = vi.fn();
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(
      response({ status: 'ok', capture_level: 'redacted_rich' }),
    )));

    const view = render(
      <ScraperAuthModal
        institution={{ id: 17, name: 'BMO', provider: 'bmo' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
        onResultUpdate={onResultUpdate}
      />,
    );

    await waitFor(() => expect(launch).toHaveBeenCalledOnce());
    view.unmount();
    await act(async () => {
      launchResult.reject(new Error('late launch failure'));
      await launchResult.promise.catch(() => {});
    });

    expect(onResultUpdate).not.toHaveBeenCalled();
  });

  it('clears a pending handoff retry when the modal unmounts', async () => {
    vi.useFakeTimers();
    const launch = vi.fn().mockResolvedValue({
      requestStatus: 'launched',
      status: 'running',
      attemptId: 'attempt-retry-unmount',
    });
    const status = vi.fn().mockResolvedValue({
      status: 'succeeded',
      resultData: {
        authenticated: true,
        responseStatus: 'handoff_ready',
        syncId: 'sync-retry-unmount',
      },
    });
    const cancel = vi.fn();
    window.breaktwentyDesktop = { visibleAuth: { cancel, launch, status } };
    let syncAttempt = 0;
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return Promise.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      }
      if (String(url).endsWith('/sync/bmo') && options.method === 'POST') {
        syncAttempt += 1;
        return Promise.resolve(response({ status: 'already_syncing', sync_id: 'sync-retry-unmount' }));
      }
      if (String(url).endsWith('/sync/visible-auth-attempt-cleanup')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const view = render(
      <ScraperAuthModal
        institution={{ id: 17, name: 'BMO', provider: 'bmo' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    await act(async () => {
      await vi.waitFor(() => expect(syncAttempt).toBe(1));
    });
    view.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    expect(syncAttempt).toBe(1);
    expect(cancel).not.toHaveBeenCalled();
  });

  it('shows add success after account confirmation without waiting for transactions', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const accountSync = deferred();
    const launch = vi.fn().mockResolvedValue({
      requestStatus: 'launched',
      status: 'running',
      attemptId: 'attempt-new-add',
    });
    const status = vi.fn().mockResolvedValue({
      status: 'succeeded',
      resultData: {
        authenticated: true,
        responseStatus: 'handoff_ready',
        syncId: 'sync-new-add',
      },
    });
    window.breaktwentyDesktop = {
      visibleAuth: { cancel: vi.fn(), launch, status },
    };
    const onClose = vi.fn();
    const onSuccess = vi.fn();
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return Promise.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      }
      if (String(url).endsWith('/sync/bmo') && options.method === 'POST') {
        return accountSync.promise;
      }
      if (String(url).endsWith('/institutions/provider/bmo/add-confirm')) {
        return Promise.resolve(response({ status: 'ok', confirmed_institutions: 1 }));
      }
      if (String(url).endsWith('/sync/visible-auth-attempt-cleanup')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <ScraperAuthModal
        institution={{ id: 17, name: 'BMO', provider: 'bmo', isNew: true }}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    );

    expect(await screen.findByText('Connecting your accounts...')).toBeInTheDocument();
    expect(screen.getByText(/Transaction history will continue in the background\./)).toBeInTheDocument();

    await act(async () => {
      accountSync.resolve(response({
        status: 'ok',
        institution_id: 17,
        sync_id: 'sync-new-add',
      }));
      await accountSync.promise;
    });

    expect(await screen.findByText('BMO has been added successfully')).toBeInTheDocument();
    expect(screen.getByText('✓')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    const confirmCall = fetchMock.mock.calls.find(([url]) => (
      String(url).endsWith('/institutions/provider/bmo/add-confirm')
    ));
    expect(JSON.parse(confirmCall[1].body)).toEqual({
      institution_id: 17,
      source_sync_id: 'sync-new-add',
    });

    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });

    expect(onClose).toHaveBeenCalledOnce();
    expect(onSuccess).toHaveBeenCalledOnce();
  });

  it('keeps one desktop attempt owner through a StrictMode effect replay', async () => {
    const launch = vi.fn().mockResolvedValue({
      requestStatus: 'launched',
      status: 'running',
      attemptId: 'attempt-strict',
    });
    const status = vi.fn().mockResolvedValue({ status: 'running', message: 'Secure browser is running.' });
    const cancel = vi.fn().mockResolvedValue({ status: 'cancelled' });
    window.breaktwentyDesktop = { visibleAuth: { cancel, launch, status } };
    const fetchMock = vi.fn((url) => {
      if (String(url).endsWith('/settings/dev-diagnostics/capture-level')) {
        return Promise.resolve(response({ status: 'ok', capture_level: 'redacted_rich' }));
      }
      if (String(url).endsWith('/sync/visible-auth-attempt-cleanup')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      throw new Error(`Unexpected request: GET ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const view = render(
      <React.StrictMode>
        <ScraperAuthModal
          institution={{ id: 17, name: 'BMO', provider: 'bmo' }}
          onClose={vi.fn()}
          onSuccess={vi.fn()}
        />
      </React.StrictMode>,
    );

    await waitFor(() => expect(status).toHaveBeenCalledOnce());
    expect(launch).toHaveBeenCalledOnce();
    view.unmount();
    await waitFor(() => {
      expect(cancel).toHaveBeenCalledWith({ provider: 'bmo', attemptId: 'attempt-strict' });
    });
  });
});
