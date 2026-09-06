import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setPromoDemoActive } from '../components/promoDemoEnvironment';
import {
  getDesktopUpdateStatus,
  isDesktopShell,
  launchDesktopVisibleAuth,
  listDesktopAppDiagnostics,
  revealSupportArchive,
  subscribeDesktopUpdateStatus,
} from './desktopBridge';

describe('desktop bridge Promo isolation', () => {
  let bridge;

  beforeEach(() => {
    bridge = {
      isDesktop: true,
      revealSupportArchive: vi.fn().mockResolvedValue({ status: 'ok' }),
      appDiagnostics: {
        list: vi.fn().mockResolvedValue({ status: 'ok', incidents: [{ incidentId: 'real' }] }),
      },
      updates: {
        status: vi.fn().mockResolvedValue({ status: 'real' }),
        onStatusChange: vi.fn(() => () => {}),
      },
      visibleAuth: {
        launch: vi.fn().mockResolvedValue({ status: 'running', attemptId: 'real' }),
      },
    };
    window.breaktwentyDesktop = bridge;
    setPromoDemoActive(true);
  });

  afterEach(() => {
    setPromoDemoActive(false);
    window.localStorage.removeItem('breaktwenty_promo_demo_store_v1');
    delete window.breaktwentyDesktop;
    vi.restoreAllMocks();
  });

  it('keeps shell visuals available while blocking real desktop data and actions', async () => {
    expect(isDesktopShell()).toBe(true);
    expect(await listDesktopAppDiagnostics()).toEqual({ status: 'ok', incidents: [], policy: null });
    expect(await getDesktopUpdateStatus()).toMatchObject({ status: 'idle', currentVersion: '0.1.0' });
    expect(await launchDesktopVisibleAuth({ provider: 'rbc' })).toMatchObject({ status: 'error' });
    expect(await revealSupportArchive('real-archive')).toMatchObject({ status: 'unavailable' });
    expect(subscribeDesktopUpdateStatus(vi.fn())).toEqual(expect.any(Function));

    expect(bridge.appDiagnostics.list).not.toHaveBeenCalled();
    expect(bridge.updates.status).not.toHaveBeenCalled();
    expect(bridge.updates.onStatusChange).not.toHaveBeenCalled();
    expect(bridge.visibleAuth.launch).not.toHaveBeenCalled();
    expect(bridge.revealSupportArchive).not.toHaveBeenCalled();
  });
});
