import { describe, expect, it } from 'vitest';
import {
  buildAccountsDetailRecentTransactionsUrl,
  buildInstitutionSyncRequestBody,
  getImportStatusDetail,
  shouldShowAccountsDetailSync,
} from '../utils/accountsViewUtils';

describe('Accounts sync initiation metadata', () => {
  it('preserves the source and optional sync ID in provider requests', () => {
    expect(buildInstitutionSyncRequestBody(17, 'moomoo-sync-1', 'sync_all')).toEqual({
      sync_id: 'moomoo-sync-1',
      institution_id: 17,
      sync_source: 'sync_all',
    });
    expect(buildInstitutionSyncRequestBody(17, null, 'individual_sync')).toEqual({
      institution_id: 17,
      sync_source: 'individual_sync',
    });
  });
});

describe('Accounts detail tray transaction loading', () => {
  it('filters recent transactions to the selected account', () => {
    const url = new URL(buildAccountsDetailRecentTransactionsUrl(42));

    expect(url.pathname).toBe('/api/transactions');
    expect(url.searchParams.get('limit')).toBe('5');
    expect(url.searchParams.get('offset')).toBe('0');
    expect(url.searchParams.get('account_ids')).toBe('42');
    expect(url.searchParams.has('account_id')).toBe(false);
  });
});

describe('Accounts detail tray transaction progress', () => {
  it('uses compact range progress copy that remains readable in the status column', () => {
    expect(getImportStatusDetail({
      history_status: 'running',
      completed_windows: 6,
      total_windows: 11,
      pending_windows: 5,
    })).toBe('6/11 ranges complete');
  });
});

describe('Accounts detail tray sync timestamps', () => {
  it('shows them only for accounts managed by a regular provider sync', () => {
    expect(shouldShowAccountsDetailSync('bmo', { is_imported: false })).toBe(true);
    expect(shouldShowAccountsDetailSync('real_estate', { is_imported: false })).toBe(false);
    expect(shouldShowAccountsDetailSync('ibkr', { is_imported: true })).toBe(false);
    expect(shouldShowAccountsDetailSync('questrade', { is_imported: true })).toBe(false);
  });

});
