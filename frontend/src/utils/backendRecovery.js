function recoveryIdFrom(recovery) {
  return String(recovery?.recoveryId || '').trim();
}

export async function reconcileRendererAfterBackendRecovery({
  recovery,
  handledRecoveryIds,
  reconnectServerEvents,
  fetchData,
  fetchAllScopeInstitutions,
  acknowledge,
}) {
  const recoveryId = recoveryIdFrom(recovery);
  if (!recoveryId || handledRecoveryIds.has(recoveryId)) {
    return { status: 'ignored' };
  }
  handledRecoveryIds.add(recoveryId);
  if (handledRecoveryIds.size > 32) {
    handledRecoveryIds.delete(handledRecoveryIds.values().next().value);
  }

  try {
    await reconnectServerEvents();
    const [appData, scopeInstitutions] = await Promise.all([
      fetchData({ forceDataRefreshId: true }),
      fetchAllScopeInstitutions(),
    ]);
    if (appData === null || scopeInstitutions === null) {
      throw new Error('Renderer reconciliation did not complete.');
    }
    await acknowledge({ recoveryId, status: 'ok' });
    return { status: 'reconciled' };
  } catch (error) {
    const message = String(error?.message || 'Renderer reconciliation failed.').slice(0, 500);
    try {
      await acknowledge({ recoveryId, status: 'error', message });
    } catch (_) {
      // The main process owns the bounded timeout and hard-reload fallback.
    }
    return { status: 'failed', message };
  }
}
