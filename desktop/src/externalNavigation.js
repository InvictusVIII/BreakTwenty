'use strict';

function normalizeHttpsUrl(rawUrl) {
  try {
    const parsed = new URL(String(rawUrl || '').trim());
    if (parsed.protocol !== 'https:' || !parsed.hostname || parsed.username || parsed.password) {
      return '';
    }
    return parsed.href;
  } catch (_error) {
    return '';
  }
}

function normalizeMailtoUrl(rawUrl) {
  try {
    const parsed = new URL(String(rawUrl || '').trim());
    if (
      parsed.protocol !== 'mailto:'
      || parsed.host
      || parsed.search
      || parsed.hash
    ) {
      return '';
    }
    const recipient = decodeURIComponent(parsed.pathname);
    if (recipient.length > 254) {
      return '';
    }
    const separatorIndex = recipient.indexOf('@');
    if (separatorIndex <= 0 || separatorIndex !== recipient.lastIndexOf('@')) {
      return '';
    }
    const localPart = recipient.slice(0, separatorIndex);
    const domain = recipient.slice(separatorIndex + 1);
    if (
      localPart.length > 64
      || !/^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?$/i.test(localPart)
      || !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/i.test(domain)
    ) {
      return '';
    }
    return `mailto:${recipient}`;
  } catch (_error) {
    return '';
  }
}

function normalizeMoomooOAuthAuthorizationUrl(rawUrl) {
  const normalizedUrl = normalizeHttpsUrl(rawUrl);
  if (!normalizedUrl || normalizedUrl.length > 4096) {
    return '';
  }
  const parsed = new URL(normalizedUrl);
  if (
    parsed.origin !== 'https://webapi.moomoo.com'
    || parsed.pathname !== '/oauth2/authorize/confirm'
    || parsed.hash
  ) {
    return '';
  }
  const allowedParameters = new Set([
    'client_id',
    'code_challenge',
    'code_challenge_method',
    'redirect_uri',
    'response_type',
    'state',
  ]);
  const parameterKeys = [...parsed.searchParams.keys()];
  if (
    parameterKeys.length !== allowedParameters.size
    || parameterKeys.some((key) => !allowedParameters.has(key))
    || [...allowedParameters].some((key) => parsed.searchParams.getAll(key).length !== 1)
  ) {
    return '';
  }
  const clientId = parsed.searchParams.get('client_id') || '';
  const challenge = parsed.searchParams.get('code_challenge') || '';
  const state = parsed.searchParams.get('state') || '';
  if (
    !/^[a-z0-9._-]{1,200}$/i.test(clientId)
    || !/^[a-z0-9_-]{43,128}$/i.test(challenge)
    || parsed.searchParams.get('code_challenge_method') !== 'S256'
    || parsed.searchParams.get('response_type') !== 'code'
    || !/^[a-z0-9_-]{43,128}$/i.test(state)
  ) {
    return '';
  }
  try {
    const callback = new URL(parsed.searchParams.get('redirect_uri'));
    if (
      callback.protocol !== 'http:'
      || callback.hostname !== 'localhost'
      || !callback.port
      || callback.pathname !== '/api/auth/moomoo/oauth/callback'
      || callback.search
      || callback.hash
    ) {
      return '';
    }
  } catch (_error) {
    return '';
  }
  return parsed.href;
}

function isSameOriginUrl(rawUrl, expectedUrl) {
  try {
    return new URL(String(rawUrl || '')).origin === new URL(String(expectedUrl || '')).origin;
  } catch (_error) {
    return false;
  }
}

function openExternalHttps(shell, rawUrl) {
  const normalizedUrl = normalizeHttpsUrl(rawUrl);
  if (!normalizedUrl || !shell || typeof shell.openExternal !== 'function') {
    return false;
  }
  void shell.openExternal(normalizedUrl);
  return true;
}

function openExternalHttpsOrMailto(shell, rawUrl) {
  const normalizedUrl = normalizeHttpsUrl(rawUrl) || normalizeMailtoUrl(rawUrl);
  if (!normalizedUrl || !shell || typeof shell.openExternal !== 'function') {
    return false;
  }
  void shell.openExternal(normalizedUrl);
  return true;
}

module.exports = {
  isSameOriginUrl,
  normalizeHttpsUrl,
  normalizeMailtoUrl,
  normalizeMoomooOAuthAuthorizationUrl,
  openExternalHttps,
  openExternalHttpsOrMailto,
};
