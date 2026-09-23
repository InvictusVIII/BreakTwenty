import { describe, expect, it } from 'vitest';

import { formatPurchaseDate } from './date';

describe('formatPurchaseDate', () => {
  it('formats calendar dates without shifting to the prior local day', () => {
    const expected = new Date(2026, 7, 22).toLocaleDateString(
      undefined,
      { year: 'numeric', month: 'short', day: 'numeric' },
    );

    expect(formatPurchaseDate('2026-08-22')).toBe(expected);
    expect(formatPurchaseDate('2026-08-22T00:00:00Z')).toBe(expected);
  });

  it('returns an empty value for missing or invalid input', () => {
    expect(formatPurchaseDate()).toBe('');
    expect(formatPurchaseDate('not-a-date')).toBe('');
  });
});
