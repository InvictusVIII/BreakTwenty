import { shouldResetInvestmentsTab, shouldShowInvestmentsTab } from './investmentsTabs';

describe('Investments tab availability during holdings refreshes', () => {
  const tabVisibility = {
    overview: true,
    holdings: true,
    options: false,
    crypto: false,
  };

  it('keeps the active tab mounted while the newly scoped holdings are loading', () => {
    expect(shouldResetInvestmentsTab('crypto', tabVisibility, false)).toBe(false);
    expect(shouldShowInvestmentsTab('crypto', 'crypto', tabVisibility, false)).toBe(true);
  });

  it('falls back only after the active tab is confirmed empty', () => {
    expect(shouldResetInvestmentsTab('crypto', tabVisibility, true)).toBe(true);
    expect(shouldShowInvestmentsTab('crypto', 'crypto', tabVisibility, true)).toBe(false);
  });

  it('preserves an active tab that still has data after loading', () => {
    expect(shouldResetInvestmentsTab('holdings', tabVisibility, true)).toBe(false);
    expect(shouldShowInvestmentsTab('holdings', 'holdings', tabVisibility, true)).toBe(true);
  });
});
