import { formatSyncErrorMessage, getClientSyncFailureState } from './clientSyncFailure';

describe('client sync failure formatting', () => {
  it('uses provider-catalog message appends', () => {
    expect(formatSyncErrorMessage('ibkr', 'Token has expired')).toBe(
      'Token has expired Update your IBKR Flex token in Settings.',
    );
  });

  it('preserves caller-specific empty-message fallbacks', () => {
    expect(formatSyncErrorMessage(null, '')).toBe('Sync failed - please retry');
    expect(formatSyncErrorMessage(null, '', { fallback: 'Sync failed' })).toBe('Sync failed');
  });

  it('classifies client network failures', () => {
    expect(getClientSyncFailureState('wise', new TypeError('Failed to fetch'))).toEqual({
      status: 'network_error',
      message: 'Connection failed',
    });
  });
});
