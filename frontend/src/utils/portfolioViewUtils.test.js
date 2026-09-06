import {
  PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
  changePercentFromBaseline,
  cloneScopeInstitutions,
  formatChangePercentCompact,
  formatChangePercentFull,
  getAccountChange,
  getAccountDisplayBalance,
  getAccountGroupChangeSummary,
  getAggregateHistoryMetricChange,
  loadPortfolioChartColors,
  resolvePortfolioNetWorthChartColor,
} from './portfolioViewUtils';
import { getAppliedBreakTwentyChartColors } from '../theme/applyTheme';

const PORTFOLIO_CHART_COLORS_STORAGE_KEY = 'breaktwenty_dashboard_chart_colors_v2';

test('clones the canonical scope-institution shape', () => {
  const source = [{ id: 1, accountIds: [2], accounts: [{ id: 2, name: 'Cash' }] }];
  const cloned = cloneScopeInstitutions(source);

  expect(cloned).toEqual(source);
  expect(cloned[0]).not.toBe(source[0]);
  expect(cloned[0].accountIds).not.toBe(source[0].accountIds);
  expect(cloned[0].accounts[0]).not.toBe(source[0].accounts[0]);
});

describe('getAggregateHistoryMetricChange', () => {
  const history = [
    { date: '2020-10-19', total_assets: 5000, total_liabilities: 0, net_worth: 5000 },
    { date: '2024-12-12', total_assets: 100000, total_liabilities: 30000, net_worth: 70000 },
    { date: '2026-08-02', total_assets: 227791.21, total_liabilities: 49433.05, net_worth: 178358.16 },
  ];

  it('uses the first and last aggregate graph points as the shared metric baseline', () => {
    expect(getAggregateHistoryMetricChange(history, 'total_assets')).toEqual({
      diff: 222791.21,
      pct: 4455.8,
      positive: true,
      start: 5000,
      end: 227791.21,
    });
    expect(getAggregateHistoryMetricChange(history, 'net_worth')).toEqual({
      diff: 173358.16,
      pct: 3467.2,
      positive: true,
      start: 5000,
      end: 178358.16,
    });
  });

  it('omits the percentage when the aggregate starting value is zero', () => {
    const change = getAggregateHistoryMetricChange(history, 'total_liabilities');
    expect(change).toEqual({
      diff: 49433.05,
      pct: null,
      positive: true,
      start: 0,
      end: 49433.05,
    });
    expect(formatChangePercentFull(change.pct)).toBeNull();
    expect(formatChangePercentCompact(change.pct)).toBeNull();
  });
});

describe('getAccountChange', () => {
  it('does not compute runaway percentages from cent-level zero baselines', () => {
    const change = getAccountChange(
      33,
      { 33: { '2024-12-14': -3.637978807091713e-12 } },
      'All',
      28872.01,
      true,
    );

    expect(change.diff).toBe(-28872.01);
    expect(change.pct).toBeNull();
    expect(changePercentFromBaseline(-28872.01, -3.637978807091713e-12)).toBeNull();
  });

  it('uses the first recorded point as the all-time graph-delta baseline', () => {
    const change = getAccountChange(
      33,
      {
        33: {
          '2024-12-12': 400,
          '2024-12-14': 0,
          '2026-06-02': 28872.01,
        },
      },
      'All',
      28872.01,
      true,
    );

    expect(change.startBalance).toBe(400);
    expect(change.diff).toBe(-28472.01);
    expect(change.pct).toBe(-7118);
    expect(change.startDate).toBe('2024-12-12');
  });

  it('shows no change for custom ranges that end before the first recorded point', () => {
    const change = getAccountChange(
      33,
      {
        33: {
          '2024-12-14': 0,
          '2026-06-02': 28872.01,
        },
      },
      PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
      28872.01,
      true,
      { start: '2023-01-01', end: '2023-12-31' },
    );

    expect(change.diff).toBe(0);
    expect(change.pct).toBe(0);
    expect(change.startDate).toBe('2023-01-01');
    expect(change.endDate).toBe('2023-12-31');
  });

  it('surfaces the first recorded date when a custom range starts before the account exists', () => {
    const change = getAccountChange(
      33,
      {
        33: {
          '2024-12-14': 400,
          '2026-06-02': 28872.01,
        },
      },
      PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
      28872.01,
      true,
      { start: '2023-01-01', end: '2026-06-19' },
    );

    expect(change.startDate).toBe('2024-12-14');
    expect(change.endDate).toBe('2026-06-19');
    expect(change.startBalance).toBe(400);
  });

  it('keeps the requested custom start date when a prior balance can be carried forward', () => {
    const change = getAccountChange(
      33,
      {
        33: {
          '2024-12-14': 400,
          '2026-06-02': 28872.01,
        },
      },
      PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
      28872.01,
      true,
      { start: '2025-01-01', end: '2026-06-19' },
    );

    expect(change.startDate).toBe('2025-01-01');
    expect(change.startBalance).toBe(400);
  });

  it('uses the custom range end balance instead of the current balance for account display', () => {
    const account = { id: 33, balance: 28872.01 };
    const balanceHistory = {
      33: {
        '2024-12-12': 400,
        '2026-06-02': 28872.01,
      },
    };

    expect(getAccountDisplayBalance(
      account,
      balanceHistory,
      PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
      { start: '2024-12-01', end: '2024-12-31' },
    )).toBe(400);

    expect(getAccountDisplayBalance(
      account,
      balanceHistory,
      PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
      { start: '2023-01-01', end: '2023-12-31' },
    )).toBe(0);
  });
});

describe('getAccountGroupChangeSummary', () => {
  it('treats imported defunct accounts as zero-change contributors in rollups', () => {
    const summary = getAccountGroupChangeSummary(
      [
        {
          id: 1,
          balance: 150,
          currency: 'CAD',
          is_liability: false,
          is_imported: false,
        },
        {
          id: 2,
          balance: 0,
          currency: 'CAD',
          is_liability: false,
          is_imported: true,
        },
      ],
      {
        1: { '2024-01-01': 100 },
        2: { '2023-01-01': 5000 },
      },
      'All',
      null,
      (value) => value,
      (account) => account.balance,
      null,
    );

    expect(summary.hasData).toBe(true);
    expect(summary.hasFallbackOnlyData).toBe(false);
    expect(summary.diff).toBe(50);
    expect(summary.start).toBe(100);
    expect(summary.end).toBe(150);
  });

  it('returns explicit zero change when a rollup only contains imported defunct accounts', () => {
    const summary = getAccountGroupChangeSummary(
      [
        {
          id: 2,
          balance: 0,
          currency: 'CAD',
          is_liability: false,
          is_imported: true,
        },
      ],
      {
        2: { '2023-01-01': 5000 },
      },
      'All',
      null,
      (value) => value,
      (account) => account.balance,
      null,
    );

    expect(summary.hasData).toBe(true);
    expect(summary.hasFallbackOnlyData).toBe(false);
    expect(summary.diff).toBe(0);
    expect(summary.start).toBe(0);
    expect(summary.end).toBe(0);
  });
});

describe('loadPortfolioChartColors', () => {
  afterEach(() => {
    localStorage.removeItem(PORTFOLIO_CHART_COLORS_STORAGE_KEY);
  });

  it('leaves net worth on the active theme default when no custom color is stored', () => {
    expect(loadPortfolioChartColors().netWorth).toBeNull();
  });

  it('preserves a custom net worth chart color across theme changes', () => {
    localStorage.setItem(PORTFOLIO_CHART_COLORS_STORAGE_KEY, JSON.stringify({ netWorth: '#123abc' }));
    expect(loadPortfolioChartColors().netWorth).toBe('#123abc');
  });

  it('resolves net worth graph color from custom preference or active theme default', () => {
    const themeChartColors = getAppliedBreakTwentyChartColors('light');
    expect(resolvePortfolioNetWorthChartColor({ netWorth: null }, themeChartColors)).toBe(themeChartColors.netWorth);
    expect(resolvePortfolioNetWorthChartColor({ netWorth: '#123abc' }, themeChartColors)).toBe('#123abc');
  });
});
