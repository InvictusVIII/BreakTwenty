import { getProviderDisplayName } from '../constants/providers';
import { getActiveSyncBatchConnectionStates } from './syncBatch';

const ACTIVE_ACTIVITY_STATUSES = new Set(['pending', 'queued', 'running', 'syncing']);

function normalizedStatus(value) {
  return String(value || '').trim().toLowerCase();
}

function institutionIdentity(value) {
  const identity = String(value || '').trim();
  return identity && identity !== '0' ? identity : '';
}

function activityIdentity(activity) {
  return institutionIdentity(activity?.institutionId)
    || String(activity?.provider || activity?.key || '').trim();
}

function institutionDetails(institutionId, provider, label, institutionsById) {
  const institution = institutionsById.get(Number(institutionId || 0));
  const normalizedProvider = String(provider || institution?.provider || '').trim();
  return {
    provider: normalizedProvider,
    label: String(
      label
      || institution?.name
      || getProviderDisplayName(normalizedProvider)
      || 'Institution'
    ).trim(),
  };
}

export function reconcileSyncNotifierActivities({
  activityItems = [],
  autoSyncStates = {},
  accountSyncActivities = [],
  activeSyncBatches = [],
  transactionImportStatus = null,
  institutions = [],
  autoSyncInProgress = false,
} = {}) {
  const items = [];
  const seenConnections = new Set();
  const addActivity = (activity) => {
    const identity = activityIdentity(activity);
    if (!identity || seenConnections.has(identity)) return;
    seenConnections.add(identity);
    items.push(activity);
  };
  (Array.isArray(activityItems) ? activityItems : []).forEach(addActivity);

  const institutionsById = new Map(
    (Array.isArray(institutions) ? institutions : [])
      .map((institution) => [Number(institution?.id || 0), institution])
      .filter(([institutionId]) => institutionId > 0),
  );
  const activeTransactionConnections = new Set();
  const transactionInstitutions = Array.isArray(transactionImportStatus?.institutions)
    ? transactionImportStatus.institutions
    : [];
  transactionInstitutions.forEach((institution) => {
    const institutionId = Number(institution?.institution_id || 0);
    const currentFetchStatus = normalizedStatus(institution?.current_fetch_status);
    const jobStatus = normalizedStatus(institution?.transaction_import_job_status);
    if (
      institutionId <= 0
      || (!ACTIVE_ACTIVITY_STATUSES.has(currentFetchStatus)
        && !ACTIVE_ACTIVITY_STATUSES.has(jobStatus))
    ) return;
    const identity = String(institutionId);
    activeTransactionConnections.add(identity);
    const details = institutionDetails(
      institutionId,
      institution?.provider,
      institution?.institution,
      institutionsById,
    );
    addActivity({
      key: `transaction-import:${institutionId}`,
      provider: details.provider,
      institutionId,
      label: details.label,
      tooltip: `Fetching ${details.label} transactions`,
    });
  });

  const batchStates = getActiveSyncBatchConnectionStates(activeSyncBatches);
  batchStates.forEach((state, institutionId) => {
    if (!ACTIVE_ACTIVITY_STATUSES.has(normalizedStatus(state?.status))) return;
    const details = institutionDetails(
      institutionId,
      state?.provider,
      state?.institution,
      institutionsById,
    );
    addActivity({
      key: `sync-batch:${state?.batch_id || 'active'}:${institutionId}`,
      provider: details.provider,
      institutionId,
      label: details.label,
      tooltip: `Syncing ${details.label}`,
    });
  });

  (Array.isArray(accountSyncActivities) ? accountSyncActivities : []).forEach(addActivity);

  Object.entries(autoSyncStates || {}).forEach(([connectionKey, state]) => {
    const institutionId = Number(state?.institutionId || connectionKey || 0);
    const identity = institutionIdentity(institutionId || connectionKey);
    const provider = String(state?.provider || '').trim();
    const status = normalizedStatus(state?.status);
    if (!identity || !provider || seenConnections.has(identity)) return;
    if (!ACTIVE_ACTIVITY_STATUSES.has(status)) return;

    const batchState = institutionId > 0 ? batchStates.get(institutionId) : null;
    if (batchState && !ACTIVE_ACTIVITY_STATUSES.has(normalizedStatus(batchState.status))) {
      return;
    }
    if (state?.awaitingTransactionImport && !activeTransactionConnections.has(identity)) {
      if (batchState || !autoSyncInProgress) return;
    } else if (!batchState && !autoSyncInProgress) {
      return;
    }

    const details = institutionDetails(
      institutionId,
      provider,
      state?.label,
      institutionsById,
    );
    addActivity({
      key: `optimistic:${identity}`,
      provider: details.provider,
      institutionId: institutionId || null,
      label: details.label,
      tooltip: state?.awaitingTransactionImport
        ? `Fetching ${details.label} transactions`
        : `Syncing ${details.label}`,
    });
  });

  return items.sort((left, right) => left.label.localeCompare(right.label));
}
