const { normalizeLocalFrontendUrl } = require('./backendApiUrl');

function assertMainWindowIpcSender(event, { getMainWindow, getFrontendUrl }) {
  const mainWindow = typeof getMainWindow === 'function' ? getMainWindow() : null;
  const sender = event?.sender;
  const senderFrame = event?.senderFrame;
  if (
    !mainWindow
    || typeof mainWindow.isDestroyed !== 'function'
    || mainWindow.isDestroyed()
    || !mainWindow.webContents
    || sender !== mainWindow.webContents
    || !senderFrame
    || senderFrame !== sender.mainFrame
  ) {
    throw new Error('BreakTwenty rejected an untrusted desktop request.');
  }
  const approvedOrigin = normalizeLocalFrontendUrl(
    typeof getFrontendUrl === 'function' ? getFrontendUrl() : '',
  );
  let senderUrl;
  try {
    senderUrl = new URL(String(senderFrame.url || ''));
  } catch (_error) {
    throw new Error('BreakTwenty rejected an untrusted desktop request.');
  }
  if (
    senderUrl.protocol !== 'http:'
    || senderUrl.username
    || senderUrl.password
    || senderUrl.origin !== approvedOrigin
  ) {
    throw new Error('BreakTwenty rejected an untrusted desktop request.');
  }
}

function createMainWindowIpcAuthorizer({ getMainWindow, getFrontendUrl }) {
  return (event) => assertMainWindowIpcSender(event, { getMainWindow, getFrontendUrl });
}

function registerPrivilegedIpcHandler(ipcMain, authorize, channel, handler) {
  if (!ipcMain || typeof ipcMain.handle !== 'function' || typeof authorize !== 'function') {
    throw new Error('BreakTwenty privileged desktop IPC authorization is unavailable.');
  }
  ipcMain.handle(channel, (event, ...args) => {
    authorize(event);
    return handler(...args);
  });
}

function registerPrivilegedIpcListener(ipcMain, authorize, channel, handler) {
  if (!ipcMain || typeof ipcMain.on !== 'function' || typeof authorize !== 'function') {
    throw new Error('BreakTwenty privileged desktop IPC authorization is unavailable.');
  }
  ipcMain.on(channel, (event, ...args) => {
    try {
      authorize(event);
      event.returnValue = handler(...args);
    } catch (_error) {
      event.returnValue = {
        status: 'error',
        message: 'BreakTwenty rejected the desktop preference request.',
      };
    }
  });
}

module.exports = {
  assertMainWindowIpcSender,
  createMainWindowIpcAuthorizer,
  registerPrivilegedIpcHandler,
  registerPrivilegedIpcListener,
};
