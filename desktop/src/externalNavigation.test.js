'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  isSameOriginUrl,
  normalizeHttpsUrl,
  normalizeMailtoUrl,
  normalizeMoomooOAuthAuthorizationUrl,
  openExternalHttps,
  openExternalHttpsOrMailto,
} = require('./externalNavigation');

test('normalizeHttpsUrl accepts only credential-free HTTPS URLs', () => {
  assert.equal(normalizeHttpsUrl('https://example.com/news?q=1'), 'https://example.com/news?q=1');
  assert.equal(normalizeHttpsUrl('http://example.com/news'), '');
  assert.equal(normalizeHttpsUrl('file:///tmp/example'), '');
  assert.equal(normalizeHttpsUrl('breaktwenty://open'), '');
  assert.equal(normalizeHttpsUrl('https://user:pass@example.com/news'), '');
  assert.equal(normalizeHttpsUrl('not a url'), '');
});

test('normalizeMoomooOAuthAuthorizationUrl accepts only the exact PKCE authorization handoff', () => {
  const valid = `https://webapi.moomoo.com/oauth2/authorize/confirm?client_id=public-client&code_challenge=${'c'.repeat(43)}&code_challenge_method=S256&redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fapi%2Fauth%2Fmoomoo%2Foauth%2Fcallback&response_type=code&state=${'s'.repeat(43)}`;
  assert.equal(normalizeMoomooOAuthAuthorizationUrl(valid), valid);
  assert.equal(normalizeMoomooOAuthAuthorizationUrl(valid.replace('webapi.moomoo.com', 'example.com')), '');
  assert.equal(normalizeMoomooOAuthAuthorizationUrl(valid.replace('S256', 'plain')), '');
  assert.equal(normalizeMoomooOAuthAuthorizationUrl(valid.replace('localhost', 'example.com')), '');
  assert.equal(normalizeMoomooOAuthAuthorizationUrl(`${valid}&token=secret`), '');
  assert.equal(normalizeMoomooOAuthAuthorizationUrl(`${valid}&state=${'x'.repeat(43)}`), '');
});

test('normalizeMailtoUrl accepts one plain recipient without message headers', () => {
  assert.equal(normalizeMailtoUrl('mailto:support@breaktwenty.com'), 'mailto:support@breaktwenty.com');
  assert.equal(normalizeMailtoUrl('mailto:support+desktop@breaktwenty.com'), 'mailto:support+desktop@breaktwenty.com');
  assert.equal(normalizeMailtoUrl('mailto:first@example.com,second@example.com'), '');
  assert.equal(normalizeMailtoUrl('mailto:support@breaktwenty.com?subject=Bug'), '');
  assert.equal(normalizeMailtoUrl('mailto:support%0A@example.com'), '');
  assert.equal(normalizeMailtoUrl('mailto:support@localhost'), '');
  assert.equal(normalizeMailtoUrl('https://breaktwenty.com'), '');
});

test('isSameOriginUrl compares complete origins', () => {
  assert.equal(isSameOriginUrl('http://localhost:3000/accounts', 'http://localhost:3000'), true);
  assert.equal(isSameOriginUrl('http://localhost:3001/accounts', 'http://localhost:3000'), false);
  assert.equal(isSameOriginUrl('https://localhost:3000/accounts', 'http://localhost:3000'), false);
  assert.equal(isSameOriginUrl('not a url', 'http://localhost:3000'), false);
});

test('openExternalHttps rejects unsafe URLs before invoking the shell', () => {
  const opened = [];
  const shell = { openExternal: (url) => opened.push(url) };

  assert.equal(openExternalHttps(shell, 'javascript:alert(1)'), false);
  assert.equal(openExternalHttps(shell, 'mailto:support@example.com'), false);
  assert.equal(openExternalHttps(shell, 'https://example.com/help'), true);
  assert.deepEqual(opened, ['https://example.com/help']);
});

test('openExternalHttpsOrMailto forwards safe web and email links only', () => {
  const opened = [];
  const shell = { openExternal: (url) => opened.push(url) };

  assert.equal(openExternalHttpsOrMailto(shell, 'javascript:alert(1)'), false);
  assert.equal(openExternalHttpsOrMailto(shell, 'mailto:first@example.com,second@example.com'), false);
  assert.equal(openExternalHttpsOrMailto(shell, 'mailto:support@example.com'), true);
  assert.equal(openExternalHttpsOrMailto(shell, 'https://example.com/help'), true);
  assert.deepEqual(opened, ['mailto:support@example.com', 'https://example.com/help']);
});
