import { describe, expect, it } from 'vitest';
import { reconcileSyncNotifierActivities } from './syncNotifier';

describe('sync rail notifier reconciliation', () => {
  const institutions = [
    { id: 1, provider: 'ibkr', name: 'Interactive Brokers' },
    { id: 2, provider: 'coinbase', name: 'Coinbase' },
    { id: 3, provider: 'questrade', name: 'Questrade' },
    { id: 4, provider: 'wise', name: 'Wise' },
  ];

  it('shows only unfinished batch entries when optimistic import snapshots are stale', () => {
    const autoSyncStates = Object.fromEntries(institutions.map((institution) => [
      String(institution.id),
      {
        status: 'syncing',
        awaitingTransactionImport: institution.id !== 1,
        provider: institution.provider,
        institutionId: institution.id,
        label: institution.name,
      },
    ]));
    const activeSyncBatches = [{
      batch_id: 'batch-interrupted',
      status: 'running',
      results: {
        'connection:1': { institution_id: 1, provider: 'ibkr', status: 'syncing' },
        'connection:2': { institution_id: 2, provider: 'coinbase', status: 'ok' },
        'connection:3': { institution_id: 3, provider: 'questrade', status: 'ok' },
        'connection:4': { institution_id: 4, provider: 'wise', status: 'ok' },
      },
    }];

    expect(reconcileSyncNotifierActivities({
      autoSyncStates,
      activeSyncBatches,
      institutions,
      autoSyncInProgress: true,
    })).toEqual([{
      key: 'sync-batch:batch-interrupted:1',
      provider: 'ibkr',
      institutionId: 1,
      label: 'Interactive Brokers',
      tooltip: 'Syncing Interactive Brokers',
    }]);
  });

  it('uses durable transaction-import status after the provider batch entry finishes', () => {
    const activities = reconcileSyncNotifierActivities({
      autoSyncStates: {
        2: {
          status: 'syncing',
          awaitingTransactionImport: true,
          provider: 'coinbase',
          institutionId: 2,
        },
      },
      activeSyncBatches: [{
        batch_id: 'batch-running',
        status: 'running',
        results: {
          'connection:2': { institution_id: 2, provider: 'coinbase', status: 'ok' },
        },
      }],
      transactionImportStatus: {
        institutions: [{
          institution_id: 2,
          institution: 'Coinbase',
          provider: 'coinbase',
          transaction_import_job_status: 'queued',
        }],
      },
      institutions,
      autoSyncInProgress: true,
    });

    expect(activities).toHaveLength(1);
    expect(activities[0]).toMatchObject({
      key: 'transaction-import:2',
      institutionId: 2,
      tooltip: 'Fetching Coinbase transactions',
    });
  });

  it('keeps immediate optimistic activity before a batch snapshot arrives', () => {
    expect(reconcileSyncNotifierActivities({
      autoSyncStates: {
        4: { status: 'syncing', provider: 'wise', institutionId: 4 },
      },
      institutions,
      autoSyncInProgress: true,
    })).toMatchObject([{
      key: 'optimistic:4',
      institutionId: 4,
      label: 'Wise',
    }]);
  });
});
