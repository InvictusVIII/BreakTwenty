const { contextBridge, ipcRenderer } = require('electron');

function commandLineValue(name) {
  const prefix = `--${name}=`;
  const argument = process.argv.find((value) => String(value).startsWith(prefix));
  return argument ? String(argument).slice(prefix.length) : '';
}

function invoke(channel, payload) {
  return ipcRenderer.invoke(channel, payload);
}

contextBridge.exposeInMainWorld('breaktwentyDesktop', {
  isDesktop: true,
  runtime: {
    platform: commandLineValue('breaktwenty-platform'),
    isPackaged: commandLineValue('breaktwenty-packaged') === '1',
  },
  getLaunchAuth: () => invoke('breaktwenty:launch-auth'),
  getStatus: () => invoke('breaktwenty:desktop-status'),
  getMainWindowZoom: () => invoke('breaktwenty:main-window-zoom'),
  backendRecovery: {
    acknowledge: (request) => invoke('breaktwenty:backend-recovery-acknowledge', request),
    onRecovered: (callback) => {
      if (typeof callback !== 'function') return () => {};
      const listener = (_event, recovery) => callback(recovery);
      ipcRenderer.on('breaktwenty:backend-recovered', listener);
      return () => {
        ipcRenderer.removeListener('breaktwenty:backend-recovered', listener);
      };
    },
  },
  onMainWindowZoomChange: (callback) => {
    if (typeof callback !== 'function') {
      return () => {};
    }
    const listener = (_event, status) => callback(status);
    ipcRenderer.on('breaktwenty:main-window-zoom-changed', listener);
    return () => {
      ipcRenderer.removeListener('breaktwenty:main-window-zoom-changed', listener);
    };
  },
  revealSupportArchive: (request) => invoke('breaktwenty:reveal-support-archive', request),
  appDiagnostics: {
    list: () => invoke('breaktwenty:app-diagnostics-list'),
    export: (request) => invoke('breaktwenty:app-diagnostics-export', request),
  },
  updates: {
    status: () => invoke('breaktwenty:updates-status'),
    check: () => invoke('breaktwenty:updates-check'),
    download: () => invoke('breaktwenty:updates-download'),
    install: () => invoke('breaktwenty:updates-install'),
    saveToken: (request) => invoke('breaktwenty:updates-save-token', request),
    clearToken: () => invoke('breaktwenty:updates-clear-token'),
    onStatusChange: (callback) => {
      if (typeof callback !== 'function') {
        return () => {};
      }
      const listener = (_event, status) => callback(status);
      ipcRenderer.on('breaktwenty:updates-changed', listener);
      return () => {
        ipcRenderer.removeListener('breaktwenty:updates-changed', listener);
      };
    },
  },
  visibleAuth: {
    launch: (request) => invoke('breaktwenty:visible-auth-launch', request),
    status: (request) => invoke('breaktwenty:visible-auth-status', request),
    cancel: (request) => invoke('breaktwenty:visible-auth-cancel', request),
  },
});
