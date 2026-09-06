import { getSyncDisplayConfig } from '../constants/providers';
import { isClientNetworkFailure } from './syncRequests';

const DEFAULT_SYNC_ERROR_MESSAGE = 'Sync failed - please retry';

export function formatSyncErrorMessage(provider, message, { fallback = DEFAULT_SYNC_ERROR_MESSAGE } = {}) {
  const normalizedMessage = (message || '').trim();
  if (!normalizedMessage) {
    return fallback;
  }
  const messageAppend = (getSyncDisplayConfig(provider).messageAppends || []).find((rule) => (
    normalizedMessage.toLowerCase().includes(String(rule.messageIncludes || '').toLowerCase())
  ));
  if (messageAppend?.append) {
    return `${normalizedMessage} ${messageAppend.append}`;
  }
  return normalizedMessage;
}

export function getClientSyncFailureState(provider, error, options) {
  if (isClientNetworkFailure(error)) {
    return {
      status: 'network_error',
      message: formatSyncErrorMessage(provider, 'Connection failed', options),
    };
  }
  return {
    status: 'error',
    message: formatSyncErrorMessage(provider, error?.message, options),
  };
}
