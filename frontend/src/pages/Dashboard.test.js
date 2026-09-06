import { describe, expect, it } from 'vitest';
import {
  filterDashboardNetWorthHistory,
  getDashboardCashFlowDisplayCurrency,
  getMarketStripTilePresentation,
} from '../utils/dashboardViewUtils';

describe('filterDashboardNetWorthHistory', () => {
  it('keeps the loading-state history empty when a persisted preset is restored', () => {
    expect(filterDashboardNetWorthHistory([], '30D', null, new Date(2026, 8, 3))).toEqual([]);
  });

  it('keeps the latest real point when the selected preset has no recent points', () => {
    const history = [{ date: '2025-01-01', net_worth: 100 }];

    expect(filterDashboardNetWorthHistory(history, '30D', null, new Date(2026, 8, 3))).toEqual(history);
  });
});

describe('getMarketStripTilePresentation', () => {
  const tile = {
    change: 12.34,
    change_pct: 0.56,
    precision: 2,
    as_of: '2026-08-22',
    source: 'fmp',
  };

  it('does not show a cached label for active API market data restored from local cache', () => {
    const presentation = getMarketStripTilePresentation(tile, false);

    expect(presentation.asOfCopy).toBe('');
  });

  it('shows EOD date for keyless public daily market data', () => {
    const presentation = getMarketStripTilePresentation({ ...tile, source: 'fred' }, true);

    expect(presentation.asOfCopy).toBe('EOD Aug 22');
  });

  it('does not show an as-of label for active API market data', () => {
    const presentation = getMarketStripTilePresentation(tile, false);

    expect(presentation.asOfCopy).toBe('');
  });
});

describe('getDashboardCashFlowDisplayCurrency', () => {
  it('keeps a cached response in its own currency during a primary-currency switch', () => {
    const cachedBtcPayload = { period: { currency: 'BTC' } };

    expect(getDashboardCashFlowDisplayCurrency(cachedBtcPayload, 'CAD')).toBe('BTC');
  });

  it('uses the active currency before a cash-flow response exists', () => {
    expect(getDashboardCashFlowDisplayCurrency(null, 'ETH')).toBe('ETH');
  });
});
