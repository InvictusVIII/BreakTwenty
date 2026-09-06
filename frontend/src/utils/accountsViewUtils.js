import { API } from '../config';
import { isManualProvider } from '../constants/providers';

export function buildInstitutionSyncRequestBody(institutionId, syncId, syncSource) {
  return {
    ...(syncId ? { sync_id: syncId } : {}),
    institution_id: institutionId,
    sync_source: syncSource,
  };
}

export function shouldShowAccountsDetailSync(provider, account = null) {
  return !isManualProvider(provider) && !account?.is_imported;
}

export function getImportStatusDetail(item) {
  const status = item?.history_status || 'not_started';
  if (status === 'complete') {
    return null;
  }
  const totalAccounts = Number(item.total_accounts || 0);
  if (totalAccounts > 0) {
    const completedAccounts = Number(item.completed_accounts || 0);
    if (
      status === 'running'
      || status === 'queued'
      || status === 'retry_needed'
      || completedAccounts < totalAccounts
    ) {
      const accountLabel = totalAccounts === 1 ? 'account' : 'accounts';
      return `${completedAccounts} of ${totalAccounts} ${accountLabel} complete`;
    }
    return null;
  }
  const completed = Number(item.completed_windows || 0);
  const total = Number(item.total_windows || 0);
  if (!total) {
    return null;
  }
  if (
    status === 'running'
    || status === 'queued'
    || status === 'retry_needed'
    || Number(item.pending_windows || 0) > 0
    || Number(item.failed_windows || 0) > 0
  ) {
    return `${completed}/${total} ranges complete`;
  }
  return null;
}

export function buildAccountsDetailRecentTransactionsUrl(accountId) {
  const params = new URLSearchParams();
  params.set('limit', '5');
  params.set('offset', '0');
  params.set('account_ids', String(accountId));
  return `${API}/transactions?${params.toString()}`;
}
