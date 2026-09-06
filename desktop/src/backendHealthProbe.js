const http = require('node:http');

const HEALTH_RESPONSE_MAX_BYTES = 64 * 1024;

function elapsedMs(startedAt, now) {
  return Math.max(0, now() - startedAt);
}

function validHealthPayload(payload, expectedAppName) {
  return Boolean(
    payload
    && typeof payload === 'object'
    && !Array.isArray(payload)
    && payload.app === expectedAppName
    && typeof payload.status === 'string'
    && payload.status.trim()
    && payload.startup
    && typeof payload.startup === 'object'
    && !Array.isArray(payload.startup),
  );
}

function probeBackendHealth({
  url,
  authorization,
  expectedAppName,
  timeoutMs,
  httpModule = http,
  now = Date.now,
} = {}) {
  const startedAt = now();
  const timings = { startedAt };
  let phase = 'tcp_connection';

  return new Promise((resolve) => {
    let settled = false;
    let request = null;
    let timer = null;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve({
        ...result,
        phase: result.phase || phase,
        elapsedMs: elapsedMs(startedAt, now),
        timings,
      });
    };

    try {
      request = httpModule.request(url, {
        method: 'GET',
        headers: { Authorization: authorization },
      }, (response) => {
        timings.headersAt = now();
        const status = Number(response.statusCode || 0) || null;
        if (!status || status < 200 || status >= 300) {
          response.resume?.();
          finish({
            ok: false,
            status,
            phase: 'http_headers',
            message: `Backend health returned HTTP ${status || 'unknown'}.`,
          });
          return;
        }

        phase = 'http_body';
        const chunks = [];
        let bytes = 0;
        response.on('data', (chunk) => {
          if (settled) return;
          const buffer = Buffer.from(chunk);
          bytes += buffer.length;
          if (bytes > HEALTH_RESPONSE_MAX_BYTES) {
            request.destroy();
            finish({
              ok: false,
              status,
              phase: 'invalid_response',
              message: 'Backend health response exceeded its safety limit.',
            });
            return;
          }
          chunks.push(buffer);
        });
        response.once('aborted', () => finish({
          ok: false,
          status,
          phase: 'http_body',
          message: 'Backend health response ended before its body completed.',
        }));
        response.once('error', (error) => finish({
          ok: false,
          status,
          phase: 'http_body',
          message: error.message || 'Backend health response body failed.',
        }));
        response.once('end', () => {
          timings.bodyAt = now();
          let payload;
          try {
            payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          } catch (_error) {
            finish({
              ok: false,
              status,
              phase: 'invalid_response',
              message: 'Backend health returned invalid JSON.',
            });
            return;
          }
          if (!validHealthPayload(payload, expectedAppName)) {
            finish({
              ok: false,
              status,
              phase: 'invalid_response',
              message: 'Backend health returned an unexpected payload.',
            });
            return;
          }
          finish({ ok: true, status, data: payload, phase: 'complete' });
        });
      });
      request.once('socket', (socket) => {
        if (!socket.connecting) {
          phase = 'http_headers';
          timings.connectedAt = now();
          return;
        }
        socket.once('connect', () => {
          phase = 'http_headers';
          timings.connectedAt = now();
        });
      });
      request.once('error', (error) => finish({
        ok: false,
        message: error.message || 'Backend health request failed.',
      }));
      timer = setTimeout(() => {
        const timedOutPhase = phase;
        request.destroy();
        finish({
          ok: false,
          phase: timedOutPhase,
          message: `Backend health timed out during ${timedOutPhase}.`,
        });
      }, Math.max(250, Number(timeoutMs) || 0));
      request.end();
    } catch (error) {
      request?.destroy?.();
      finish({
        ok: false,
        message: error.message || 'Backend health request failed.',
      });
    }
  });
}

module.exports = {
  HEALTH_RESPONSE_MAX_BYTES,
  probeBackendHealth,
  validHealthPayload,
};
