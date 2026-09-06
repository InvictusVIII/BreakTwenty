const TOKEN_RE = /^[A-Za-z0-9+/]{43}=$/;
const LOOPBACK_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]']);

let authenticatedFetchImpl = null;
let apiBoundary = null;

function validateToken(value) {
  const token = String(value || '');
  if (!TOKEN_RE.test(token)) {
    throw new Error('BreakTwenty launch authentication is unavailable.');
  }
  return token;
}

function normalizeApiBoundary(value) {
  const raw = String(value || '');
  if (!raw || raw !== raw.trim() || raw.includes('\\')) {
    throw new Error('BreakTwenty backend URL must use the private local API boundary.');
  }
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (error) {
    throw new Error(
      'BreakTwenty backend URL must use the private local API boundary.',
      { cause: error },
    );
  }
  const pathPrefix = parsed.pathname.replace(/\/+$/, '') || '/';
  if (
    parsed.protocol !== 'http:'
    || !LOOPBACK_HOSTNAMES.has(parsed.hostname.toLowerCase())
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || parsed.port === '0'
    || pathPrefix !== '/api'
  ) {
    throw new Error('BreakTwenty backend URL must use the private local API boundary.');
  }
  return {
    origin: parsed.origin,
    pathPrefix: '/api',
    backendApiUrl: `${parsed.origin}/api`,
  };
}

function requestUrl(input) {
  if (input instanceof Request) return new URL(input.url);
  return new URL(String(input), window.location.href);
}

export function isBackendApiUrl(input) {
  if (!apiBoundary) return false;
  try {
    const parsed = requestUrl(input);
    return !parsed.username
      && !parsed.password
      && parsed.protocol === 'http:'
      && parsed.origin === apiBoundary.origin
      && (parsed.pathname === apiBoundary.pathPrefix
        || parsed.pathname.startsWith(`${apiBoundary.pathPrefix}/`));
  } catch (_error) {
    return false;
  }
}

function authenticatedRequest(input, init, token) {
  const headers = new Headers(input instanceof Request ? input.headers : undefined);
  if (init?.headers) {
    new Headers(init.headers).forEach((value, name) => headers.set(name, value));
  }
  headers.set('Authorization', `Bearer ${token}`);
  if (input instanceof Request) {
    return new Request(input, { ...init, headers });
  }
  return [input, { ...init, headers }];
}

class FetchEventSource {
  static CONNECTING = 0;

  static OPEN = 1;

  static CLOSED = 2;

  constructor(url, options = {}) {
    this.url = String(url);
    this.withCredentials = options.withCredentials === true;
    this.readyState = FetchEventSource.CONNECTING;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.listeners = new Map();
    this.abortController = null;
    this.closed = false;
    this.lastEventId = '';
    this.retryMs = 3000;
    void this.connect();
  }

  addEventListener(type, callback) {
    if (typeof callback !== 'function') return;
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(callback);
  }

  removeEventListener(type, callback) {
    this.listeners.get(type)?.delete(callback);
  }

  dispatch(type, event) {
    const propertyHandler = this[`on${type}`];
    if (typeof propertyHandler === 'function') propertyHandler.call(this, event);
    this.listeners.get(type)?.forEach((callback) => callback.call(this, event));
  }

  close() {
    this.closed = true;
    this.readyState = FetchEventSource.CLOSED;
    this.abortController?.abort();
  }

  async connect() {
    if (this.closed) return;
    this.readyState = FetchEventSource.CONNECTING;
    this.abortController = new AbortController();
    const headers = { Accept: 'text/event-stream' };
    if (this.lastEventId) headers['Last-Event-ID'] = this.lastEventId;
    try {
      const response = await apiFetch(this.url, {
        headers,
        cache: 'no-store',
        credentials: this.withCredentials ? 'include' : 'same-origin',
        signal: this.abortController.signal,
      });
      if (!response.ok || !response.body) {
        throw new Error(`Event stream returned HTTP ${response.status}.`);
      }
      this.readyState = FetchEventSource.OPEN;
      this.dispatch('open', new Event('open'));
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!this.closed) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        buffer = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        let boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
          this.consumeBlock(buffer.slice(0, boundary));
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf('\n\n');
        }
        if (done) break;
      }
      if (!this.closed) throw new Error('Event stream closed.');
    } catch (error) {
      if (this.closed || error?.name === 'AbortError') return;
      this.readyState = FetchEventSource.CONNECTING;
      this.dispatch('error', new Event('error'));
      window.setTimeout(() => { void this.connect(); }, this.retryMs);
    }
  }

  consumeBlock(block) {
    let eventType = 'message';
    const data = [];
    block.split('\n').forEach((line) => {
      if (!line || line.startsWith(':')) return;
      const separator = line.indexOf(':');
      const field = separator < 0 ? line : line.slice(0, separator);
      let value = separator < 0 ? '' : line.slice(separator + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'data') data.push(value);
      if (field === 'event' && value) eventType = value;
      if (field === 'id' && !value.includes('\0')) this.lastEventId = value;
      if (field === 'retry' && /^\d+$/.test(value)) this.retryMs = Number(value);
    });
    if (data.length === 0) return;
    const event = new MessageEvent(eventType, {
      data: data.join('\n'),
      lastEventId: this.lastEventId,
      origin: new URL(this.url).origin,
    });
    this.dispatch(eventType, event);
  }
}

FetchEventSource.prototype.CONNECTING = FetchEventSource.CONNECTING;
FetchEventSource.prototype.OPEN = FetchEventSource.OPEN;
FetchEventSource.prototype.CLOSED = FetchEventSource.CLOSED;

export async function installApiTransport() {
  if (typeof window === 'undefined') return;
  if (authenticatedFetchImpl) return;
  if (typeof window.breaktwentyDesktop?.getLaunchAuth !== 'function') {
    throw new Error('BreakTwenty Desktop launch authentication is required.');
  }
  const auth = await window.breaktwentyDesktop.getLaunchAuth();
  const token = validateToken(auth?.accessToken);
  apiBoundary = normalizeApiBoundary(auth?.backendApiUrl);
  window.BREAKTWENTY_RUNTIME_CONFIG = {
    ...(window.BREAKTWENTY_RUNTIME_CONFIG || {}),
    backendApiUrl: apiBoundary.backendApiUrl,
  };
  const baseFetch = window.fetch.bind(window);
  authenticatedFetchImpl = (input, init) => {
    if (!isBackendApiUrl(input)) return baseFetch(input, init);
    const request = authenticatedRequest(input, init, token);
    return Array.isArray(request) ? baseFetch(request[0], request[1]) : baseFetch(request);
  };
  window.fetch = authenticatedFetchImpl;
  window.EventSource = FetchEventSource;
}

export function apiFetch(input, init) {
  // Resolve at call time so development-only outer transports (notably Promo)
  // remain the single API boundary for authenticated images and SSE as well as
  // ordinary fetches. In the normal app window.fetch is authenticatedFetchImpl.
  return window.fetch(input, init);
}
