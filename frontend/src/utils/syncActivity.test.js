import { expect, test } from 'vitest';
import {
  getActiveSyncActivityKeys,
  hasSettledSyncActivity,
} from './syncActivity';

test('tracks account syncs and transaction imports for authoritative status refreshes', () => {
  const activeKeys = getActiveSyncActivityKeys({
    active: [
      {
        activity_id: 'sync:tangerine',
        provider: 'tangerine',
        kind: 'accounts_sync',
        status: 'running',
      },
      {
        activity_id: 'transaction-import:rbc',
        provider: 'rbc',
        kind: 'transaction_import',
        status: 'queued',
      },
      {
        activity_id: 'sync:complete',
        provider: 'cibc',
        kind: 'accounts_sync',
        status: 'complete',
      },
    ],
  });

  expect([...activeKeys]).toEqual(['sync:tangerine', 'transaction-import:rbc']);
});

test('does not invent an identity for malformed activity records', () => {
  expect([...getActiveSyncActivityKeys({
    active: [{ provider: 'rbc', kind: 'accounts_sync', status: 'running' }],
  })]).toEqual([]);
});

test('detects one settled connection while a slower sibling remains active', () => {
  expect(hasSettledSyncActivity(
    new Set(['transaction-import:wise', 'sync:ibkr']),
    new Set(['sync:ibkr']),
  )).toBe(true);
  expect(hasSettledSyncActivity(
    new Set(['sync:ibkr']),
    new Set(['sync:ibkr']),
  )).toBe(false);
});
