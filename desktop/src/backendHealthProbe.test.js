const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const test = require('node:test');

const { probeBackendHealth } = require('./backendHealthProbe');

function fakeHttp({ statusCode = 200, chunks = [], connect = true, requestError = null } = {}) {
  return {
    request(_url, _options, onResponse) {
      const request = new EventEmitter();
      request.destroy = () => {};
      request.end = () => {
        const socket = new EventEmitter();
        socket.connecting = connect;
        request.emit('socket', socket);
        if (connect === true) socket.emit('connect');
        if (requestError) {
          request.emit('error', requestError);
          return;
        }
        const response = new EventEmitter();
        response.statusCode = statusCode;
        response.resume = () => {};
        onResponse(response);
        chunks.forEach((chunk) => response.emit('data', Buffer.from(chunk)));
        response.emit('end');
      };
      return request;
    },
  };
}

test('accepts a valid health response without changing degraded status semantics', async () => {
  const result = await probeBackendHealth({
    url: 'http://127.0.0.1:8765/api/health',
    authorization: 'Bearer fixture',
    expectedAppName: 'BreakTwenty',
    timeoutMs: 1000,
    httpModule: fakeHttp({
      chunks: [JSON.stringify({
        app: 'BreakTwenty',
        status: 'degraded',
        startup: { status: 'degraded' },
      })],
    }),
  });

  assert.equal(result.ok, true);
  assert.equal(result.phase, 'complete');
  assert.equal(result.data.status, 'degraded');
});

test('classifies TCP, headers, body, and invalid-response failures', async () => {
  const tcp = await probeBackendHealth({
    url: 'http://127.0.0.1:8765/api/health',
    authorization: 'Bearer fixture',
    expectedAppName: 'BreakTwenty',
    timeoutMs: 1000,
    httpModule: fakeHttp({ connect: 'pending', requestError: new Error('connect refused') }),
  });
  assert.equal(tcp.phase, 'tcp_connection');

  const headers = await probeBackendHealth({
    url: 'http://127.0.0.1:8765/api/health',
    authorization: 'Bearer fixture',
    expectedAppName: 'BreakTwenty',
    timeoutMs: 1000,
    httpModule: fakeHttp({ statusCode: 503 }),
  });
  assert.equal(headers.phase, 'http_headers');

  const invalid = await probeBackendHealth({
    url: 'http://127.0.0.1:8765/api/health',
    authorization: 'Bearer fixture',
    expectedAppName: 'BreakTwenty',
    timeoutMs: 1000,
    httpModule: fakeHttp({ chunks: ['not-json'] }),
  });
  assert.equal(invalid.phase, 'invalid_response');

  const bodyHttp = fakeHttp({ chunks: [] });
  bodyHttp.request = (_url, _options, onResponse) => {
    const request = new EventEmitter();
    request.destroy = () => {};
    request.end = () => {
      const socket = new EventEmitter();
      socket.connecting = false;
      request.emit('socket', socket);
      const response = new EventEmitter();
      response.statusCode = 200;
      response.resume = () => {};
      onResponse(response);
      response.emit('aborted');
    };
    return request;
  };
  const body = await probeBackendHealth({
    url: 'http://127.0.0.1:8765/api/health',
    authorization: 'Bearer fixture',
    expectedAppName: 'BreakTwenty',
    timeoutMs: 1000,
    httpModule: bodyHttp,
  });
  assert.equal(body.phase, 'http_body');
});
