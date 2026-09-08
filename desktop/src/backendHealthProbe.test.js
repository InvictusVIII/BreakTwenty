const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const test = require('node:test');

const {
  expectedBackendOwnershipProof,
  probeBackendHealth,
} = require('./backendHealthProbe');

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

test('proves embedded backend ownership before transmitting bearer authentication', async () => {
  const ownershipToken = Buffer.alloc(32, 7).toString('base64');
  const calls = [];
  const httpModule = {
    request(url, options, onResponse) {
      const request = new EventEmitter();
      request.destroy = () => {};
      request.end = () => {
        calls.push({ url: new URL(url.toString()), options });
        const socket = new EventEmitter();
        socket.connecting = false;
        request.emit('socket', socket);
        const response = new EventEmitter();
        response.statusCode = 200;
        response.resume = () => {};
        onResponse(response);
        const challenge = new URL(url.toString()).searchParams.get('challenge');
        const payload = challenge
          ? {
              proof: expectedBackendOwnershipProof(
                ownershipToken,
                new URL(url.toString()).origin,
                challenge,
              ),
            }
          : { app: 'BreakTwenty', status: 'ok', startup: { status: 'ok' } };
        response.emit('data', Buffer.from(JSON.stringify(payload)));
        response.emit('end');
      };
      return request;
    },
  };

  const result = await probeBackendHealth({
    url: 'http://127.0.0.1:8765/api/health',
    authorization: 'Bearer private-launch-token',
    ownershipToken,
    expectedAppName: 'BreakTwenty',
    timeoutMs: 1000,
    httpModule,
    randomBytesImpl: () => Buffer.alloc(32, 3),
  });

  assert.equal(result.ok, true);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url.pathname, '/api/health/ownership');
  assert.deepEqual(calls[0].options.headers, {});
  assert.equal(calls[1].url.pathname, '/api/health');
  assert.equal(calls[1].options.headers.Authorization, 'Bearer private-launch-token');
});

test('never transmits bearer authentication when ownership proof comes from another endpoint', async () => {
  const calls = [];
  const ownershipToken = Buffer.alloc(32, 9).toString('base64');
  const httpModule = {
    request(url, options, onResponse) {
      const request = new EventEmitter();
      request.destroy = () => {};
      request.end = () => {
        calls.push({ url: new URL(url.toString()), options });
        const socket = new EventEmitter();
        socket.connecting = false;
        request.emit('socket', socket);
        const response = new EventEmitter();
        response.statusCode = 200;
        response.resume = () => {};
        onResponse(response);
        const challenge = new URL(url.toString()).searchParams.get('challenge');
        const proof = expectedBackendOwnershipProof(
          ownershipToken,
          'http://127.0.0.1:9999',
          challenge,
        );
        response.emit('data', Buffer.from(JSON.stringify({ proof })));
        response.emit('end');
      };
      return request;
    },
  };

  const result = await probeBackendHealth({
    url: 'http://127.0.0.1:8765/api/health',
    authorization: 'Bearer must-not-leak',
    ownershipToken,
    expectedAppName: 'BreakTwenty',
    timeoutMs: 1000,
    httpModule,
    randomBytesImpl: () => Buffer.alloc(32, 4),
  });

  assert.equal(result.ok, false);
  assert.equal(result.phase, 'ownership_invalid_response');
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].options.headers, {});
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
