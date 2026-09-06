import React from 'react';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import InstitutionSettingsModal, { formatPurchaseDate } from './InstitutionSettingsModal';

vi.mock('./InstitutionLogo', () => ({
  default: ({ name }) => <span>{name}</span>,
  AuthenticatedInstitutionImage: () => null,
}));

vi.mock('./BrandName', () => ({
  renderBrandMarkedText: (value) => {
    const match = String(value).match(/^\*\*(.+?)\*\*(.*)$/);
    return match ? <><strong>{match[1]}</strong>{match[2]}</> : value;
  },
  renderBrandText: (value) => value,
}));

vi.mock('./promoDemoEnvironment', () => ({
  isPromoDemoActive: () => false,
}));

vi.mock('../utils/syncRequests', () => ({
  USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS: 120_000,
  fetchWithTimeout: (url, options) => fetch(url, options),
}));

const institution = {
  id: 17,
  name: 'Questrade',
  provider: 'questrade',
  category: 'bank_brokerage',
  sync_status: 'ok',
  has_logo: false,
};

function response(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: vi.fn().mockResolvedValue(payload),
  };
}

function requestMethod(options = {}) {
  return options.method || 'GET';
}

function isSettingsRead(url, options) {
  return requestMethod(options) === 'GET' && url.includes('/settings?institution_id=17');
}

function isSettingsWrite(url, options) {
  return requestMethod(options) === 'POST' && url.endsWith('/settings');
}

function isInstitutionAccountsRead(url, institutionId, options) {
  return requestMethod(options) === 'GET' && url.endsWith(`/institutions/${institutionId}/accounts`);
}

function renderSettings(targetInstitution = institution) {
  return render(
    <InstitutionSettingsModal
      institution={targetInstitution}
      onClose={vi.fn()}
      onDataChange={vi.fn()}
    />,
  );
}

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('InstitutionSettingsModal credential response handling', () => {
  it('formats value-history calendar dates without shifting to the prior local day', () => {
    const expected = new Date(2026, 7, 22).toLocaleDateString(
      undefined,
      { year: 'numeric', month: 'short', day: 'numeric' },
    );

    expect(formatPurchaseDate('2026-08-22')).toBe(expected);
    expect(formatPurchaseDate('2026-08-22T00:00:00Z')).toBe(expected);
  });

  it('explains the full local provider-data deletion scope in human terms', async () => {
    const fetchMock = vi.fn((url, options = {}) => {
      if (isSettingsRead(url, options)) {
        return Promise.resolve(response({}));
      }
      if (isInstitutionAccountsRead(url, 17, options)) {
        return Promise.resolve(response([]));
      }
      throw new Error(`Unexpected request: ${requestMethod(options)} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Institution' }));

    expect(screen.getByText('Are you sure you want to delete Questrade?')).toBeInTheDocument();
    expect(screen.getByText(/This permanently wipes all Questrade data from BreakTwenty/)).toHaveClass(
      'institution-delete-warning',
    );
  });

  it('hydrates a saved API secret masked and reveals it only with the eye toggle', async () => {
    const fetchMock = vi.fn((url, options = {}) => {
      if (isSettingsRead(url, options)) {
        return Promise.resolve(response({
          credential_presence: { questrade_refresh_token: true },
          questrade_refresh_token: 'saved-api-secret',
        }));
      }
      if (isInstitutionAccountsRead(url, 17, options)) {
        return Promise.resolve(response([]));
      }
      throw new Error(`Unexpected request: ${requestMethod(options)} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSettings();
    await screen.findByRole('button', { name: 'Update Credentials' });
    expect(screen.getByText(/not sent to the developer/i)).toBeInTheDocument();
    const input = document.querySelector('.settings-field input');
    await waitFor(() => expect(input?.value).toBe('saved-api-secret'));
    expect(input?.type).toBe('password');
    fireEvent.click(document.querySelector('.modal-secret-toggle'));
    expect(input?.type).toBe('text');
    expect(input?.value).toBe('saved-api-secret');
  });

  it('shows managed authorization instead of credential fields for cloud Moomoo', async () => {
    const moomooInstitution = {
      ...institution,
      name: 'Moomoo',
      provider: 'moomoo',
    };
    const onReconnect = vi.fn();
    const fetchMock = vi.fn((url, options = {}) => {
      if (isInstitutionAccountsRead(url, 17, options)) {
        return Promise.resolve(response([]));
      }
      throw new Error(`Unexpected request: ${requestMethod(options)} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <InstitutionSettingsModal
        institution={moomooInstitution}
        onClose={vi.fn()}
        onDataChange={vi.fn()}
        onReconnect={onReconnect}
      />,
    );

    expect(await screen.findByText('How Moomoo authorization works:')).toBeInTheDocument();
    expect(screen.getByText('No Moomoo credentials to manage.').tagName).toBe('STRONG');
    expect(screen.getByText('Authorize once.').tagName).toBe('STRONG');
    expect(screen.getByText('Login changes normally do not interrupt syncing.').tagName).toBe('STRONG');
    expect(screen.getByText(/You never need to find, copy, or paste it/)).toBeInTheDocument();
    expect(document.querySelectorAll('.settings-field input')).toHaveLength(0);
    expect(fetchMock.mock.calls.some(([url]) => url.includes('/settings?'))).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Reconnect Moomoo' }));
    expect(onReconnect).toHaveBeenCalledWith(moomooInstitution);
  });

  it('hydrates saved scraper credentials masked and reveals them with their eye toggles', async () => {
    const rbcInstitution = {
      ...institution,
      id: 23,
      name: 'RBC',
      provider: 'rbc',
    };
    const fetchMock = vi.fn((url, options = {}) => {
      const method = requestMethod(options);
      if (method === 'GET' && url.includes('/credentials/rbc/status?institution_id=23')) {
        return Promise.resolve(response({
          status: 'ok',
          has_saved_credentials: true,
          username: 'saved-user',
          password: 'saved-password',
        }));
      }
      if (isInstitutionAccountsRead(url, 23, options)) {
        return Promise.resolve(response([]));
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSettings(rbcInstitution);
    await screen.findByRole('button', { name: 'Save Credentials' });
    const inputs = document.querySelectorAll('.settings-field input');
    await waitFor(() => {
      expect(inputs[0]?.value).toBe('saved-user');
      expect(inputs[1]?.value).toBe('saved-password');
    });
    expect(inputs[0]?.type).toBe('password');
    expect(inputs[1]?.type).toBe('password');
    const toggles = document.querySelectorAll('.modal-secret-toggle');
    fireEvent.click(toggles[0]);
    fireEvent.click(toggles[1]);
    expect(inputs[0]?.type).toBe('text');
    expect(inputs[1]?.type).toBe('text');
  });

  it('treats an HTTP-successful credential save with an error status as a failure', async () => {
    const fetchMock = vi.fn((url, options = {}) => {
      if (isSettingsRead(url, options)) {
        return Promise.resolve(response({ credential_presence: { questrade_refresh_token: true } }));
      }
      if (isSettingsWrite(url, options)) {
        return Promise.resolve(response({ status: 'error', message: 'Save rejected' }));
      }
      if (isInstitutionAccountsRead(url, 17, options)) {
        return Promise.resolve(response([]));
      }
      throw new Error(`Unexpected request: ${requestMethod(options)} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSettings();
    await screen.findByRole('button', { name: 'Update Credentials' });
    const input = document.querySelector('.settings-field input');
    fireEvent.change(input, { target: { value: 'new-token' } });
    fireEvent.click(screen.getByRole('button', { name: 'Update Credentials' }));

    expect(await screen.findByText('Save rejected')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith('/sync/questrade'))).toBe(false);
  });

  it('never requests old plaintext credentials for client-side rollback', async () => {
    let settingsWrites = 0;
    const fetchMock = vi.fn((url, options = {}) => {
      if (isSettingsRead(url, options)) {
        return Promise.resolve(response({ credential_presence: { questrade_refresh_token: true } }));
      }
      if (isSettingsWrite(url, options)) {
        settingsWrites += 1;
        return Promise.resolve(response({
          status: 'ok',
          credential_replacement_id: '11111111-1111-4111-8111-111111111111',
        }));
      }
      if (requestMethod(options) === 'POST' && url.endsWith('/sync/questrade')) {
        return Promise.resolve(response({ status: 'error', message: 'Login rejected' }));
      }
      if (isInstitutionAccountsRead(url, 17, options)) {
        return Promise.resolve(response([]));
      }
      if (
        requestMethod(options) === 'POST'
        && url.endsWith('/credential-replacements/11111111-1111-4111-8111-111111111111/rollback')
      ) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      throw new Error(`Unexpected request: ${requestMethod(options)} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSettings();
    await screen.findByRole('button', { name: 'Update Credentials' });
    const input = document.querySelector('.settings-field input');
    fireEvent.change(input, { target: { value: 'new-token' } });
    fireEvent.click(screen.getByRole('button', { name: 'Update Credentials' }));

    expect(await screen.findByText('Login rejected')).toBeInTheDocument();
    expect(settingsWrites).toBe(1);
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith('/rollback'))).toBe(true);
  });

  it('best-effort rolls back a pending replacement if the modal unmounts', async () => {
    const pendingSync = new Promise(() => {});
    const fetchMock = vi.fn((url, options = {}) => {
      if (isSettingsRead(url, options)) {
        return Promise.resolve(response({ credential_presence: { questrade_refresh_token: true } }));
      }
      if (isSettingsWrite(url, options)) {
        return Promise.resolve(response({
          status: 'ok',
          credential_replacement_id: '22222222-2222-4222-8222-222222222222',
        }));
      }
      if (requestMethod(options) === 'POST' && url.endsWith('/sync/questrade')) {
        return pendingSync;
      }
      if (requestMethod(options) === 'POST' && url.endsWith('/rollback')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      if (isInstitutionAccountsRead(url, 17, options)) {
        return Promise.resolve(response([]));
      }
      throw new Error(`Unexpected request: ${requestMethod(options)} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const rendered = renderSettings();
    await screen.findByRole('button', { name: 'Update Credentials' });
    fireEvent.change(document.querySelector('.settings-field input'), { target: { value: 'new-token' } });
    fireEvent.click(screen.getByRole('button', { name: 'Update Credentials' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url, options]) => (
      isSettingsWrite(url, options)
    ))).toBe(true));

    rendered.unmount();

    await waitFor(() => expect(fetchMock.mock.calls.some(([url, options]) => (
      url.endsWith('/credential-replacements/22222222-2222-4222-8222-222222222222/rollback')
      && options.keepalive === true
    ))).toBe(true));
  });

  it('does not persist new scraper credentials when restoring 2FA sync status fails', async () => {
    const wealthsimpleInstitution = {
      ...institution,
      id: 23,
      name: 'Wealthsimple',
      provider: 'wealthsimple',
      sync_status: 'auth_required',
    };
    const fetchMock = vi.fn((url, options = {}) => {
      const method = requestMethod(options);
      if (method === 'GET' && url.includes('/credentials/wealthsimple/status?institution_id=23')) {
        return Promise.resolve(response({ status: 'ok', has_saved_credentials: true }));
      }
      if (method === 'POST' && url.endsWith('/sync/wealthsimple/login')) {
        return Promise.resolve(response({ status: '2fa_required' }));
      }
      if (method === 'PUT' && url.endsWith('/institutions/23/sync-status')) {
        return Promise.resolve(response({ status: 'error', message: 'Status restore rejected' }));
      }
      if (method === 'PUT' && url.endsWith('/credentials/wealthsimple')) {
        return Promise.resolve(response({ status: 'ok' }));
      }
      if (isInstitutionAccountsRead(url, 23, options)) {
        return Promise.resolve(response([]));
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderSettings(wealthsimpleInstitution);
    await screen.findByRole('button', { name: 'Save Credentials' });
    const [usernameInput, passwordInput] = document.querySelectorAll('.settings-field input');
    expect(usernameInput.value).toBe('');
    expect(passwordInput.value).toBe('');
    fireEvent.change(usernameInput, { target: { value: 'new@example.com' } });
    fireEvent.change(passwordInput, { target: { value: 'new-password' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Credentials' }));

    expect(await screen.findByText('Status restore rejected Previous credentials were kept.')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url, options]) => (
      requestMethod(options) === 'PUT' && url.endsWith('/credentials/wealthsimple')
    ))).toBe(false);
  });
});
