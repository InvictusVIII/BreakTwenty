import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import NetWorthChart from './NetWorthChart';
import EChart from './charts/EChart';

vi.mock('./charts/EChart', () => ({
  default: vi.fn(() => <div data-testid="networth-chart-echart" />),
}));

const chartColors = {
  grid: '#333333',
  highlightStroke: '#ffffff',
  highlightStrokeWidth: 2,
  tick: '#cccccc',
  netWorth: '#f6902a',
};

describe('NetWorthChart', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('uses native axis hover and clears the marker when the chart is left', () => {
    render(
      <NetWorthChart
        history={[
          { date: '2026-08-21', net_worth: 100 },
          { date: '2026-08-22', net_worth: 120 },
        ]}
        balancesHidden={false}
        color="#f6902a"
        chartColors={chartColors}
      />
    );

    const chart = {
      dispatchAction: vi.fn(),
    };
    act(() => {
      EChart.mock.calls[0][0].onChartReady(chart);
    });

    const props = EChart.mock.calls.at(-1)[0];
    expect(props.option.tooltip.trigger).toBe('axis');
    expect(props.option.tooltip).not.toHaveProperty('triggerOn');
    expect(props.onGridHover).toBeUndefined();
    expect(props.onGridLeave).toBeUndefined();

    act(() => {
      props.onEvents.globalout();
    });
    expect(chart.dispatchAction).toHaveBeenLastCalledWith({
      type: 'downplay',
      seriesIndex: 0,
    });
    expect(chart.dispatchAction).toHaveBeenCalledWith({ type: 'hideTip' });
  });
});
