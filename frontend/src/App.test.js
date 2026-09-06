import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, test, vi } from 'vitest';
import App from './App';

vi.mock('./assets/icons/investments-icon.svg?react', () => ({
  default: (props) => <svg data-testid="investments-icon" {...props} />,
}));

vi.mock('./assets/icons/accounts-icon.svg?react', () => ({
  default: (props) => <svg data-testid="accounts-icon" {...props} />,
}));

vi.mock('./components/charts/EChart', () => ({
  default: function MockEChart() {
    return <div data-testid="echart" />;
  },
}));

vi.mock('./components/AddToNetWorthModal', () => ({
  default: function MockAddToNetWorthModal({ onAuthNeeded }) {
    return (
      <button
        type="button"
        onClick={() => onAuthNeeded({ name: 'Coinbase', provider: 'coinbase', implemented: true })}
      >
        Choose Coinbase
      </button>
    );
  },
}));

vi.mock('./components/ApiAuthModal', () => ({
  default: function MockApiAuthModal({ onSuccess }) {
    return <button type="button" onClick={onSuccess}>Complete Coinbase add</button>;
  },
}));

const emptyNetWorth = {
  current: {
    total_assets: 0,
    total_liabilities: 0,
    net_worth: 0,
    currency: 'CAD',
    date: null,
  },
  history: [],
};

beforeEach(() => {
  window.history.replaceState({}, '', '/');
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal('fetch', vi.fn((url) => {
    const endpoint = String(url);
    const body = endpoint.includes('/onboarding/status')
      ? { completed: true }
      : endpoint.includes('/accounts/transaction-import-status')
        ? { institutions: [] }
        : endpoint.includes('/sync/batches/active')
          ? { status: 'ok', batches: [] }
        : endpoint.includes('/sync/activity')
          ? { active: [] }
      : endpoint.includes('/settings')
        ? {
            user_timezone: 'America/Toronto',
            user_timezone_configured: true,
            user_time_format: '24h',
          }
        : endpoint.includes('/networth')
          ? emptyNetWorth
          : endpoint.includes('/fx-rates')
            ? { base: 'CAD', rates: { CAD: 1 } }
            : [];

    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(body),
    });
  }));
});

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete window.breaktwentyDesktop;
});

test('renders the BreakTwenty navigation shell', async () => {
  render(<App />);

  expect(await screen.findByLabelText('Dashboard')).toBeInTheDocument();
  expect(screen.getByLabelText('Support Diagnostics')).toBeInTheDocument();
  const websiteLink = screen.getByRole('link', { name: 'Visit breaktwenty.com' });
  expect(websiteLink).toHaveAttribute('href', 'https://breaktwenty.com');
  expect(websiteLink).toHaveAttribute('target', '_blank');
  expect(websiteLink).toHaveAttribute('rel', 'noreferrer');
  expect(websiteLink).toHaveAttribute('data-tooltip', 'Visit breaktwenty.com');
  expect(localStorage.getItem('breaktwenty_last_auto_sync')).toBeNull();
});

test('cleans an interrupted add before loading institutions and starting autosync', async () => {
  localStorage.setItem('breaktwenty_pending_add_provider_moomoo', JSON.stringify({
    provider: 'moomoo',
    attemptId: 'stale-moomoo-attempt',
    startedAt: 1,
  }));
  const calls = [];
  global.fetch.mockImplementation((url, options = {}) => {
    const endpoint = String(url);
    const method = options.method || 'GET';
    calls.push(`${method} ${endpoint}`);
    if (method === 'DELETE' && endpoint.includes('/institutions/provider/moomoo/state')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ removed_institutions: 0, skipped_institutions: 0 }),
      });
    }
    if (method === 'POST' && endpoint.endsWith('/sync/batch')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ status: 'started', batch_id: 'startup-auto-batch' }),
      });
    }
    if (endpoint.endsWith('/sync/batch/startup-auto-batch')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ status: 'done', batch_id: 'startup-auto-batch', results: {} }),
      });
    }
    const body = endpoint.includes('/onboarding/status')
      ? { completed: true }
      : endpoint.includes('/accounts/transaction-import-status')
        ? { institutions: [] }
        : endpoint.includes('/sync/batches/active')
          ? { status: 'ok', batches: [] }
          : endpoint.includes('/sync/activity')
            ? { active: [] }
            : endpoint.includes('/settings')
              ? {
                  user_timezone: 'America/Toronto',
                  user_timezone_configured: true,
                  user_time_format: '24h',
                }
              : endpoint.includes('/networth')
                ? emptyNetWorth
                : endpoint.includes('/institutions/scope')
                  ? [{
                      id: 7,
                      provider: 'coinbase',
                      name: 'Coinbase',
                      accounts: [],
                      accountIds: [],
                    }]
                  : endpoint.includes('/institutions/all')
                  ? [{ id: 7, provider: 'coinbase', name: 'Coinbase' }]
                  : endpoint.includes('/institutions')
                    ? [{ id: 7, provider: 'coinbase', name: 'Coinbase' }]
                    : endpoint.includes('/fx-rates')
                      ? { base: 'CAD', rates: { CAD: 1 } }
                      : [];
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  });

  render(<App />);

  await waitFor(() => {
    expect(calls.some((call) => call.startsWith('POST ') && call.endsWith('/sync/batch'))).toBe(true);
  });
  const cleanupIndex = calls.findIndex((call) => (
    call.startsWith('DELETE ') && call.includes('/institutions/provider/moomoo/state')
  ));
  const institutionsIndex = calls.findIndex((call) => call.endsWith('/institutions/all'));
  const autoSyncIndex = calls.findIndex((call) => (
    call.startsWith('POST ') && call.endsWith('/sync/batch')
  ));
  expect(cleanupIndex).toBeGreaterThanOrEqual(0);
  expect(institutionsIndex).toBeGreaterThan(cleanupIndex);
  expect(autoSyncIndex).toBeGreaterThan(institutionsIndex);
  expect(localStorage.getItem('breaktwenty_pending_add_provider_moomoo')).toBeNull();
  expect(localStorage.getItem('breaktwenty_last_auto_sync')).not.toBeNull();
});

test('separates application incidents from institution diagnostics and exports one', async () => {
  const list = vi.fn().mockResolvedValue({
    status: 'ok',
    policy: {
      retentionDays: 7,
      retentionHours: 168,
      maxIncidents: 8,
      maxTotalBytes: 8 * 1024 * 1024,
    },
    incidents: [{
      incidentId: '20260826T220000Z_11111111-1111-4111-8111-111111111111',
      createdAt: '2026-08-26T22:00:00.000Z',
      outcome: 'recovered',
    }],
  });
  const exportIncident = vi.fn().mockResolvedValue({
    status: 'ok',
    filename: 'BreakTwenty_application_diagnostics.zip',
  });
  window.breaktwentyDesktop = {
    isDesktop: true,
    appDiagnostics: { list, export: exportIncident },
  };

  render(<App />);
  fireEvent.click(await screen.findByLabelText('Support Diagnostics'));
  fireEvent.click(screen.getByRole('tab', { name: 'Application' }));

  expect(await screen.findByText(/Nothing is uploaded to the developer/)).toBeInTheDocument();
  expect(await screen.findByText(/Incidents stay on this device for 7 days/)).toBeInTheDocument();
  expect(await screen.findByRole('radio', { name: /Recovered/ })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Export Selected Incident' }));

  await waitFor(() => {
    expect(exportIncident).toHaveBeenCalledWith({
      incidentId: '20260826T220000Z_11111111-1111-4111-8111-111111111111',
    });
  });
  expect(await screen.findByText('Saved BreakTwenty_application_diagnostics.zip.')).toBeInTheDocument();
});

test('shows an initial snapshot failure with a working retry instead of false-empty data', async () => {
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  let networthAttempts = 0;
  global.fetch.mockImplementation((url) => {
    const endpoint = String(url);
    if (endpoint.includes('/networth')) {
      networthAttempts += 1;
      if (networthAttempts === 1) {
        return Promise.resolve({
          ok: false,
          status: 500,
          json: () => Promise.resolve({ detail: 'Snapshot unavailable' }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(emptyNetWorth),
      });
    }
    const body = endpoint.includes('/onboarding/status')
      ? { completed: true }
      : endpoint.includes('/accounts/transaction-import-status')
      ? { institutions: [] }
      : endpoint.includes('/sync/batches/active')
        ? { status: 'ok', batches: [] }
      : endpoint.includes('/sync/activity')
        ? { active: [] }
        : endpoint.includes('/settings')
          ? {
              user_timezone: 'America/Toronto',
              user_timezone_configured: true,
              user_time_format: '24h',
            }
          : endpoint.includes('/fx-rates')
            ? { base: 'CAD', rates: { CAD: 1 } }
            : [];
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  });

  render(<App />);

  expect(await screen.findByText('Snapshot unavailable')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByLabelText('Dashboard')).toBeInTheDocument();
  expect(networthAttempts).toBeGreaterThanOrEqual(2);
  expect(consoleError).toHaveBeenCalledTimes(1);
  expect(consoleError).toHaveBeenCalledWith(
    'Failed to fetch data:',
    expect.objectContaining({ message: 'Snapshot unavailable', status: 500 }),
  );
});

test('automatically retries initial snapshot failures when the backend comes back', async () => {
  vi.useFakeTimers();
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  let networthAttempts = 0;
  global.fetch.mockImplementation((url) => {
    const endpoint = String(url);
    if (endpoint.includes('/networth')) {
      networthAttempts += 1;
      if (networthAttempts === 1) {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ detail: 'Backend warming up' }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(emptyNetWorth),
      });
    }
    const body = endpoint.includes('/onboarding/status')
      ? { completed: true }
      : endpoint.includes('/accounts/transaction-import-status')
      ? { institutions: [] }
      : endpoint.includes('/sync/batches/active')
        ? { status: 'ok', batches: [] }
      : endpoint.includes('/sync/activity')
        ? { active: [] }
        : endpoint.includes('/settings')
          ? {
              user_timezone: 'America/Toronto',
              user_timezone_configured: true,
              user_time_format: '24h',
            }
          : endpoint.includes('/fx-rates')
            ? { base: 'CAD', rates: { CAD: 1 } }
            : [];
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  });

  render(<App />);

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByText('Backend warming up')).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });

  expect(screen.getByLabelText('Dashboard')).toBeInTheDocument();
  expect(networthAttempts).toBe(2);
  expect(consoleError).toHaveBeenCalledWith(
    'Failed to fetch data:',
    expect.objectContaining({ message: 'Backend warming up', status: 503 }),
  );
});

test('refreshes newly added institution data in place without a browser reload', async () => {
  render(<App />);

  await screen.findByLabelText('Dashboard');
  const initialNetworthRequests = global.fetch.mock.calls.filter(([url]) => (
    String(url).includes('/networth')
  )).length;

  fireEvent.click(screen.getByLabelText('Add to Net Worth'));
  fireEvent.click(await screen.findByRole('button', { name: 'Choose Coinbase' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Complete Coinbase add' }));

  await waitFor(() => {
    const networthRequests = global.fetch.mock.calls.filter(([url]) => (
      String(url).includes('/networth')
    )).length;
    expect(networthRequests).toBe(initialNetworthRequests + 1);
  });
  expect(screen.getByLabelText('Dashboard')).toBeInTheDocument();
});

test('keeps the Cash Flow tray reservation stable without changing scroll position', async () => {
  window.history.replaceState({}, '', '/cash-flow');

  const baseFetch = global.fetch.getMockImplementation();
  global.fetch.mockImplementation((url, options) => {
    const endpoint = String(url);
    if (endpoint.includes('/transactions?')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ transactions: [], total: 0 }),
      });
    }
    if (endpoint.includes('/cash-flow')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          period: { start: '2026-09-01', end: '2026-09-30', currency: 'CAD' },
          totals: { income: 450, expense: 75, net: 375 },
          income_breakdown: [],
          expense_breakdown: [],
          needs_review: { category_id: null, transaction_count: 0 },
          investment_activity: {
            buys: -100,
            sells: 600,
            dividends: 0,
            interest: 0,
            withholding_tax: 0,
            expired: 0,
            transfers: 0,
            deposits: 0,
            withdrawals: 0,
            cc_payments: 0,
            loan_payments: 0,
            loan_advances: 0,
            realized_pnl: 0,
            breakdown: [{ category_id: 91, seed_key: 'sell', name: 'Sell' }],
          },
          trend_12_months: [],
          recurring: [],
          previous_totals: null,
        }),
      });
    }
    return baseFetch(url, options);
  });

  render(<App />);
  const sellTrigger = (await screen.findByText('Sells')).closest('[role="button"]');
  const main = document.querySelector('.app-main');
  main.scrollTop = 875;
  fireEvent.click(sellTrigger);
  await waitFor(() => {
    expect(document.querySelector('.app-shell')).toHaveClass('right-tray-cashflow-detail');
    const tray = document.querySelector('.cashflow-detail-tray');
    expect(tray).toHaveClass('is-pinned');
    expect(tray).not.toHaveClass('is-anchored');
  });
  await screen.findByText('No transactions.');
  fireEvent.mouseMove(document.body);
  expect(main.scrollTop).toBe(875);

  const shell = document.querySelector('.app-shell');
  const column = document.querySelector('.app-main-column');
  const originalGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;
  let probeMeasurements = 0;
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function getRect() {
    if (this.parentElement === shell && this.getAttribute('aria-hidden') === 'true') {
      probeMeasurements += 1;
      const probeWidth = this.style.width.includes('--app-work-area-max')
        ? 1200
        : this.style.width.includes('--app-tray-edge-gap')
          ? 24
          : 480;
      return new DOMRect(0, 0, probeWidth, 0);
    }
    if (this === main) return new DOMRect(0, 0, 1600, 2200);
    if (this === column) return new DOMRect(0, 0, 1172, 2200);
    if (this.matches?.('.panel-shell')) return new DOMRect(0, 0, 1172, 300);
    return originalGetBoundingClientRect.call(this);
  });
  Object.defineProperty(main, 'clientWidth', { configurable: true, value: 1600 });
  main.style.paddingLeft = '100px';
  main.style.paddingRight = '100px';
  vi.spyOn(document.documentElement, 'clientWidth', 'get').mockReturnValue(1700);

  const setPropertySpy = vi.spyOn(shell.style, 'setProperty');
  window.dispatchEvent(new Event('resize'));
  await waitFor(() => {
    expect(shell.style.getPropertyValue('--app-tray-content-reservation')).toBe('328.000px');
  });
  expect(setPropertySpy).not.toHaveBeenCalledWith(
    '--app-tray-content-reservation',
    '100.000px',
  );

  setPropertySpy.mockClear();
  const measurementsBeforeMutation = probeMeasurements;
  main.appendChild(document.createElement('span'));
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(probeMeasurements).toBe(measurementsBeforeMutation);
  expect(setPropertySpy).not.toHaveBeenCalled();
  expect(shell.style.getPropertyValue('--app-tray-content-reservation')).toBe('328.000px');
});
