const DESKTOP_HTTP_CACHE_POLICY_VERSION = 1;
const DESKTOP_HTTP_CACHE_POLICY_PREFERENCE = 'desktopHttpCachePolicyVersion';

async function clearLegacyDesktopHttpCaches({
  electronSession,
  readPreferences,
  writePreferences,
}) {
  if (
    !electronSession
    || typeof electronSession.clearCache !== 'function'
    || typeof electronSession.clearCodeCaches !== 'function'
    || typeof readPreferences !== 'function'
    || typeof writePreferences !== 'function'
  ) {
    throw new TypeError('Desktop HTTP cache cleanup dependencies are unavailable.');
  }
  const preferences = readPreferences();
  if (
    Number(preferences?.[DESKTOP_HTTP_CACHE_POLICY_PREFERENCE] || 0)
    >= DESKTOP_HTTP_CACHE_POLICY_VERSION
  ) {
    return false;
  }

  await electronSession.clearCache();
  await electronSession.clearCodeCaches({ urls: [] });
  if (!writePreferences({
    ...(preferences || {}),
    [DESKTOP_HTTP_CACHE_POLICY_PREFERENCE]: DESKTOP_HTTP_CACHE_POLICY_VERSION,
  })) {
    throw new Error('Desktop HTTP cache cleanup could not be recorded.');
  }
  return true;
}

module.exports = {
  clearLegacyDesktopHttpCaches,
  DESKTOP_HTTP_CACHE_POLICY_PREFERENCE,
  DESKTOP_HTTP_CACHE_POLICY_VERSION,
};
