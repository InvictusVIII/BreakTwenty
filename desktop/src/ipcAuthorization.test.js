const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const {
  createMainWindowIpcAuthorizer,
  registerPrivilegedIpcHandler,
  registerPrivilegedIpcListener,
} = require('./ipcAuthorization');

function fixture({ senderUrl = 'http://127.0.0.1:3000/accounts', sameSender = true, mainFrame = true } = {}) {
  const trustedContents = { mainFrame: {} };
  const event = {
    sender: sameSender ? trustedContents : { mainFrame: {} },
    senderFrame: mainFrame ? trustedContents.mainFrame : {},
  };
  event.senderFrame.url = senderUrl;
  const window = { isDestroyed: () => false, webContents: trustedContents };
  return { event, window };
}

test('privileged IPC accepts only the approved main-window main frame', async () => {
  const { event, window } = fixture();
  const handlers = new Map();
  const ipcMain = { handle: (channel, handler) => handlers.set(channel, handler) };
  const authorize = createMainWindowIpcAuthorizer({
    getMainWindow: () => window,
    getFrontendUrl: () => 'http://127.0.0.1:3000',
  });
  registerPrivilegedIpcHandler(ipcMain, authorize, 'breaktwenty:probe', (payload) => payload);

  assert.equal(await handlers.get('breaktwenty:probe')(event, 'ok'), 'ok');
});

test('privileged IPC rejects non-main senders, subframes, and remote or credentialed URLs', () => {
  const cases = [
    fixture({ sameSender: false }),
    fixture({ mainFrame: false }),
    fixture({ senderUrl: 'https://127.0.0.1:3000/accounts' }),
    fixture({ senderUrl: 'http://evil.test:3000/accounts' }),
    fixture({ senderUrl: 'http://user:password@127.0.0.1:3000/accounts' }),
  ];
  for (const { event, window } of cases) {
    const authorize = createMainWindowIpcAuthorizer({
      getMainWindow: () => window,
      getFrontendUrl: () => 'http://127.0.0.1:3000',
    });
    assert.throws(() => authorize(event), /untrusted desktop request/);
  }
});

test('privileged synchronous IPC returns data only to the approved main frame', () => {
  const trusted = fixture();
  const rejected = fixture({ senderUrl: 'http://evil.test:3000/accounts' });
  const listeners = new Map();
  const ipcMain = { on: (channel, listener) => listeners.set(channel, listener) };
  const authorize = createMainWindowIpcAuthorizer({
    getMainWindow: () => trusted.window,
    getFrontendUrl: () => 'http://127.0.0.1:3000',
  });
  registerPrivilegedIpcListener(ipcMain, authorize, 'breaktwenty:probe-sync', (payload) => ({
    status: 'ok',
    value: payload,
  }));

  listeners.get('breaktwenty:probe-sync')(trusted.event, 'saved');
  assert.deepEqual(trusted.event.returnValue, { status: 'ok', value: 'saved' });

  rejected.event.sender = trusted.window.webContents;
  rejected.event.senderFrame = trusted.window.webContents.mainFrame;
  rejected.event.senderFrame.url = 'http://evil.test:3000/accounts';
  listeners.get('breaktwenty:probe-sync')(rejected.event, 'blocked');
  assert.equal(rejected.event.returnValue.status, 'error');
});

test('all desktop IPC registrations use the privileged registration boundary', () => {
  const root = path.resolve(__dirname);
  const mainSource = fs.readFileSync(path.join(root, 'main.js'), 'utf8');
  const updaterSource = fs.readFileSync(path.join(root, 'appUpdater.js'), 'utf8');
  assert.equal(mainSource.includes('ipcMain.handle('), false);
  assert.equal(updaterSource.includes('this.ipcMain.handle('), false);
  assert.match(mainSource, /registerPrivilegedIpcHandler/);
  assert.match(updaterSource, /registerPrivilegedIpcHandler/);
});
