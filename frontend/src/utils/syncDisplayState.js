import {
  hasProviderTransactionImport,
  isManualProvider,
} from '../constants/providers';

export const TRANSACTION_IMPORT_RETRY_STATUS = 'transaction_import_retry_needed';

const ACTIVE_STATUSES = new Set([
  'pending',
  'queued',
  'running',
  'syncing',
  'pending_backend',
  'already_syncing',
]);
const AUTH_STATUSES = new Set([
  'auth_required',
  'different_profile_detected',
  'verification_required',
  'mfa_required',
  'login_required',
  'reauth_required',
  'approval_required',
]);
const NETWORK_STATUSES = new Set(['network_error']);
const FAILURE_STATUSES = new Set(['error', 'error_flex', 'error_scraper']);
const SUCCESS_STATUSES = new Set(['ok', 'skipped']);
const TRANSACTION_ACTIVE_STATUSES = new Set(['queued', 'running']);
const TRANSACTION_FAILURE_STATUSES = new Set(['failed', 'retry_needed', 'needs_retry']);

const DISPLAY_BY_TONE = {
  failed: { label: 'Sync failed', priority: 0, iconState: 'attention', actionTarget: 'sync' },
  auth: { label: 'Sign in required', priority: 1, iconState: 'sync', actionTarget: 'auth' },
  partial: { label: 'Partial sync', priority: 2, iconState: 'attention', actionTarget: 'sync' },
  syncing: { label: 'Syncing', priority: 3, iconState: 'spinner', actionTarget: null },
  never: { label: 'Never synced', priority: 4, iconState: 'sync', actionTarget: 'sync' },
  manual: { label: 'Manually added', priority: 5, iconState: 'dot', actionTarget: null },
  synced: { label: 'Synced', priority: 6, iconState: 'sync', actionTarget: 'sync' },
};

function normalizeStatus(value) {
  return String(value || '').trim().toLowerCase();
}

function normalizeAccountStatus(value) {
  const status = normalizeStatus(value);
  if (!status) return '';
  if (status === 'skipped') return 'ok';
  return status;
}

function parseTimestamp(value) {
  const timestamp = Date.parse(String(value || ''));
  return Number.isFinite(timestamp) ? timestamp : null;
}

function shouldUseBatchTransactionJob(payload, importInstitution, transactionJob) {
  if (!transactionJob) return false;
  if (!importInstitution) return true;

  const batchJobId = String(transactionJob.job_id || '').trim();
  const liveJobId = String(importInstitution.transaction_import_job_id || '').trim();
  if (batchJobId && liveJobId && batchJobId === liveJobId) {
    return false;
  }

  const payloadGeneratedAt = parseTimestamp(payload?.generated_at);
  const batchJobUpdatedAt = parseTimestamp(transactionJob.updated_at);
  if (payloadGeneratedAt !== null && batchJobUpdatedAt !== null) {
    return payloadGeneratedAt < batchJobUpdatedAt;
  }

  return true;
}

function matchesInstitution(candidate, institution) {
  if (!candidate || !institution) return false;
  const institutionId = Number(institution.id || 0);
  const candidateInstitutionId = Number(candidate.institution_id || candidate.institutionId || 0);
  if (institutionId > 0 && candidateInstitutionId > 0) {
    return institutionId === candidateInstitutionId;
  }
  const provider = String(institution.provider || '').trim();
  const candidateProvider = String(candidate.provider || '').trim();
  return Boolean(provider && candidateProvider && provider === candidateProvider);
}

function getActiveActivity(syncActivity, institution) {
  return (Array.isArray(syncActivity?.active) ? syncActivity.active : []).find((activity) => (
    matchesInstitution(activity, institution)
    && TRANSACTION_ACTIVE_STATUSES.has(normalizeStatus(activity?.status))
  )) || null;
}

export function isCurrentAutosyncState(autoState, autoSyncInProgress) {
  return Boolean(
    autoState
    && (autoSyncInProgress || autoState.awaitingTransactionImport || autoState.justSynced)
  );
}

export function isProviderAuthStatus(status) {
  return AUTH_STATUSES.has(normalizeStatus(status));
}

export function findTransactionImportInstitution(payload, institution) {
  const institutions = Array.isArray(payload?.institutions) ? payload.institutions : [];
  if (!institution) return null;
  return institutions.find((item) => {
    const sameId = item.institution_id !== undefined
      && item.institution_id !== null
      && String(item.institution_id) === String(institution.id);
    const sameProvider = item.provider && institution.provider
      && String(item.provider) === String(institution.provider);
    const sameName = item.institution && institution.name
      && String(item.institution) === String(institution.name);
    return sameId || (sameProvider && sameName) || sameProvider;
  }) || null;
}

export function sanitizeProviderSyncMessage(value, fallback = '') {
  let message = String(value || '').replace(/\s+/g, ' ').trim();
  if (!message) return fallback;
  message = message
    .replace(/\b(password|passcode|pin|secret|token|cookie|authorization|bearer|account(?:\s+(?:number|id|key))?)\s*[:=]\s*[^,;\s]+/gi, '$1: [redacted]')
    .replace(/\b[A-Fa-f0-9]{32,}\b/g, '[redacted]')
    .replace(/\b[A-Za-z0-9+/_-]{48,}={0,2}\b/g, '[redacted]')
    .replace(/([?&](?:token|code|key|secret|password|account(?:_?id)?)=)[^&\s]+/gi, '$1[redacted]');
  return message.slice(0, 320) || fallback;
}

export function getTransactionImportSettlementStatus(payload, institution, transactionJob = null) {
  if (!hasProviderTransactionImport(institution?.provider)) return 'not_applicable';
  const importInstitution = findTransactionImportInstitution(payload, institution);
  const useBatchTransactionJob = shouldUseBatchTransactionJob(
    payload,
    importInstitution,
    transactionJob,
  );
  const jobStatus = normalizeStatus(
    useBatchTransactionJob
      ? transactionJob?.status
      : importInstitution?.transaction_import_job_status,
  );
  const currentFetchStatus = normalizeStatus(importInstitution?.current_fetch_status);
  const historyStatus = normalizeStatus(importInstitution?.history_status || 'not_started');
  const status = normalizeStatus(importInstitution?.status);

  if (
    TRANSACTION_ACTIVE_STATUSES.has(jobStatus)
    || TRANSACTION_ACTIVE_STATUSES.has(currentFetchStatus)
  ) {
    return 'active';
  }
  if (AUTH_STATUSES.has(jobStatus) || AUTH_STATUSES.has(status)) {
    return 'auth_required';
  }
  if (
    TRANSACTION_FAILURE_STATUSES.has(jobStatus)
    || TRANSACTION_FAILURE_STATUSES.has(currentFetchStatus)
    || TRANSACTION_FAILURE_STATUSES.has(status)
  ) {
    return 'failed';
  }
  if (TRANSACTION_ACTIVE_STATUSES.has(historyStatus)) return 'active';
  if (TRANSACTION_FAILURE_STATUSES.has(historyStatus)) return 'failed';
  if (importInstitution && historyStatus === 'complete' && (!currentFetchStatus || currentFetchStatus === 'idle')) {
    return 'complete';
  }
  return 'unknown';
}

export function getTransactionImportIssueMessage(payload, institution, fallback = 'Transaction import is incomplete') {
  const importInstitution = findTransactionImportInstitution(payload, institution);
  return sanitizeProviderSyncMessage(
    importInstitution?.last_error
      || importInstitution?.transaction_import_job_last_progress_label
      || importInstitution?.current_fetch_label
      || importInstitution?.history_status_label,
    fallback,
  );
}

function getLatestAccountState({
  institution,
  manualSyncState,
  autoSyncState,
  autoSyncInProgress,
  batchState,
}) {
  const candidates = [
    manualSyncState,
    batchState,
    isCurrentAutosyncState(autoSyncState, autoSyncInProgress) ? autoSyncState : null,
    institution ? { status: institution.sync_status } : null,
  ];
  const candidate = candidates.find((item) => normalizeStatus(item?.status));
  const status = normalizeAccountStatus(candidate?.status);
  return {
    candidate,
    status,
    message: sanitizeProviderSyncMessage(candidate?.message),
  };
}

function buildDisplayModel(tone, {
  lastSyncText,
  includeLastSync = true,
  iconState = null,
  warningRow = null,
  sourceStatus = '',
  message = '',
  actionTarget = undefined,
} = {}) {
  const display = DISPLAY_BY_TONE[tone];
  const resolvedIconState = iconState || display.iconState;
  const tooltipRows = includeLastSync ? [`Last sync: ${lastSyncText || 'Never'}`] : [];
  if (warningRow) tooltipRows.push(warningRow);
  return {
    ...display,
    tone,
    colorClass: `is-${tone}`,
    iconState: resolvedIconState,
    spin: resolvedIconState === 'spinner',
    actionTarget: actionTarget === undefined ? display.actionTarget : actionTarget,
    sourceStatus,
    message,
    tooltipRows,
    tooltip: tooltipRows.join('\n'),
  };
}

export function resolveProviderSyncDisplay({
  institution,
  lastSynced = null,
  lastSyncText = null,
  manualSyncState = null,
  autoSyncState = null,
  autoSyncInProgress = false,
  batchState = null,
  syncActivity = null,
  transactionImportStatus = null,
} = {}) {
  const provider = String(institution?.provider || '').trim();
  const resolvedLastSyncText = lastSyncText || (lastSynced ? 'Unknown' : 'Never');
  if (!provider || isManualProvider(provider)) {
    return buildDisplayModel('manual', {
      lastSyncText: resolvedLastSyncText,
      includeLastSync: false,
    });
  }

  const transactionJob = batchState?.transaction_import_job || null;
  const transactionSettlement = getTransactionImportSettlementStatus(
    transactionImportStatus,
    institution,
    transactionJob,
  );
  const accountState = getLatestAccountState({
    institution,
    manualSyncState,
    autoSyncState,
    autoSyncInProgress,
    batchState,
  });
  const status = accountState.status;
  const activeActivity = getActiveActivity(syncActivity, institution);
  const waitingOnTransactions = Boolean(
    manualSyncState?.awaitingTransactionImport || autoSyncState?.awaitingTransactionImport,
  );
  const justSynced = Boolean(manualSyncState?.justSynced || autoSyncState?.justSynced);
  const accountPhaseActive = ACTIVE_STATUSES.has(status)
    && !(waitingOnTransactions && ['auth_required', 'failed', 'complete'].includes(transactionSettlement));

  if (activeActivity || accountPhaseActive || transactionSettlement === 'active') {
    return buildDisplayModel('syncing', {
      lastSyncText: resolvedLastSyncText,
      sourceStatus: status || normalizeStatus(activeActivity?.status),
    });
  }

  const isOtherTerminalFailure = Boolean(
    status
    && status !== TRANSACTION_IMPORT_RETRY_STATUS
    && !ACTIVE_STATUSES.has(status)
    && !AUTH_STATUSES.has(status)
    && !SUCCESS_STATUSES.has(status),
  );
  if (NETWORK_STATUSES.has(status) || FAILURE_STATUSES.has(status) || isOtherTerminalFailure) {
    const isNetwork = NETWORK_STATUSES.has(status);
    const message = accountState.message || (isNetwork
      ? 'Could not connect to this provider'
      : 'The provider refresh did not complete');
    return buildDisplayModel('failed', {
      lastSyncText: resolvedLastSyncText,
      sourceStatus: status,
      message,
      actionTarget: isNetwork ? 'sync' : 'credentials',
      warningRow: `${isNetwork ? 'Network error' : 'Sync failed'}: ${message}`,
    });
  }

  if (AUTH_STATUSES.has(status) || transactionSettlement === 'auth_required') {
    const message = accountState.message || getTransactionImportIssueMessage(
      transactionImportStatus,
      institution,
      'Sign in again to continue syncing',
    );
    return buildDisplayModel('auth', {
      lastSyncText: resolvedLastSyncText,
      sourceStatus: status || 'auth_required',
      message,
    });
  }

  const accountSucceeded = SUCCESS_STATUSES.has(status) || Boolean(lastSynced);
  if (transactionSettlement === 'failed' || (transactionSettlement === 'unknown' && accountSucceeded)) {
    const message = getTransactionImportIssueMessage(transactionImportStatus, institution);
    return buildDisplayModel('partial', {
      lastSyncText: resolvedLastSyncText,
      sourceStatus: TRANSACTION_IMPORT_RETRY_STATUS,
      message,
      warningRow: `Retry needed: ${message}`,
    });
  }

  if (
    accountSucceeded
    && (transactionSettlement === 'complete' || transactionSettlement === 'not_applicable')
    && lastSynced
  ) {
    return buildDisplayModel('synced', {
      lastSyncText: resolvedLastSyncText,
      sourceStatus: status || 'ok',
      iconState: justSynced ? 'check' : 'sync',
    });
  }

  return buildDisplayModel('never', {
    lastSyncText: resolvedLastSyncText,
    sourceStatus: status,
  });
}
