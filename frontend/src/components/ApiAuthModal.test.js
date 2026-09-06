import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  clearPendingInstitutionAdd: vi.fn(),
  confirmPendingInstitutionAdd: vi.fn(),
  confirmPendingInstitutionAddResult: vi.fn(),
  fetchWithTimeout: vi.fn(),
  markPendingInstitutionAdd: vi.fn(() => 'attempt-1'),
  isDesktopShell: vi.fn(() => true),
  cancelDesktopVisibleAuth: vi.fn(),
  getDesktopVisibleAuthStatus: vi.fn(),
  launchDesktopVisibleAuth: vi.fn(),
  startIncompleteInstitutionAddCleanup: vi.fn(),
}));

vi.mock('../utils/incompleteInstitutionAdds', () => ({
  clearPendingInstitutionAdd: mocks.clearPendingInstitutionAdd,
  confirmPendingInstitutionAdd: mocks.confirmPendingInstitutionAdd,
  confirmPendingInstitutionAddResult: mocks.confirmPendingInstitutionAddResult,
  markPendingInstitutionAdd: mocks.markPendingInstitutionAdd,
  startIncompleteInstitutionAddCleanup: mocks.startIncompleteInstitutionAddCleanup,
}));

vi.mock('../utils/syncRequests', () => ({
  ADD_INSTITUTION_SYNC_REQUEST_TIMEOUT_MS: 180000,
  USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS: 120000,
  fetchWithTimeout: mocks.fetchWithTimeout,
}));

vi.mock('../utils/desktopBridge', () => ({
  cancelDesktopVisibleAuth: mocks.cancelDesktopVisibleAuth,
  getDesktopVisibleAuthStatus: mocks.getDesktopVisibleAuthStatus,
  isDesktopShell: mocks.isDesktopShell,
  launchDesktopVisibleAuth: mocks.launchDesktopVisibleAuth,
}));

import ApiAuthModal from './ApiAuthModal';

function deferred() {
  let resolve;
  const promise = new Promise((promiseResolve) => { resolve = promiseResolve; });
  return { promise, resolve };
}

describe('API provider add flow', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    Object.values(mocks).forEach((mock) => mock.mockClear());
    mocks.markPendingInstitutionAdd.mockReturnValue('attempt-1');
    mocks.isDesktopShell.mockReturnValue(true);
    mocks.cancelDesktopVisibleAuth.mockResolvedValue({ status: 'cancelled' });
    mocks.getDesktopVisibleAuthStatus.mockResolvedValue({ status: 'running' });
    mocks.launchDesktopVisibleAuth.mockResolvedValue({ status: 'running', attemptId: 'oauth-browser-1' });
    mocks.confirmPendingInstitutionAdd.mockResolvedValue(true);
    vi.stubGlobal('fetch', vi.fn(async (_url, options = {}) => ({
      ok: true,
      json: async () => (
        options.method === 'POST'
          ? { status: 'ok', institution_id: 17 }
          : {}
      ),
    })));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('shows add success after accounts are ready while transaction import continues in the background', async () => {
    const accountSync = deferred();
    mocks.fetchWithTimeout.mockReturnValue(accountSync.promise);
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    const { container } = render(
      <ApiAuthModal
        institution={{ name: 'Interactive Brokers', provider: 'ibkr', isNew: true }}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    );

    const credentialInputs = container.querySelectorAll('.settings-field input');
    fireEvent.change(credentialInputs[0], { target: { value: '123456' } });
    fireEvent.change(credentialInputs[1], { target: { value: 'test-token' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save & Sync' }));

    await waitFor(() => expect(mocks.fetchWithTimeout).toHaveBeenCalledOnce());
    await act(async () => { vi.advanceTimersByTime(1200); });

    expect(screen.getByText('Connecting your accounts...')).toBeInTheDocument();
    expect(screen.getByText(/Transaction history will continue in the background\./)).toBeInTheDocument();

    await act(async () => {
      accountSync.resolve({
        json: async () => ({ status: 'ok', sync_id: 'ibkr-sync-1' }),
      });
      await accountSync.promise;
    });

    await waitFor(() => expect(mocks.confirmPendingInstitutionAdd).toHaveBeenCalledWith(
      'ibkr',
      17,
      'ibkr-sync-1',
      '',
    ));
    expect(await screen.findByText('Interactive Brokers has been added successfully')).toBeInTheDocument();
    expect(screen.getByText('✓')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();

    await act(async () => { vi.advanceTimersByTime(1500); });

    expect(onClose).toHaveBeenCalledOnce();
    expect(onSuccess).toHaveBeenCalledOnce();
  });

  it('shows the provider failure reason instead of dropping the add modal', async () => {
    mocks.fetchWithTimeout.mockResolvedValue({
      json: async () => ({ status: 'auth_required', message: 'IBKR rejected the Flex token.' }),
    });
    const onClose = vi.fn();

    const { container } = render(
      <ApiAuthModal
        institution={{ name: 'Interactive Brokers', provider: 'ibkr', isNew: true }}
        onClose={onClose}
        onSuccess={vi.fn()}
      />,
    );

    const credentialInputs = container.querySelectorAll('.settings-field input');
    fireEvent.change(credentialInputs[0], { target: { value: '123456' } });
    fireEvent.change(credentialInputs[1], { target: { value: 'test-token' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save & Sync' }));

    expect(await screen.findByText('IBKR rejected the Flex token.')).toBeInTheDocument();
    expect(screen.getByText('✕')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('uses browser OAuth for Moomoo in the web app', async () => {
    mocks.isDesktopShell.mockReturnValue(false);
    mocks.confirmPendingInstitutionAddResult.mockResolvedValue({
      status: 'ok',
      confirmed_institutions: 1,
    });
    mocks.fetchWithTimeout.mockResolvedValue({
      json: async () => ({
        status: 'ok',
        sync_id: 'moomoo-cloud-sync-1',
        attempt_id: 'moomoo-attempt-1',
      }),
    });
    const popup = {
      closed: false,
      close: vi.fn(),
      location: { replace: vi.fn() },
    };
    vi.stubGlobal('open', vi.fn(() => popup));
    globalThis.fetch.mockImplementation(async (url, options = {}) => {
      if (!options.method && url.endsWith('/settings')) {
        return { ok: true, json: async () => ({}) };
      }
      if (url.endsWith('/auth/moomoo/oauth/start')) {
        return {
          ok: true,
          json: async () => ({
            status: 'waiting',
            flow_id: 'flow-1',
            sync_id: 'moomoo-cloud-sync-1',
            attempt_id: 'moomoo-attempt-1',
            authorization_url: 'https://webapi.moomoo.com/oauth2/authorize/confirm?safe=test',
            expires_in: 600,
          }),
        };
      }
      if (url.endsWith('/auth/moomoo/oauth/flow-1/status')) {
        return { ok: true, json: async () => ({ status: 'authorized' }) };
      }
      if (url.endsWith('/auth/moomoo/oauth/flow-1/complete')) {
        return {
          ok: true,
          json: async () => ({
            status: 'ok',
            institution_id: 17,
            sync_id: 'moomoo-cloud-sync-1',
            attempt_id: 'moomoo-attempt-1',
          }),
        };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });

    const { container } = render(
      <ApiAuthModal
        institution={{ name: 'Moomoo', provider: 'moomoo', isNew: true }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(container.querySelectorAll('.settings-field input')).toHaveLength(0);
    expect(screen.getByText(/never receives your Moomoo password/)).toBeInTheDocument();
    expect(screen.getByText(/dropdown supports multiple account selections/)).toBeInTheDocument();
    expect(screen.getByText('Accounts & Orders')).toHaveClass('moomoo-required-scope');
    expect(screen.getByText('Accounts & Orders').tagName).toBe('STRONG');
    expect(container.querySelector('.modal-desc .brand-name')).toBeInTheDocument();
    expect(screen.getByText(/This is required to import the account, balances, holdings, and transaction history/)).toBeInTheDocument();
    expect(screen.getByText('Trade Execution').tagName).toBe('STRONG');
    expect(screen.getByText(/never places trades/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Continue to Moomoo' }));

    await waitFor(() => expect(mocks.fetchWithTimeout).toHaveBeenCalledWith(
      expect.stringMatching(/\/sync\/moomoo$/),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          add_flow: true,
          sync_id: 'moomoo-cloud-sync-1',
          attempt_id: 'moomoo-attempt-1',
          institution_id: 17,
        }),
      }),
      180000,
    ));
    const popupUrl = new URL(popup.location.replace.mock.calls[0][0]);
    expect(popupUrl.origin).toBe('https://passport.moomoo.com');
    expect(popupUrl.searchParams.get('lang')).toBe('en-us');
    expect(popupUrl.searchParams.get('type')).toBe('login');
    expect(popupUrl.searchParams.get('target')).toMatch(
      /^https:\/\/webapi\.moomoo\.com\/oauth2\/authorize\/confirm/,
    );
    expect(popup.close).toHaveBeenCalled();
    await waitFor(() => expect(mocks.confirmPendingInstitutionAdd).toHaveBeenCalledWith(
      'moomoo',
      17,
      'moomoo-cloud-sync-1',
      'moomoo-attempt-1',
    ));
    expect(await screen.findByText('Moomoo has been added successfully')).toBeInTheDocument();
  });

  it('uses OAuth and the managed visible-auth browser in Electron', async () => {
    mocks.isDesktopShell.mockReturnValue(true);
    mocks.launchDesktopVisibleAuth.mockResolvedValue({
      status: 'running',
      attemptId: 'moomoo-attempt-linux',
    });
    mocks.confirmPendingInstitutionAddResult.mockResolvedValue({ status: 'ok' });
    mocks.fetchWithTimeout.mockResolvedValue({
      json: async () => ({
        status: 'ok',
        sync_id: 'moomoo-linux-electron-sync',
        attempt_id: 'moomoo-attempt-linux',
      }),
    });
    globalThis.fetch.mockImplementation(async (url, options = {}) => {
      if (!options.method && url.endsWith('/settings')) return { ok: true, json: async () => ({}) };
      if (url.endsWith('/settings/dev-diagnostics/capture-level')) {
        return { ok: true, json: async () => ({ status: 'ok', capture_level: 'developer_local' }) };
      }
      if (url.endsWith('/auth/moomoo/oauth/start')) {
        return { ok: true, json: async () => ({
          status: 'waiting',
          flow_id: 'linux-electron-flow',
          sync_id: 'moomoo-linux-electron-sync',
          attempt_id: 'moomoo-attempt-linux',
          authorization_url: 'https://webapi.moomoo.com/oauth2/authorize/confirm?safe=test',
          expires_in: 600,
        }) };
      }
      if (url.endsWith('/auth/moomoo/oauth/linux-electron-flow/status')) {
        return { ok: true, json: async () => ({ status: 'authorized' }) };
      }
      if (url.endsWith('/auth/moomoo/oauth/linux-electron-flow/complete')) {
        return { ok: true, json: async () => ({
          status: 'ok', institution_id: 17, sync_id: 'moomoo-linux-electron-sync',
          attempt_id: 'moomoo-attempt-linux',
        }) };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });

    const { container } = render(
      <ApiAuthModal
        institution={{ name: 'Moomoo', provider: 'moomoo', isNew: true }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    expect(container.querySelectorAll('.settings-field input')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Continue to Moomoo' }));
    await waitFor(() => expect(mocks.launchDesktopVisibleAuth).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: 'moomoo',
        authorizationUrl: expect.stringMatching(/^https:\/\/webapi\.moomoo\.com\/oauth2\/authorize\/confirm/),
        syncId: 'moomoo-linux-electron-sync',
        attemptId: 'moomoo-attempt-linux',
        addFlow: true,
        captureLevel: 'developer_local',
      }),
    ));
    expect(await screen.findByText('Moomoo has been added successfully')).toBeInTheDocument();
  });

  it('uses the same interrupted result when another API provider add form is closed', async () => {
    const onClose = vi.fn();

    render(
      <ApiAuthModal
        institution={{ name: 'Interactive Brokers', provider: 'ibkr', isNew: true }}
        onClose={onClose}
        onSuccess={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '✕' }));
    expect(screen.getByText('Adding Interactive Brokers was interrupted')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => { vi.advanceTimersByTime(1500); });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('shows success for an existing API provider re-sync before returning to the UI', async () => {
    const replacementId = '33333333-3333-4333-8333-333333333333';
    globalThis.fetch.mockImplementation(async (url, options = {}) => {
      if (!options.method) return { ok: true, json: async () => ({}) };
      if (url.endsWith('/settings')) {
        return {
          ok: true,
          json: async () => ({
            status: 'ok',
            institution_id: 17,
            credential_replacement_id: replacementId,
          }),
        };
      }
      if (url.endsWith(`/credential-replacements/${replacementId}/commit`)) {
        return { ok: true, json: async () => ({ status: 'ok' }) };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    mocks.fetchWithTimeout.mockResolvedValue({
      json: async () => ({ status: 'ok', sync_id: 'ibkr-sync-2' }),
    });
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    const { container } = render(
      <ApiAuthModal
        institution={{ id: 17, name: 'Interactive Brokers', provider: 'ibkr' }}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    );

    const credentialInputs = container.querySelectorAll('.settings-field input');
    fireEvent.change(credentialInputs[0], { target: { value: '123456' } });
    fireEvent.change(credentialInputs[1], { target: { value: 'test-token' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save & Sync' }));

    expect(await screen.findByText('Interactive Brokers synced successfully')).toBeInTheDocument();
    expect(screen.getByText('✓')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
    expect(mocks.confirmPendingInstitutionAdd).not.toHaveBeenCalled();
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining(`/credential-replacements/${replacementId}/commit`),
      { method: 'POST' },
    );

    await act(async () => { vi.advanceTimersByTime(1500); });

    expect(onClose).toHaveBeenCalledOnce();
    expect(onSuccess).toHaveBeenCalledOnce();
  });

  it('rolls back an existing-provider credential replacement after validation fails', async () => {
    const replacementId = '44444444-4444-4444-8444-444444444444';
    globalThis.fetch.mockImplementation(async (url, options = {}) => {
      if (!options.method) return { ok: true, json: async () => ({}) };
      if (url.endsWith('/settings')) {
        return {
          ok: true,
          json: async () => ({
            status: 'ok',
            institution_id: 17,
            credential_replacement_id: replacementId,
          }),
        };
      }
      if (url.endsWith(`/credential-replacements/${replacementId}/rollback`)) {
        return { ok: true, json: async () => ({ status: 'ok' }) };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    mocks.fetchWithTimeout.mockResolvedValue({
      json: async () => ({ status: 'auth_required', message: 'Token rejected.' }),
    });

    const { container } = render(
      <ApiAuthModal
        institution={{ id: 17, name: 'Questrade', provider: 'questrade' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    fireEvent.change(container.querySelector('.settings-field input'), {
      target: { value: 'submitted-token' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save & Sync' }));

    expect(await screen.findByText('Token rejected.')).toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining(`/credential-replacements/${replacementId}/rollback`),
      { method: 'POST' },
    );
  });

  it('best-effort settles an interrupted replacement when the modal unmounts', async () => {
    const replacementId = '55555555-5555-4555-8555-555555555555';
    const pendingSync = new Promise(() => {});
    globalThis.fetch.mockImplementation(async (url, options = {}) => {
      if (!options.method) return { ok: true, json: async () => ({}) };
      if (url.endsWith('/settings')) {
        return {
          ok: true,
          json: async () => ({
            status: 'ok',
            institution_id: 17,
            credential_replacement_id: replacementId,
          }),
        };
      }
      if (url.endsWith(`/credential-replacements/${replacementId}/rollback`)) {
        return { ok: true, json: async () => ({ status: 'ok' }) };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    mocks.fetchWithTimeout.mockReturnValue(pendingSync);

    const rendered = render(
      <ApiAuthModal
        institution={{ id: 17, name: 'Questrade', provider: 'questrade' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    fireEvent.change(rendered.container.querySelector('.settings-field input'), {
      target: { value: 'submitted-token' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save & Sync' }));
    await waitFor(() => expect(mocks.fetchWithTimeout).toHaveBeenCalledOnce());

    rendered.unmount();

    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining(`/credential-replacements/${replacementId}/rollback`),
      { method: 'POST', keepalive: true },
    ));
  });

  it('rolls back a replacement returned after the modal already unmounted', async () => {
    const replacementId = '66666666-6666-4666-8666-666666666666';
    const settingsSave = deferred();
    globalThis.fetch.mockImplementation((url, options = {}) => {
      if (!options.method) {
        return Promise.resolve({ ok: true, json: async () => ({}) });
      }
      if (url.endsWith('/settings')) return settingsSave.promise;
      if (url.endsWith(`/credential-replacements/${replacementId}/rollback`)) {
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) });
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });

    const rendered = render(
      <ApiAuthModal
        institution={{ id: 17, name: 'Questrade', provider: 'questrade' }}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    fireEvent.change(rendered.container.querySelector('.settings-field input'), {
      target: { value: 'submitted-token' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save & Sync' }));
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/settings$/),
      expect.objectContaining({ method: 'POST' }),
    ));

    rendered.unmount();
    settingsSave.resolve({
      ok: true,
      json: async () => ({
        status: 'ok',
        institution_id: 17,
        credential_replacement_id: replacementId,
      }),
    });

    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining(`/credential-replacements/${replacementId}/rollback`),
      { method: 'POST', keepalive: true },
    ));
    expect(mocks.fetchWithTimeout).not.toHaveBeenCalled();
  });
});
