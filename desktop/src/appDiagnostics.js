const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');

const APP_INCIDENT_SCHEMA_VERSION = 1;
const APP_INCIDENT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
const APP_INCIDENT_MAX_COUNT = 8;
const APP_INCIDENT_TOTAL_MAX_BYTES = 8 * 1024 * 1024;
const APP_INCIDENT_LOG_TAIL_MAX_BYTES = 384 * 1024;
const APP_INCIDENT_ID_RE = /^\d{8}T\d{6}Z_[0-9a-f]{8}-[0-9a-f-]{27}$/i;
const SENSITIVE_LINE_RE = /\b(?:authorization|cookies?|set-cookie|password|passcode|secret|credentials?|storage[_ -]?state|encryption[_ -]?key|access[_ -]?token|refresh[_ -]?token|id[_ -]?token|auth[_ -]?token|github[_ -]?(?:authorization|credentials?|token)|release[_ -]?(?:credentials?|token)|private[_ -]?update[_ -]?token|request[_ -]?headers?|key[_ -]?bootstrap|csc[_ -]?(?:link|key[_ -]?password)|apple[_ -]?(?:api[_ -]?key|id[_ -]?password)|azure[_ -]?client[_ -]?secret|aws[_ -]?secret[_ -]?access[_ -]?key)\b/i;
const SENSITIVE_KEY_RE = /^(?:authorization|proxy-authorization|cookies?|set-cookie|password|passcode|secret|credentials?|storage[_ -]?state|encryption[_ -]?key|(?:access|refresh|id|auth|github|release)[_ -]?token|gh[_ -]?token|private[_ -]?update[_ -]?token|request[_ -]?headers?|key[_ -]?bootstrap|csc[_ -]?(?:link|key[_ -]?password)|apple[_ -]?(?:api[_ -]?key|id[_ -]?password)|azure[_ -]?client[_ -]?secret|aws[_ -]?secret[_ -]?access[_ -]?key)$/i;
const DATA_BEARING_LINE_RE = /\b(?:account|accounts|balance|balances|transaction|transactions|holding|holdings|position|positions|net[_ -]?worth|cash[_ -]?flow|institution[_ -]?id|external[_ -]?id|amount|currency)\b/i;
const SENSITIVE_HEADER_DECLARATION_RE = /(?:^|[\s{,])["']?(?:cookie|set-cookie)["']?\s*[:=]/i;
const SERIALIZED_COOKIE_LINE_RE = /^\s*["']?(?:__Host-|__Secure-)?[!#$%&'*+.^_`|~0-9A-Za-z-]+=[^\r\n]+["']?,?\s*$/i;
const EMBEDDED_COOKIE_RE = /(?:^|[;\s"'[(,])(?:_gh_sess|_octo|logged_in|dotcom_user|user_session|__Host-[^=;\s]+|__Secure-[^=;\s]+)=/i;
const COOKIE_ATTRIBUTE_RE = /;\s*(?:domain|path|expires|max-age|secure|httponly|samesite|partitioned)(?:\s*=|\s*;|\s*$)/i;
const AUTH_SCHEME_RE = /\b(?:Bearer|Basic|token)\s+[A-Za-z0-9._~+\/-]{8,}=*/gi;
const JWT_RE = /\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?:\.[A-Za-z0-9_-]{8,})?/g;
const GITHUB_TOKEN_RE = /\b(?:github[_]pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b/gi;
const LONG_SECRET_RE = /\b[A-Za-z0-9+/_-]{48,}={0,2}\b/g;
const SENSITIVE_JSON_VALUE_RE = /(["']?(?:password|passcode|secret|credentials?|cookies?|set-cookie|authorization|proxy-authorization|access_token|refresh_token|id_token|auth_token|github_token|gh_token|release_token|encryption_key)["']?\s*[:=]\s*)(["']?)[^\s,;}]+\2/gi;
const SENSITIVE_QUERY_VALUE_RE = /([?&](?:access_token|api_key|auth|authorization|client_secret|credential|key|sig|signature|token|x-amz-credential|x-amz-security-token|x-amz-signature|x-goog-credential|x-goog-signature)=)[^&#\s]+/gi;
const URL_CREDENTIAL_RE = /([a-z][a-z0-9+.-]*:\/\/)[^/@\s]+@/gi;
const REDACTED_SENSITIVE_LINE = '[REDACTED SENSITIVE LOG LINE]';

function ensurePrivateDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true, mode: 0o700 });
  try {
    fs.chmodSync(dirPath, 0o700);
  } catch (_error) {
  }
}

function writePrivateFile(filePath, content) {
  const tempPath = `${filePath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tempPath, content, { encoding: 'utf8', mode: 0o600 });
  try {
    fs.chmodSync(tempPath, 0o600);
  } catch (_error) {
  }
  fs.renameSync(tempPath, filePath);
}

function safeStat(filePath) {
  try {
    return fs.lstatSync(filePath);
  } catch (_error) {
    return null;
  }
}

function readBoundedJson(filePath, maxBytes = 128 * 1024) {
  const stats = safeStat(filePath);
  if (!stats?.isFile() || stats.isSymbolicLink() || stats.size <= 0 || stats.size > maxBytes) {
    return null;
  }
  try {
    const payload = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    return payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : null;
  } catch (_error) {
    return null;
  }
}

function normalizeRuntimeFingerprint(payload) {
  if (!payload || payload.schemaVersion !== 1) return null;
  const safeText = (value, limit = 200) => (
    typeof value === 'string' && value.length <= limit && /^[A-Za-z0-9 ._+:/()-]*$/.test(value)
      ? value
      : null
  );
  const safeDigest = (value) => (
    typeof value === 'string' && /^[0-9a-f]{64}$/.test(value) ? value : null
  );
  const safeObject = (value, keys) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(keys.flatMap((key) => {
      const normalized = safeText(value[key]);
      return normalized === null ? [] : [[key, normalized]];
    }));
  };
  const dependencies = payload.dependencies && typeof payload.dependencies === 'object'
    ? Object.fromEntries(Object.entries(payload.dependencies).flatMap(([name, version]) => {
      const safeName = safeText(name, 80);
      const safeVersion = safeText(version, 80);
      return safeName === null || safeVersion === null ? [] : [[safeName, safeVersion]];
    }).slice(0, 64))
    : {};
  const components = payload.components && typeof payload.components === 'object'
    ? Object.fromEntries(Object.entries(payload.components).flatMap(([name, digest]) => {
      const safeName = safeText(name, 80);
      const safeHash = safeDigest(digest);
      return safeName === null || safeHash === null ? [] : [[safeName, safeHash]];
    }).slice(0, 32))
    : {};
  return {
    schemaVersion: 1,
    fingerprintSha256: safeDigest(payload.fingerprintSha256),
    python: safeObject(payload.python, ['implementation', 'version', 'compiler', 'byteOrder']),
    platform: safeObject(payload.platform, ['system', 'machine', 'architecture']),
    opensslVersion: safeText(payload.opensslVersion),
    sslModule: safeObject(payload.sslModule, ['kind']),
    portableRuntime: safeObject(payload.portableRuntime, [
      'runtimeKind',
      'release',
      'target',
      'asset',
      'pythonVersion',
      'pipOnlyBinary',
      'pipNoBinary',
    ]),
    dependencies,
    components,
  };
}

function runtimeFingerprintForDiagnostics(filePath) {
  return normalizeRuntimeFingerprint(readBoundedJson(filePath));
}

function readTail(filePath, maxBytes = APP_INCIDENT_LOG_TAIL_MAX_BYTES) {
  const stats = safeStat(filePath);
  if (!stats?.isFile() || stats.isSymbolicLink() || stats.size <= 0) return '';
  const length = Math.min(stats.size, Math.max(0, Number(maxBytes) || 0));
  const buffer = Buffer.alloc(length);
  const descriptor = fs.openSync(filePath, 'r');
  try {
    fs.readSync(descriptor, buffer, 0, length, Math.max(0, stats.size - length));
  } finally {
    fs.closeSync(descriptor);
  }
  return buffer.toString('utf8');
}

function replaceKnownPaths(value, redactionRoots) {
  let result = String(value || '');
  const normalizedRoots = [...new Set(
    (redactionRoots || [])
      .map((root) => String(root || '').trim())
      .filter(Boolean),
  )].sort((left, right) => right.length - left.length);
  for (const normalized of normalizedRoots) {
    const forwardSlash = normalized.replaceAll('\\', '/');
    const backslash = normalized.replaceAll('/', '\\');
    const variants = [...new Set([
      normalized,
      forwardSlash,
      backslash,
      backslash.replaceAll('\\', '\\\\'),
      encodeURI(forwardSlash),
    ])].sort((left, right) => right.length - left.length);
    for (const variant of variants) {
      const escaped = variant.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      result = result.replace(new RegExp(escaped, 'gi'), '[LOCAL_PATH]');
    }
  }
  return result;
}

function replaceExactSecrets(value, exactSecrets) {
  let result = String(value || '');
  const secrets = [...new Set(
    (exactSecrets || [])
      .map((secret) => String(secret || '').trim())
      .filter((secret) => secret.length >= 8),
  )].sort((left, right) => right.length - left.length);
  for (const secret of secrets) {
    for (const variant of [...new Set([secret, encodeURIComponent(secret)])]) {
      result = result.replaceAll(variant, '[REDACTED_TOKEN]');
    }
  }
  return result;
}

function delimiterBalance(value) {
  let balance = 0;
  let quote = '';
  let escaped = false;
  for (const character of String(value || '')) {
    if (quote) {
      if (escaped) {
        escaped = false;
      } else if (character === '\\') {
        escaped = true;
      } else if (character === quote) {
        quote = '';
      }
      continue;
    }
    if (character === '"' || character === "'" || character === '`') {
      quote = character;
      continue;
    }
    if (character === '[' || character === '{') balance += 1;
    if (character === ']' || character === '}') balance -= 1;
  }
  return balance;
}

function likelySerializedCookieLine(line) {
  return (
    SERIALIZED_COOKIE_LINE_RE.test(line)
    || EMBEDDED_COOKIE_RE.test(line)
    || (COOKIE_ATTRIBUTE_RE.test(line) && /[!#$%&'*+.^_`|~0-9A-Za-z-]+=/.test(line))
  );
}

function sanitizeLogText(value, redactionRoots = [], exactSecrets = []) {
  let sensitiveValueDepth = 0;
  let pendingSensitiveValue = false;
  const sanitizedLines = replaceKnownPaths(replaceExactSecrets(value, exactSecrets), redactionRoots)
    .split(/\r?\n/)
    .map((line) => {
      if (sensitiveValueDepth > 0) {
        sensitiveValueDepth = Math.max(0, sensitiveValueDepth + delimiterBalance(line));
        return REDACTED_SENSITIVE_LINE;
      }
      if (pendingSensitiveValue) {
        pendingSensitiveValue = false;
        sensitiveValueDepth = Math.max(0, delimiterBalance(line));
        return REDACTED_SENSITIVE_LINE;
      }
      const sensitiveHeaderMatch = line.match(SENSITIVE_HEADER_DECLARATION_RE);
      if (sensitiveHeaderMatch) {
        const declarationTail = line.slice((sensitiveHeaderMatch.index || 0) + sensitiveHeaderMatch[0].length);
        sensitiveValueDepth = Math.max(0, delimiterBalance(declarationTail));
        pendingSensitiveValue = !declarationTail.trim();
        return REDACTED_SENSITIVE_LINE;
      }
      if (likelySerializedCookieLine(line)) {
        return REDACTED_SENSITIVE_LINE;
      }
      if (SENSITIVE_LINE_RE.test(line)) {
        SENSITIVE_LINE_RE.lastIndex = 0;
        return REDACTED_SENSITIVE_LINE;
      }
      SENSITIVE_LINE_RE.lastIndex = 0;
      if (DATA_BEARING_LINE_RE.test(line)) {
        return '[REDACTED DATA-BEARING LOG LINE]';
      }
      return line
        .replace(AUTH_SCHEME_RE, '[REDACTED_AUTHORIZATION]')
        .replace(JWT_RE, '[REDACTED_TOKEN]')
        .replace(GITHUB_TOKEN_RE, '[REDACTED_TOKEN]')
        .replace(LONG_SECRET_RE, '[REDACTED_LONG_VALUE]')
        .replace(SENSITIVE_JSON_VALUE_RE, '$1[REDACTED]')
        .replace(SENSITIVE_QUERY_VALUE_RE, '$1[REDACTED]')
        .replace(URL_CREDENTIAL_RE, '$1[REDACTED]@');
    });
  return sanitizedLines.join('\n').trim();
}

function sanitizeValue(value, redactionRoots = [], depth = 0, exactSecrets = []) {
  if (depth > 8) return '[OMITTED]';
  if (typeof value === 'string') {
    return sanitizeLogText(value, redactionRoots, exactSecrets).slice(0, 4000);
  }
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'boolean' || value === null) return value;
  if (Array.isArray(value)) {
    return value.slice(0, 100).map(
      (item) => sanitizeValue(item, redactionRoots, depth + 1, exactSecrets),
    );
  }
  if (value && typeof value === 'object') {
    const result = {};
    for (const [key, item] of Object.entries(value).slice(0, 100)) {
      if (SENSITIVE_KEY_RE.test(key)) {
        result[key] = '[REDACTED]';
      } else {
        result[key] = sanitizeValue(item, redactionRoots, depth + 1, exactSecrets);
      }
    }
    return result;
  }
  return value === undefined ? null : String(value).slice(0, 1000);
}

function createSanitizingLogger(logger, { redactionRoots = [], exactSecrets = () => [] } = {}) {
  const sanitizedLogger = {};
  for (const level of ['debug', 'info', 'warn', 'error']) {
    if (typeof logger?.[level] !== 'function') continue;
    sanitizedLogger[level] = (...args) => {
      const secrets = typeof exactSecrets === 'function' ? exactSecrets() : exactSecrets;
      const sanitizedArgs = args.map((value) => {
        if (value instanceof Error) {
          return sanitizeLogText(value.stack || value.message, redactionRoots, secrets);
        }
        return sanitizeValue(value, redactionRoots, 0, secrets);
      });
      return logger[level](...sanitizedArgs);
    };
  }
  return sanitizedLogger;
}

function sanitizeExportEntry(name, data, redactionRoots) {
  const text = data.toString('utf8');
  if (String(name || '').toLowerCase().endsWith('.json')) {
    try {
      const source = JSON.parse(text);
      const sanitized = sanitizeValue(source, redactionRoots);
      const runtimeFingerprint = normalizeRuntimeFingerprint(source?.app?.runtimeFingerprint);
      if (runtimeFingerprint && sanitized?.app) {
        sanitized.app.runtimeFingerprint = runtimeFingerprint;
      }
      return Buffer.from(`${JSON.stringify(sanitized, null, 2)}\n`, 'utf8');
    } catch (_error) {
    }
  }
  const sanitized = sanitizeLogText(text, redactionRoots);
  return Buffer.from(sanitized ? `${sanitized}\n` : '', 'utf8');
}

function incidentIdFor(date = new Date()) {
  const timestamp = date.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
  return `${timestamp}_${crypto.randomUUID()}`;
}

const CRC32_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = (value & 1) ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
    }
    table[index] = value >>> 0;
  }
  return table;
})();

function crc32(buffer) {
  let value = 0xffffffff;
  for (const byte of buffer) value = CRC32_TABLE[(value ^ byte) & 0xff] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}

function zipDateTime(date = new Date()) {
  const year = Math.max(1980, date.getFullYear());
  return {
    date: (((year - 1980) & 0x7f) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
    time: (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2),
  };
}

function buildZipArchive(entries) {
  const localParts = [];
  const centralParts = [];
  let offset = 0;
  for (const entry of entries) {
    const filename = Buffer.from(entry.name, 'utf8');
    const source = entry.data;
    const compressed = zlib.deflateRawSync(source, { level: 7 });
    const checksum = crc32(source);
    const timestamp = zipDateTime(entry.modifiedAt);
    const localHeader = Buffer.alloc(30);
    localHeader.writeUInt32LE(0x04034b50, 0);
    localHeader.writeUInt16LE(20, 4);
    localHeader.writeUInt16LE(0x0800, 6);
    localHeader.writeUInt16LE(8, 8);
    localHeader.writeUInt16LE(timestamp.time, 10);
    localHeader.writeUInt16LE(timestamp.date, 12);
    localHeader.writeUInt32LE(checksum, 14);
    localHeader.writeUInt32LE(compressed.length, 18);
    localHeader.writeUInt32LE(source.length, 22);
    localHeader.writeUInt16LE(filename.length, 26);
    localHeader.writeUInt16LE(0, 28);
    localParts.push(localHeader, filename, compressed);

    const centralHeader = Buffer.alloc(46);
    centralHeader.writeUInt32LE(0x02014b50, 0);
    centralHeader.writeUInt16LE(20, 4);
    centralHeader.writeUInt16LE(20, 6);
    centralHeader.writeUInt16LE(0x0800, 8);
    centralHeader.writeUInt16LE(8, 10);
    centralHeader.writeUInt16LE(timestamp.time, 12);
    centralHeader.writeUInt16LE(timestamp.date, 14);
    centralHeader.writeUInt32LE(checksum, 16);
    centralHeader.writeUInt32LE(compressed.length, 20);
    centralHeader.writeUInt32LE(source.length, 24);
    centralHeader.writeUInt16LE(filename.length, 28);
    centralHeader.writeUInt16LE(0, 30);
    centralHeader.writeUInt16LE(0, 32);
    centralHeader.writeUInt16LE(0, 34);
    centralHeader.writeUInt16LE(0, 36);
    centralHeader.writeUInt32LE(0, 38);
    centralHeader.writeUInt32LE(offset, 42);
    centralParts.push(centralHeader, filename);
    offset += localHeader.length + filename.length + compressed.length;
  }
  const centralDirectory = Buffer.concat(centralParts);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralDirectory.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...localParts, centralDirectory, end]);
}

function directorySize(dirPath) {
  const rootStats = safeStat(dirPath);
  if (!rootStats?.isDirectory() || rootStats.isSymbolicLink()) return 0;
  let total = 0;
  for (const name of fs.readdirSync(dirPath).slice(0, 64)) {
    const childPath = path.join(dirPath, name);
    const stats = safeStat(childPath);
    if (!stats || stats.isSymbolicLink()) continue;
    if (stats.isFile()) total += stats.size;
  }
  return total;
}

function removeIncidentDirectory(rootDir, incidentDir) {
  const resolvedRoot = path.resolve(rootDir);
  const resolvedIncident = path.resolve(incidentDir);
  if (path.dirname(resolvedIncident) !== resolvedRoot || !APP_INCIDENT_ID_RE.test(path.basename(resolvedIncident))) {
    return false;
  }
  const stats = safeStat(resolvedIncident);
  if (!stats?.isDirectory() || stats.isSymbolicLink()) return false;
  fs.rmSync(resolvedIncident, { recursive: true, force: true });
  return true;
}

class AppDiagnostics {
  constructor({
    logDir,
    appVersion = '',
    channel = '',
    electronLogPath = '',
    backendLogPath = '',
    runtimeFingerprintPath = '',
    redactionRoots = [],
    now = () => new Date(),
  } = {}) {
    this.rootDir = path.resolve(String(logDir || ''), 'app-incidents');
    this.appVersion = String(appVersion || '');
    this.channel = String(channel || '');
    this.electronLogPath = String(electronLogPath || '');
    this.backendLogPath = String(backendLogPath || '');
    this.runtimeFingerprintPath = String(runtimeFingerprintPath || '');
    this.redactionRoots = [...new Set((redactionRoots || []).map((item) => String(item || '')).filter(Boolean))];
    this.now = now;
    ensurePrivateDir(this.rootDir);
    this.prune();
  }

  policy() {
    return {
      retentionDays: APP_INCIDENT_RETENTION_MS / (24 * 60 * 60 * 1000),
      retentionHours: APP_INCIDENT_RETENTION_MS / (60 * 60 * 1000),
      maxIncidents: APP_INCIDENT_MAX_COUNT,
      maxTotalBytes: APP_INCIDENT_TOTAL_MAX_BYTES,
      maxLogTailBytes: APP_INCIDENT_LOG_TAIL_MAX_BYTES,
    };
  }

  incidentPath(incidentId) {
    const normalized = String(incidentId || '');
    if (!APP_INCIDENT_ID_RE.test(normalized)) return '';
    const candidate = path.resolve(this.rootDir, normalized);
    return path.dirname(candidate) === this.rootDir ? candidate : '';
  }

  readManifest(incidentDir) {
    const manifestPath = path.join(incidentDir, 'manifest.json');
    const stats = safeStat(manifestPath);
    if (!stats?.isFile() || stats.isSymbolicLink() || stats.size > 256 * 1024) return null;
    try {
      const payload = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      return payload && payload.schemaVersion === APP_INCIDENT_SCHEMA_VERSION ? payload : null;
    } catch (_error) {
      return null;
    }
  }

  prune() {
    ensurePrivateDir(this.rootDir);
    const cutoff = this.now().getTime() - APP_INCIDENT_RETENTION_MS;
    const incidents = [];
    for (const name of fs.readdirSync(this.rootDir).slice(0, 128)) {
      if (!APP_INCIDENT_ID_RE.test(name)) continue;
      const incidentDir = this.incidentPath(name);
      const stats = safeStat(incidentDir);
      if (!stats?.isDirectory() || stats.isSymbolicLink()) continue;
      const manifest = this.readManifest(incidentDir);
      const createdAtMs = Date.parse(manifest?.createdAt || '') || stats.mtimeMs;
      if (createdAtMs < cutoff) {
        removeIncidentDirectory(this.rootDir, incidentDir);
        continue;
      }
      incidents.push({ incidentDir, createdAtMs, bytes: directorySize(incidentDir) });
    }
    incidents.sort((left, right) => right.createdAtMs - left.createdAtMs);
    let retainedBytes = 0;
    incidents.forEach((incident, index) => {
      const exceedsCount = index >= APP_INCIDENT_MAX_COUNT;
      const exceedsBytes = retainedBytes + incident.bytes > APP_INCIDENT_TOTAL_MAX_BYTES;
      if (exceedsCount || exceedsBytes) {
        removeIncidentDirectory(this.rootDir, incident.incidentDir);
      } else {
        retainedBytes += incident.bytes;
      }
    });
  }

  capture({ trigger, summary = '', backend = null, probes = [], details = null } = {}) {
    const createdAt = this.now();
    const incidentId = incidentIdFor(createdAt);
    const incidentDir = this.incidentPath(incidentId);
    ensurePrivateDir(incidentDir);
    const manifest = {
      schemaVersion: APP_INCIDENT_SCHEMA_VERSION,
      incidentId,
      createdAt: createdAt.toISOString(),
      updatedAt: createdAt.toISOString(),
      trigger: String(trigger || 'application_issue').slice(0, 120),
      summary: sanitizeLogText(summary, this.redactionRoots).slice(0, 1000),
      outcome: 'captured',
      app: {
        version: this.appVersion,
        channel: this.channel || null,
        platform: process.platform,
        architecture: process.arch,
        osVersion: os.release(),
        electronVersion: process.versions?.electron || null,
        chromiumVersion: process.versions?.chrome || null,
        nodeVersion: process.versions?.node || null,
        runtimeFingerprint: runtimeFingerprintForDiagnostics(this.runtimeFingerprintPath),
      },
      backend: sanitizeValue(backend, this.redactionRoots),
      probes: sanitizeValue(probes, this.redactionRoots),
      details: sanitizeValue(details, this.redactionRoots),
      privacy: {
        rawDatabaseIncluded: false,
        providerDiagnosticsIncluded: false,
        credentialsIncluded: false,
        sourceLogsSanitized: true,
      },
      policy: this.policy(),
    };
    writePrivateFile(path.join(incidentDir, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
    this.refreshLogs(incidentId);
    writePrivateFile(
      path.join(incidentDir, 'README.txt'),
      'BreakTwenty application diagnostic incident\n\nThis bundle contains bounded, sanitized technical logs and recovery metadata. It does not include the encrypted database, provider diagnostic artifacts, credentials, cookies, tokens, account records, balances, or transaction data. Review it before sharing and send it privately.\n',
    );
    this.prune();
    return this.summaryFor(incidentDir, manifest);
  }

  refreshLogs(incidentId, { suffix = '', electronMarker = '' } = {}) {
    const incidentDir = this.incidentPath(incidentId);
    if (!incidentDir || !this.readManifest(incidentDir)) return false;
    const normalizedSuffix = String(suffix || '');
    if (normalizedSuffix && !/^\.[a-z0-9-]{1,40}$/i.test(normalizedSuffix)) return false;
    const sources = [
      ['electron-main.log', this.electronLogPath],
      ['embedded-backend-process.log', this.backendLogPath],
    ];
    for (const [filename, sourcePath] of sources) {
      if (!sourcePath) continue;
      let tail = sanitizeLogText(readTail(sourcePath), this.redactionRoots);
      if (filename === 'electron-main.log' && electronMarker) {
        const marker = sanitizeLogText(String(electronMarker), this.redactionRoots).trim();
        if (marker && !tail.includes(marker)) tail = `${tail}${tail ? '\n' : ''}${marker}`;
      }
      if (tail) {
        const extension = path.extname(filename);
        const base = filename.slice(0, -extension.length);
        writePrivateFile(path.join(incidentDir, `${base}${normalizedSuffix}${extension}`), `${tail}\n`);
      }
    }
    return true;
  }

  finalize(incidentId, updates = {}) {
    const incidentDir = this.incidentPath(incidentId);
    if (!incidentDir) return null;
    const manifest = this.readManifest(incidentDir);
    if (!manifest) return null;
    const nextManifest = {
      ...manifest,
      ...sanitizeValue(updates, this.redactionRoots),
      incidentId: manifest.incidentId,
      schemaVersion: APP_INCIDENT_SCHEMA_VERSION,
      updatedAt: this.now().toISOString(),
    };
    writePrivateFile(path.join(incidentDir, 'manifest.json'), `${JSON.stringify(nextManifest, null, 2)}\n`);
    this.prune();
    return this.summaryFor(incidentDir, nextManifest);
  }

  summaryFor(incidentDir, manifest = null) {
    const payload = manifest || this.readManifest(incidentDir);
    if (!payload) return null;
    return {
      incidentId: payload.incidentId,
      createdAt: payload.createdAt,
      updatedAt: payload.updatedAt,
      trigger: payload.trigger,
      summary: payload.summary,
      outcome: payload.outcome,
      recovery: payload.recovery || null,
      bytes: directorySize(incidentDir),
    };
  }

  list() {
    this.prune();
    const incidents = [];
    for (const name of fs.readdirSync(this.rootDir).slice(0, 128)) {
      const incidentDir = this.incidentPath(name);
      if (!incidentDir) continue;
      const summary = this.summaryFor(incidentDir);
      if (summary) incidents.push(summary);
    }
    incidents.sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt));
    return incidents.slice(0, APP_INCIDENT_MAX_COUNT);
  }

  async exportIncident(incidentId, destinationPath) {
    const incidentDir = this.incidentPath(incidentId);
    const stats = safeStat(incidentDir);
    if (!incidentDir || !stats?.isDirectory() || stats.isSymbolicLink() || !this.readManifest(incidentDir)) {
      throw new Error('Application diagnostic incident was not found.');
    }
    const entries = [];
    let inputBytes = 0;
    for (const name of fs.readdirSync(incidentDir).sort().slice(0, 32)) {
      if (path.basename(name) !== name) continue;
      const filePath = path.join(incidentDir, name);
      const fileStats = safeStat(filePath);
      if (!fileStats?.isFile() || fileStats.isSymbolicLink()) continue;
      const data = sanitizeExportEntry(name, fs.readFileSync(filePath), this.redactionRoots);
      inputBytes += data.length;
      if (inputBytes > APP_INCIDENT_TOTAL_MAX_BYTES) {
        throw new Error('Application diagnostic incident exceeded its safety limit.');
      }
      entries.push({
        name,
        data,
        modifiedAt: fileStats.mtime,
      });
    }
    const archive = buildZipArchive(entries);
    if (archive.length <= 0 || archive.length > APP_INCIDENT_TOTAL_MAX_BYTES) {
      throw new Error('Application diagnostic archive exceeded its safety limit.');
    }
    fs.writeFileSync(destinationPath, archive, { mode: 0o600 });
    return { bytes: archive.length };
  }
}

module.exports = {
  APP_INCIDENT_LOG_TAIL_MAX_BYTES,
  APP_INCIDENT_MAX_COUNT,
  APP_INCIDENT_RETENTION_MS,
  APP_INCIDENT_TOTAL_MAX_BYTES,
  AppDiagnostics,
  buildZipArchive,
  createSanitizingLogger,
  incidentIdFor,
  sanitizeLogText,
  sanitizeValue,
};
