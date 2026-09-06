import { isPlainObject, readJsonResponse } from './apiResponse';

export const VISIBILITY_UPDATE_TIMEOUT_MS = 15 * 1000;
const VISIBILITY_UPDATE_CONCURRENCY = 4;

function operationKey(operation) {
  return `${operation.kind}:${operation.id}`;
}

function visibilityOperations(institutions, previousInstitutions = null) {
  const previousHiddenByKey = previousInstitutions
    ? new Map(visibilityOperations(previousInstitutions).map((operation) => (
      [operationKey(operation), operation.hidden]
    )))
    : null;

  const operations = (institutions || []).flatMap((institution) => ([
    {
      kind: 'institution',
      id: institution.id,
      hidden: Boolean(institution.hidden),
    },
    ...(institution.accounts || []).map((account) => ({
      kind: 'account',
      id: account.id,
      hidden: Boolean(account.hidden),
    })),
  ]));

  if (!previousHiddenByKey) return operations;

  return operations.filter((operation) => {
    const previousHidden = previousHiddenByKey.get(operationKey(operation));
    return previousHidden === undefined || previousHidden !== operation.hidden;
  });
}

async function allSettledWithConcurrency(items, limit, worker) {
  const results = new Array(items.length);
  let nextIndex = 0;
  const workerCount = Math.max(1, Math.min(Number(limit) || 1, items.length));

  await Promise.all(Array.from({ length: workerCount }, async () => {
    while (nextIndex < items.length) {
      const index = nextIndex;
      nextIndex += 1;
      try {
        results[index] = { status: 'fulfilled', value: await worker(items[index]) };
      } catch (reason) {
        results[index] = { status: 'rejected', reason };
      }
    }
  }));

  return results;
}

async function persistVisibilityOperation({
  apiBase,
  fetchImpl,
  operation,
  timeoutMs,
}) {
  const collection = operation.kind === 'institution' ? 'institutions' : 'accounts';
  const label = `${operation.kind === 'institution' ? 'Institution' : 'Account'} visibility update`;
  const timeout = Number(timeoutMs);
  const canAbort = typeof AbortController !== 'undefined' && Number.isFinite(timeout) && timeout > 0;
  const controller = canAbort ? new AbortController() : null;
  let timedOut = false;
  let timeoutId = null;

  try {
    const request = (async () => {
      const response = await fetchImpl(`${apiBase}/${collection}/${operation.id}/hidden`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hidden: operation.hidden }),
        ...(controller ? { signal: controller.signal } : {}),
      });
      await readJsonResponse(response, {
        label,
        validate: (payload) => isPlainObject(payload) && payload.status === 'ok',
      });
    })();
    if (canAbort) {
      await Promise.race([
        request,
        new Promise((_, reject) => {
          timeoutId = setTimeout(() => {
            timedOut = true;
            controller.abort();
            reject(new Error(`${label} timed out.`));
          }, timeout);
        }),
      ]);
    } else {
      await request;
    }
  } catch (error) {
    if (timedOut) {
      throw new Error(`${label} timed out.`, { cause: error });
    }
    throw error;
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }
}

export async function persistVisibilityScope({
  apiBase,
  institutions,
  previousInstitutions = null,
  fetchImpl = fetch,
  timeoutMs = VISIBILITY_UPDATE_TIMEOUT_MS,
}) {
  const operations = visibilityOperations(institutions, previousInstitutions);
  const results = await allSettledWithConcurrency(
    operations,
    VISIBILITY_UPDATE_CONCURRENCY,
    (operation) => persistVisibilityOperation({
      apiBase,
      fetchImpl,
      operation,
      timeoutMs,
    }),
  );
  const failures = results.filter((result) => result.status === 'rejected');
  if (failures.length > 0) {
    const firstError = failures[0].reason;
    const suffix = failures.length > 1 ? ` (${failures.length} updates failed)` : '';
    throw new Error(`${firstError?.message || 'Could not save visibility.'}${suffix}`);
  }
}

export async function reconcileVisibilityScope({
  onDataChange,
  fetchAllScopeInstitutions,
}) {
  if (onDataChange) {
    await Promise.resolve(onDataChange()).catch(() => null);
  }
  if (!fetchAllScopeInstitutions) return null;
  const institutions = await Promise.resolve(fetchAllScopeInstitutions()).catch(() => null);
  return Array.isArray(institutions) ? institutions : null;
}

export function scopeWithSelectedAccounts(institutions, selectedAccountIds) {
  const selectedIds = selectedAccountIds instanceof Set ? selectedAccountIds : new Set(selectedAccountIds || []);
  return (institutions || []).map((institution) => {
    const accounts = (institution.accounts || []).map((account) => ({
      ...account,
      hidden: !selectedIds.has(account.id),
    }));
    return {
      ...institution,
      hidden: !accounts.some((account) => !account.hidden),
      accounts,
    };
  });
}

export function selectedAccountIdsFromScope(institutions) {
  return new Set((institutions || []).flatMap((institution) => (
    institution.hidden
      ? []
      : (institution.accounts || [])
        .filter((account) => !account.hidden)
        .map((account) => account.id)
  )));
}
