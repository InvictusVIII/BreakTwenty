import { describe, expect, it, vi } from 'vitest';

import { reconcileRendererAfterBackendRecovery } from './backendRecovery';

describe('reconcileRendererAfterBackendRecovery', () => {
  it('reconnects SSE and performs only read reconciliation once', async () => {
    const handledRecoveryIds = new Set();
    const reconnectServerEvents = vi.fn().mockResolvedValue({ status: 'connected' });
    const fetchData = vi.fn().mockResolvedValue({ networth: {} });
    const fetchAllScopeInstitutions = vi.fn().mockResolvedValue([]);
    const acknowledge = vi.fn().mockResolvedValue({ status: 'ok' });
    const startProviderWork = vi.fn();
    const options = {
      recovery: { recoveryId: '784ebef1-2642-46c7-9c82-728d594be019' },
      handledRecoveryIds,
      reconnectServerEvents,
      fetchData,
      fetchAllScopeInstitutions,
      acknowledge,
    };

    await expect(reconcileRendererAfterBackendRecovery(options)).resolves.toEqual({
      status: 'reconciled',
    });
    await expect(reconcileRendererAfterBackendRecovery(options)).resolves.toEqual({
      status: 'ignored',
    });

    expect(reconnectServerEvents).toHaveBeenCalledTimes(1);
    expect(fetchData).toHaveBeenCalledTimes(1);
    expect(fetchData).toHaveBeenCalledWith({ forceDataRefreshId: true });
    expect(fetchAllScopeInstitutions).toHaveBeenCalledTimes(1);
    expect(acknowledge).toHaveBeenCalledWith({
      recoveryId: options.recovery.recoveryId,
      status: 'ok',
    });
    expect(startProviderWork).not.toHaveBeenCalled();
  });

  it('reports reconciliation failure so the main process can fall back', async () => {
    const acknowledge = vi.fn().mockResolvedValue({ status: 'ok' });
    await expect(reconcileRendererAfterBackendRecovery({
      recovery: { recoveryId: '37e7a0c4-f8f8-42b9-8f4e-8fef60a20f25' },
      handledRecoveryIds: new Set(),
      reconnectServerEvents: vi.fn().mockRejectedValue(new Error('SSE unavailable')),
      fetchData: vi.fn(),
      fetchAllScopeInstitutions: vi.fn(),
      acknowledge,
    })).resolves.toEqual({ status: 'failed', message: 'SSE unavailable' });
    expect(acknowledge).toHaveBeenCalledWith({
      recoveryId: '37e7a0c4-f8f8-42b9-8f4e-8fef60a20f25',
      status: 'error',
      message: 'SSE unavailable',
    });
  });
});
