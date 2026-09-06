const LOOPBACK_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]']);

function normalizeLocalHttpUrl(value, expectedPath, label) {
  const raw = String(value || '');
  if (!raw || raw !== raw.trim() || raw.includes('\\')) {
    throw new Error(`${label} must use the private local backend boundary.`);
  }
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (_error) {
    throw new Error(`${label} must use the private local backend boundary.`);
  }
  const normalizedPath = parsed.pathname.replace(/\/+$/, '') || '/';
  if (
    parsed.protocol !== 'http:'
    || !LOOPBACK_HOSTNAMES.has(parsed.hostname.toLowerCase())
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || parsed.port === '0'
    || normalizedPath !== expectedPath
  ) {
    throw new Error(`${label} must use the private local backend boundary.`);
  }
  return `${parsed.origin}${expectedPath}`;
}

function normalizeLocalBackendApiUrl(value) {
  return normalizeLocalHttpUrl(value, '/api', 'BreakTwenty backend API URL');
}

function normalizeLocalBackendHealthUrl(value, backendApiUrl) {
  const normalizedApiUrl = normalizeLocalBackendApiUrl(backendApiUrl);
  const normalizedHealthUrl = normalizeLocalHttpUrl(
    value,
    '/api/health',
    'BreakTwenty backend health URL',
  );
  if (new URL(normalizedHealthUrl).origin !== new URL(normalizedApiUrl).origin) {
    throw new Error('BreakTwenty backend health URL must match the private local backend API origin.');
  }
  return normalizedHealthUrl;
}

function normalizeLocalFrontendUrl(value, { expectedPort = null } = {}) {
  const raw = String(value || '');
  if (!raw || raw !== raw.trim() || raw.includes('\\')) {
    throw new Error('BreakTwenty frontend URL must use a private local origin.');
  }
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (_error) {
    throw new Error('BreakTwenty frontend URL must use a private local origin.');
  }
  const normalizedPath = parsed.pathname.replace(/\/+$/, '') || '/';
  const port = Number(parsed.port || 0);
  if (
    parsed.protocol !== 'http:'
    || !LOOPBACK_HOSTNAMES.has(parsed.hostname.toLowerCase())
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || normalizedPath !== '/'
    || !Number.isInteger(port)
    || port < 1
    || port > 65535
    || (expectedPort !== null && port !== Number(expectedPort))
  ) {
    throw new Error('BreakTwenty frontend URL must use a private local origin.');
  }
  return parsed.origin;
}

if (require.main === module) {
  const operation = String(process.argv[2] || '').trim().toLowerCase();
  try {
    if (operation === 'api' || operation === 'validate') {
      process.stdout.write(`${normalizeLocalBackendApiUrl(process.argv[3])}\n`);
    } else if (operation === 'health') {
      process.stdout.write(`${normalizeLocalBackendHealthUrl(process.argv[3], process.argv[4])}\n`);
    } else if (operation === 'frontend') {
      process.stdout.write(`${normalizeLocalFrontendUrl(process.argv[3])}\n`);
    } else {
      throw new Error('Usage: backendApiUrl.js <api|validate|frontend> <url>');
    }
  } catch (error) {
    console.error(`[BreakTwenty] ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = {
  normalizeLocalBackendApiUrl,
  normalizeLocalBackendHealthUrl,
  normalizeLocalFrontendUrl,
};
