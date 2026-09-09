import { isPromoDemoActive } from '../components/promoDemoEnvironment';

const PROMO_UPDATE_STATUS = Object.freeze({
  enabled: true,
  status: 'idle',
  message: 'BreakTwenty is up to date.',
  canCheck: true,
  canDownload: false,
  canInstall: false,
  currentVersion: '0.1.0',
  auth: { feed: { private: false } },
});

function isPromoDesktopBoundaryActive() {
  return isPromoDemoActive();
}

export function getDesktopBridge() {
  if (typeof window === 'undefined') {
    return null;
  }
  return window.breaktwentyDesktop || null;
}

export function isDesktopShell() {
  return Boolean(getDesktopBridge()?.isDesktop);
}

export async function getMainWindowZoomStatus() {
  const bridge = getDesktopBridge();
  if (!bridge?.getMainWindowZoom) {
    return null;
  }
  try {
    return await bridge.getMainWindowZoom();
  } catch (_) {
    return null;
  }
}

export function subscribeMainWindowZoomStatus(callback) {
  const bridge = getDesktopBridge();
  if (!bridge?.onMainWindowZoomChange || typeof callback !== 'function') {
    return () => {};
  }
  return bridge.onMainWindowZoomChange(callback);
}

export function subscribeBackendRecovery(callback) {
  const bridge = getDesktopBridge();
  if (!bridge?.backendRecovery?.onRecovered || typeof callback !== 'function') {
    return () => {};
  }
  return bridge.backendRecovery.onRecovered(callback);
}

export async function acknowledgeBackendRecovery(request) {
  const bridge = getDesktopBridge();
  if (!bridge?.backendRecovery?.acknowledge) {
    return { status: 'unavailable' };
  }
  return bridge.backendRecovery.acknowledge(request);
}

export async function revealSupportArchive(archiveId) {
  if (isPromoDesktopBoundaryActive()) {
    return {
      status: 'unavailable',
      message: 'Support archives are disabled in the Promo environment.',
    };
  }
  const bridge = getDesktopBridge();
  if (!bridge?.revealSupportArchive) {
    return {
      status: 'unavailable',
      message: 'Desktop folder opening is not available.',
    };
  }
  try {
    return await bridge.revealSupportArchive({ archiveId });
  } catch (error) {
    return {
      status: 'error',
      message: error?.message || 'Could not open the support archive folder.',
    };
  }
}

export async function listDesktopAppDiagnostics() {
  if (isPromoDesktopBoundaryActive()) {
    return {
      status: 'ok',
      incidents: [],
      policy: null,
    };
  }
  const bridge = getDesktopBridge();
  if (!bridge?.appDiagnostics?.list) {
    return {
      status: 'unavailable',
      message: 'Application diagnostics are available in the desktop app.',
      incidents: [],
    };
  }
  try {
    return await bridge.appDiagnostics.list();
  } catch (error) {
    const mainProcessIsStale = String(error?.message || '').includes(
      "No handler registered for 'breaktwenty:app-diagnostics-list'",
    );
    return {
      status: 'error',
      message: mainProcessIsStale
        ? 'Quit and reopen the desktop app once to finish enabling Application diagnostics.'
        : error?.message || 'Application diagnostics could not be loaded.',
      incidents: [],
    };
  }
}

export async function exportDesktopAppDiagnostic(incidentId, userTimezone = '') {
  if (isPromoDesktopBoundaryActive()) {
    return {
      status: 'unavailable',
      message: 'Application diagnostic exports are disabled in the Promo environment.',
    };
  }
  const bridge = getDesktopBridge();
  if (!bridge?.appDiagnostics?.export) {
    return {
      status: 'unavailable',
      message: 'Application diagnostics are available in the desktop app.',
    };
  }
  try {
    return await bridge.appDiagnostics.export({ incidentId, userTimezone });
  } catch (error) {
    const mainProcessIsStale = String(error?.message || '').includes(
      "No handler registered for 'breaktwenty:app-diagnostics-export'",
    );
    return {
      status: 'error',
      message: mainProcessIsStale
        ? 'Quit and reopen the desktop app once to finish enabling Application diagnostics.'
        : error?.message || 'Application diagnostics could not be exported.',
    };
  }
}

export async function getDesktopUpdateStatus() {
  if (isPromoDesktopBoundaryActive()) return { ...PROMO_UPDATE_STATUS };
  const bridge = getDesktopBridge();
  if (!bridge?.updates?.status) {
    return {
      enabled: false,
      status: 'unavailable',
      message: 'Desktop updates are not available.',
      canCheck: false,
      canDownload: false,
      canInstall: false,
    };
  }
  return bridge.updates.status();
}

export async function checkDesktopUpdates() {
  if (isPromoDesktopBoundaryActive()) return { ...PROMO_UPDATE_STATUS };
  const bridge = getDesktopBridge();
  if (!bridge?.updates?.check) {
    return getDesktopUpdateStatus();
  }
  return bridge.updates.check();
}

export async function downloadDesktopUpdate() {
  if (isPromoDesktopBoundaryActive()) return { ...PROMO_UPDATE_STATUS };
  const bridge = getDesktopBridge();
  if (!bridge?.updates?.download) {
    return getDesktopUpdateStatus();
  }
  return bridge.updates.download();
}

export async function installDesktopUpdate() {
  if (isPromoDesktopBoundaryActive()) return { ...PROMO_UPDATE_STATUS };
  const bridge = getDesktopBridge();
  if (!bridge?.updates?.install) {
    return getDesktopUpdateStatus();
  }
  return bridge.updates.install();
}

export async function saveDesktopUpdateToken(token) {
  if (isPromoDesktopBoundaryActive()) {
    return {
      ...PROMO_UPDATE_STATUS,
      actionStatus: 'error',
      actionMessage: 'Desktop update credentials are disabled in the Promo environment.',
    };
  }
  const bridge = getDesktopBridge();
  if (!bridge?.updates?.saveToken) {
    return {
      ...(await getDesktopUpdateStatus()),
      actionStatus: 'error',
      actionMessage: 'Desktop update token storage is not available.',
    };
  }
  return bridge.updates.saveToken({ token });
}

export async function clearDesktopUpdateToken() {
  if (isPromoDesktopBoundaryActive()) return { ...PROMO_UPDATE_STATUS };
  const bridge = getDesktopBridge();
  if (!bridge?.updates?.clearToken) {
    return {
      ...(await getDesktopUpdateStatus()),
      actionStatus: 'error',
      actionMessage: 'Desktop update token storage is not available.',
    };
  }
  return bridge.updates.clearToken();
}

export function subscribeDesktopUpdateStatus(callback) {
  if (isPromoDesktopBoundaryActive()) return () => {};
  const bridge = getDesktopBridge();
  if (!bridge?.updates?.onStatusChange || typeof callback !== 'function') {
    return () => {};
  }
  return bridge.updates.onStatusChange(callback);
}

export async function launchDesktopVisibleAuth(request) {
  if (isPromoDesktopBoundaryActive()) {
    return {
      status: 'error',
      message: 'Secure account connections are disabled in the Promo environment.',
    };
  }
  const bridge = getDesktopBridge();
  if (!bridge?.visibleAuth?.launch) {
    return {
      status: 'error',
      message: 'Desktop visible auth is not available.',
    };
  }
  return bridge.visibleAuth.launch(request);
}

export async function getDesktopVisibleAuthStatus(request) {
  if (isPromoDesktopBoundaryActive()) {
    return {
      status: 'idle',
      message: 'Secure account connections are disabled in the Promo environment.',
    };
  }
  const bridge = getDesktopBridge();
  if (!bridge?.visibleAuth?.status) {
    return {
      status: 'idle',
      message: 'Desktop visible auth is not available.',
    };
  }
  return bridge.visibleAuth.status(request);
}

export async function cancelDesktopVisibleAuth(request) {
  if (isPromoDesktopBoundaryActive()) {
    return {
      status: 'idle',
      message: 'Secure account connections are disabled in the Promo environment.',
    };
  }
  const bridge = getDesktopBridge();
  if (!bridge?.visibleAuth?.cancel) {
    return {
      status: 'idle',
      message: 'Desktop visible auth is not available.',
    };
  }
  return bridge.visibleAuth.cancel(request);
}
