const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const test = require('node:test');

const {
  BackendRecoveryHandoff,
  completeBackendRecoveryHandoff,
} = require('./backendRecoveryHandoff');

const RECOVERY_ID = '11111111-1111-4111-8111-111111111111';

function renderer({ finishFallback = true } = {}) {
  const contents = new EventEmitter();
  contents.destroyed = false;
  contents.sent = [];
  contents.reloads = 0;
  contents.isDestroyed = () => contents.destroyed;
  contents.send = (channel, payload) => contents.sent.push([channel, payload]);
  contents.reloadIgnoringCache = () => {
    contents.reloads += 1;
    if (finishFallback) setImmediate(() => contents.emit('did-finish-load'));
  };
  return contents;
}

test('successful soft recovery preserves the renderer and accepts one acknowledgement', async () => {
  const handoff = new BackendRecoveryHandoff({ softTimeoutMs: 100, fallbackTimeoutMs: 100 });
  const contents = renderer();
  const resultPromise = handoff.run(contents, {
    recoveryId: RECOVERY_ID,
    backendRecoveredAt: '2026-09-04T20:00:00.000Z',
  });
  assert.equal(contents.sent.length, 1);
  assert.deepEqual(handoff.acknowledge({ recoveryId: RECOVERY_ID, status: 'ok' }), { status: 'ok' });
  assert.equal(handoff.acknowledge({ recoveryId: RECOVERY_ID, status: 'ok' }).status, 'ignored');

  const result = await resultPromise;
  assert.equal(result.completion, 'soft_acknowledged');
  assert.equal(contents.reloads, 0);
});

test('failed soft recovery registers did-finish-load before fallback reload', async () => {
  const handoff = new BackendRecoveryHandoff({ softTimeoutMs: 100, fallbackTimeoutMs: 100 });
  const contents = renderer();
  const resultPromise = handoff.run(contents, { recoveryId: RECOVERY_ID });
  handoff.acknowledge({ recoveryId: RECOVERY_ID, status: 'error', message: 'snapshot failed' });

  const result = await resultPromise;
  assert.equal(result.completion, 'fallback_reload');
  assert.equal(result.softFailure.message, 'snapshot failed');
  assert.equal(contents.reloads, 1);
});

test('missing acknowledgement uses a bounded fallback and reports a bounded load timeout', async () => {
  const handoff = new BackendRecoveryHandoff({ softTimeoutMs: 5, fallbackTimeoutMs: 5 });
  const contents = renderer({ finishFallback: false });
  const result = await handoff.run(contents, { recoveryId: RECOVERY_ID });

  assert.equal(result.completion, 'fallback_timeout');
  assert.equal(result.softFailure.timedOut, true);
  assert.equal(contents.reloads, 1);
});

test('incident finalization follows renderer completion', async () => {
  const events = [];
  const handoff = new BackendRecoveryHandoff({ softTimeoutMs: 100, fallbackTimeoutMs: 100 });
  const contents = renderer();
  contents.reloadIgnoringCache = () => {
    contents.reloads += 1;
    events.push('reload');
    setImmediate(() => {
      events.push('did-finish-load');
      contents.emit('did-finish-load');
    });
  };
  const completion = completeBackendRecoveryHandoff({
    handoff,
    webContents: contents,
    recovery: { recoveryId: RECOVERY_ID },
    finalize: (result) => {
      events.push(`finalize:${result.completion}`);
    },
  });
  handoff.acknowledge({ recoveryId: RECOVERY_ID, status: 'error' });

  assert.equal((await completion).completion, 'fallback_reload');
  assert.deepEqual(events, ['reload', 'did-finish-load', 'finalize:fallback_reload']);
});
