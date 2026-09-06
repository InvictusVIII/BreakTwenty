export const DEFAULT_SYNC_REQUEST_TIMEOUT_MS = 60 * 1000;
export const USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS = 2 * 60 * 1000;
export const ADD_INSTITUTION_SYNC_REQUEST_TIMEOUT_MS = 2 * 60 * 1000;
export const PUSH_APPROVAL_TIMEOUT_MS = 90 * 1000;

const CLIENT_NETWORK_FAILURE_PATTERNS = [
  /failed to fetch/i,
  /networkerror/i,
  /network error/i,
  /load failed/i,
  /sync timed out/i,
];

export function fetchWithTimeout(url, options = {}, timeoutMs = DEFAULT_SYNC_REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const upstreamSignal = options.signal;
  const handleUpstreamAbort = () => controller.abort();
  if (upstreamSignal?.aborted) {
    controller.abort();
  } else {
    upstreamSignal?.addEventListener('abort', handleUpstreamAbort, { once: true });
  }
  const id = setTimeout(() => controller.abort(), timeoutMs);
  const cleanup = () => {
    clearTimeout(id);
    upstreamSignal?.removeEventListener('abort', handleUpstreamAbort);
  };

  return fetch(url, { ...options, signal: controller.signal })
    .then((resp) => {
      cleanup();
      return resp;
    })
    .catch((err) => {
      const cancelledByCaller = upstreamSignal?.aborted;
      cleanup();
      if (err.name !== 'AbortError') throw err;
      if (cancelledByCaller) {
        const cancelledError = new Error('Sync request was cancelled');
        cancelledError.name = 'AbortError';
        throw cancelledError;
      }
      throw new Error('Sync timed out');
    });
}

export function isClientNetworkFailure(errorOrMessage) {
  const name = String(errorOrMessage?.name || '').trim();
  const message = String(errorOrMessage?.message || errorOrMessage || '').trim();
  if (name === 'AbortError') {
    return true;
  }
  return CLIENT_NETWORK_FAILURE_PATTERNS.some((pattern) => pattern.test(message));
}
