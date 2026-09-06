const assert = require('node:assert/strict');
const test = require('node:test');

const {
  normalizeLocalBackendApiUrl,
  normalizeLocalBackendHealthUrl,
  normalizeLocalFrontendUrl,
} = require('./backendApiUrl');

test('normalizes only the exact loopback backend API boundary', () => {
  assert.equal(
    normalizeLocalBackendApiUrl('http://localhost:8000/api/'),
    'http://localhost:8000/api',
  );
  assert.equal(
    normalizeLocalBackendApiUrl('http://127.0.0.1/api'),
    'http://127.0.0.1/api',
  );
  assert.equal(
    normalizeLocalBackendApiUrl('http://[::1]:8765/api///'),
    'http://[::1]:8765/api',
  );
});

test('rejects hostile or ambiguous backend API URLs before a bearer can be sent', () => {
  const rejected = [
    '',
    ' https://127.0.0.1:8000/api',
    'https://127.0.0.1:8000/api',
    'http://backend.test/api',
    'http://localhost.example/api',
    'http://127.0.0.2/api',
    'http://user:password@localhost:8000/api',
    'http://localhost:8000/api?next=http://evil.test',
    'http://localhost:8000/api#fragment',
    'http://localhost:8000/',
    'http://localhost:8000/api/v1',
    'http://localhost:8000/api%2fhealth',
    'http://localhost\\@evil.test/api',
    'file:///api',
    'http://localhost:0/api',
  ];
  for (const value of rejected) {
    assert.throws(
      () => normalizeLocalBackendApiUrl(value),
      /private local backend boundary/,
      value,
    );
  }
});

test('health authentication cannot be redirected to another origin or path', () => {
  assert.equal(
    normalizeLocalBackendHealthUrl(
      'http://127.0.0.1:8000/api/health/',
      'http://127.0.0.1:8000/api',
    ),
    'http://127.0.0.1:8000/api/health',
  );
  for (const value of [
    'http://localhost:8000/api/health',
    'http://127.0.0.1:8001/api/health',
    'http://127.0.0.1:8000/api/health?token=1',
    'http://127.0.0.1:8000/api/health/extra',
  ]) {
    assert.throws(
      () => normalizeLocalBackendHealthUrl(value, 'http://127.0.0.1:8000/api'),
      /private local backend/,
      value,
    );
  }
});

test('frontend renderer origins are explicit-port loopback HTTP roots only', () => {
  assert.equal(normalizeLocalFrontendUrl('http://localhost:3000/'), 'http://localhost:3000');
  assert.equal(normalizeLocalFrontendUrl('http://127.0.0.1:32100'), 'http://127.0.0.1:32100');
  assert.equal(normalizeLocalFrontendUrl('http://[::1]:3000'), 'http://[::1]:3000');
  assert.equal(
    normalizeLocalFrontendUrl('http://127.0.0.1:32100', { expectedPort: 32100 }),
    'http://127.0.0.1:32100',
  );
  for (const value of [
    'https://127.0.0.1:3000',
    'http://example.test:3000',
    'http://localhost.example:3000',
    'http://user:password@localhost:3000',
    'http://localhost:3000/?next=evil',
    'http://localhost:3000/#fragment',
    'http://localhost:3000/app',
    'http://localhost',
    'http://localhost:0',
    'http://localhost\\@evil.test:3000',
  ]) {
    assert.throws(() => normalizeLocalFrontendUrl(value), /private local origin/, value);
  }
  assert.throws(
    () => normalizeLocalFrontendUrl('http://localhost:3001', { expectedPort: 3000 }),
    /private local origin/,
  );
});
