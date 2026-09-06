import { describe, expect, it } from 'vitest';

import {
  getTransactionImportSettlementStatus,
  isCurrentAutosyncState,
  resolveProviderSyncDisplay,
  sanitizeProviderSyncMessage,
} from './syncDisplayState';

const institution = { id: 12, provider: 'bmo', name: 'BMO', sync_status: 'ok' };
const completeTransactions = {
  institutions: [{
    institution_id: 12,
    provider: 'bmo',
    current_fetch_status: 'idle',
    history_status: 'complete',
  }],
};

function resolve(overrides = {}) {
  return resolveProviderSyncDisplay({
    institution,
    lastSynced: '2026-08-26T18:00:00Z',
    lastSyncText: '2 hours ago',
    transactionImportStatus: completeTransactions,
    nowMs: Date.parse('2026-08-26T20:00:00Z'),
    ...overrides,
  });
}

describe('provider sync display authority', () => {
  it('expires a retained autosync error after autosync finishes', () => {
    expect(isCurrentAutosyncState({ status: 'error', institutionId: 12 }, false)).toBe(false);
  });

  it('keeps autosync state authoritative while autosync is active', () => {
    expect(isCurrentAutosyncState({ status: 'error', institutionId: 12 }, true)).toBe(true);
  });

  it('keeps the bounded post-sync and transaction-import states authoritative', () => {
    expect(isCurrentAutosyncState({ status: 'ok', justSynced: true }, false)).toBe(true);
    expect(isCurrentAutosyncState({ status: 'syncing', awaitingTransactionImport: true }, false)).toBe(true);
  });

  it('lets transaction retry override account success and optimistic success', () => {
    const model = resolve({
      autoSyncState: { status: 'ok', justSynced: true, institutionId: 12 },
      transactionImportStatus: {
        institutions: [{
          institution_id: 12,
          provider: 'bmo',
          current_fetch_status: 'retry_needed',
          history_status: 'retry_needed',
          last_error: 'BMO browser transaction capture did not include this date range',
        }],
      },
    });

    expect(model).toMatchObject({
      label: 'Partial sync',
      tone: 'partial',
      colorClass: 'is-partial',
      actionTarget: 'sync',
    });
    expect(model.tooltipRows).toEqual([
      'Last sync: 2 hours ago',
      'Retry needed: BMO browser transaction capture did not include this date range',
    ]);
  });

  it('maps already_syncing to the canonical active display', () => {
    expect(resolve({ manualSyncState: { status: 'already_syncing' } })).toMatchObject({
      label: 'Syncing',
      tone: 'syncing',
      iconState: 'spinner',
    });
  });

  it('keeps a provider Syncing while its batch transaction job is queued', () => {
    expect(resolve({
      batchState: {
        status: 'ok',
        transaction_import_job: { status: 'queued' },
      },
    })).toMatchObject({
      label: 'Syncing',
      tone: 'syncing',
    });
  });

  it('lets the matching live transaction job settle before a slower batch sibling', () => {
    expect(resolve({
      batchState: {
        status: 'ok',
        transaction_import_job: {
          job_id: 'bmo-tximport-1',
          status: 'queued',
          updated_at: '2026-08-26T19:00:00Z',
        },
      },
      transactionImportStatus: {
        generated_at: '2026-08-26T19:00:05Z',
        institutions: [{
          institution_id: 12,
          provider: 'bmo',
          current_fetch_status: 'idle',
          history_status: 'complete',
          transaction_import_job_id: 'bmo-tximport-1',
          transaction_import_job_status: 'complete',
        }],
      },
    })).toMatchObject({
      label: 'Synced',
      tone: 'synced',
    });
  });

  it('does not revive a queued batch snapshot after its terminal job ages out of the live payload', () => {
    expect(resolve({
      batchState: {
        status: 'ok',
        transaction_import_job: {
          job_id: 'bmo-tximport-1',
          status: 'queued',
          updated_at: '2026-08-26T19:00:00Z',
        },
      },
      transactionImportStatus: {
        generated_at: '2026-08-26T19:02:00Z',
        institutions: [{
          institution_id: 12,
          provider: 'bmo',
          current_fetch_status: 'idle',
          history_status: 'complete',
          transaction_import_job_id: null,
          transaction_import_job_status: null,
        }],
      },
    })).toMatchObject({
      label: 'Synced',
      tone: 'synced',
    });
  });

  it('uses the queued batch handoff while the live payload still predates that job', () => {
    expect(resolve({
      batchState: {
        status: 'ok',
        transaction_import_job: {
          job_id: 'bmo-tximport-1',
          status: 'queued',
          updated_at: '2026-08-26T19:00:05Z',
        },
      },
      transactionImportStatus: {
        generated_at: '2026-08-26T19:00:00Z',
        institutions: [{
          institution_id: 12,
          provider: 'bmo',
          current_fetch_status: 'idle',
          history_status: 'complete',
          transaction_import_job_id: null,
          transaction_import_job_status: null,
        }],
      },
    })).toMatchObject({
      label: 'Syncing',
      tone: 'syncing',
    });
  });

  it('maps network failures to red failure rather than partial sync', () => {
    expect(resolve({
      manualSyncState: { status: 'network_error', message: 'Connection timed out' },
      transactionImportStatus: {
        institutions: [{
          institution_id: 12,
          provider: 'bmo',
          current_fetch_status: 'retry_needed',
          history_status: 'retry_needed',
        }],
      },
    })).toMatchObject({
      label: 'Sync failed',
      tone: 'failed',
      tooltipRows: ['Last sync: 2 hours ago', 'Network error: Connection timed out'],
    });
  });

  it('maps auth_required to yellow sign-in required', () => {
    expect(resolve({
      manualSyncState: { status: 'auth_required', message: 'Your saved session expired' },
    })).toMatchObject({
      label: 'Sign in required',
      tone: 'auth',
      actionTarget: 'auth',
      tooltipRows: ['Last sync: 2 hours ago'],
    });
  });

  it('allows green Synced only when the full provider contract is complete', () => {
    expect(resolve()).toMatchObject({ label: 'Synced', tone: 'synced', iconState: 'sync' });
    expect(resolve({ manualSyncState: { status: 'ok', justSynced: true } })).toMatchObject({
      label: 'Synced',
      tone: 'synced',
      iconState: 'check',
    });
    expect(resolve({ manualSyncState: { status: 'provider_parser_error' } })).toMatchObject({
      label: 'Sync failed',
      tone: 'failed',
    });
    expect(resolve({ transactionImportStatus: { institutions: [] } })).toMatchObject({
      label: 'Partial sync',
      tone: 'partial',
    });
  });

  it('does not expose a pointless last-sync tooltip for manual providers', () => {
    expect(resolve({
      institution: { id: 31, provider: 'real_estate', name: 'Real Estate' },
      lastSynced: null,
      lastSyncText: 'Never',
      transactionImportStatus: null,
    })).toMatchObject({
      label: 'Manually added',
      tone: 'manual',
      tooltipRows: [],
      tooltip: '',
    });
  });

  it('gives Dashboard and Accounts the same model for identical provider inputs', () => {
    const sharedInputs = {
      institution,
      lastSynced: '2026-08-26T18:00:00Z',
      lastSyncText: '2 hours ago',
      transactionImportStatus: {
        institutions: [{
          institution_id: 12,
          provider: 'bmo',
          current_fetch_status: 'retry_needed',
          history_status: 'retry_needed',
        }],
      },
      nowMs: Date.parse('2026-08-26T20:00:00Z'),
    };
    const accountsModel = resolveProviderSyncDisplay(sharedInputs);
    const dashboardModel = resolveProviderSyncDisplay(sharedInputs);

    expect(dashboardModel).toEqual(accountsModel);
  });
});

describe('transaction import settlement', () => {
  it('keeps active, auth, failed, and complete transaction states distinct', () => {
    expect(getTransactionImportSettlementStatus({
      institutions: [{ institution_id: 12, provider: 'bmo', current_fetch_status: 'running' }],
    }, institution)).toBe('active');
    expect(getTransactionImportSettlementStatus({
      institutions: [{ institution_id: 12, provider: 'bmo', transaction_import_job_status: 'auth_required' }],
    }, institution)).toBe('auth_required');
    expect(getTransactionImportSettlementStatus({
      institutions: [{ institution_id: 12, provider: 'bmo', history_status: 'retry_needed' }],
    }, institution)).toBe('failed');
    expect(getTransactionImportSettlementStatus(completeTransactions, institution)).toBe('complete');
  });

  it('redacts credential-like values before tooltip display', () => {
    expect(sanitizeProviderSyncMessage('token=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234 request failed'))
      .toBe('token: [redacted] request failed');
  });
});
