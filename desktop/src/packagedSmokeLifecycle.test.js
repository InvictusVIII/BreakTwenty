const assert = require('node:assert/strict');
const test = require('node:test');

const { requestGracefulSmokeExit } = require('./packagedSmokeLifecycle');

test('packaged smoke requests a graceful quit before its forced-exit fallback', () => {
  const calls = [];
  const fallback = { unref: () => calls.push(['unref']) };
  const returned = requestGracefulSmokeExit(
    {
      quit: () => calls.push(['quit']),
      exit: (code) => calls.push(['exit', code]),
    },
    7,
    {
      forceAfterMs: 321,
      schedule: (callback, delay) => {
        calls.push(['schedule', delay]);
        callback();
        return fallback;
      },
      setExitCode: (code) => calls.push(['exit-code', code]),
    },
  );

  assert.equal(returned, fallback);
  assert.deepEqual(calls, [
    ['exit-code', 7],
    ['quit'],
    ['schedule', 321],
    ['exit', 7],
    ['unref'],
  ]);
});
