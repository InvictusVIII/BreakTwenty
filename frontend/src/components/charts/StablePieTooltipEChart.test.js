import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import StablePieTooltipEChart from './StablePieTooltipEChart';

const echartProps = vi.hoisted(() => ({
  latest: null,
}));

vi.mock('./EChart', () => ({
  default: (props) => {
    echartProps.latest = props;
    return <div data-testid="echart" />;
  },
}));

describe('StablePieTooltipEChart', () => {
  beforeEach(() => {
    echartProps.latest = null;
  });

  it('shows pie tooltips only when the hovered segment changes', () => {
    const chart = { dispatchAction: vi.fn() };
    render(<StablePieTooltipEChart option={{ series: [] }} />);
    echartProps.latest.onChartReady(chart);

    const segment = { componentType: 'series', seriesType: 'pie', seriesIndex: 0, dataIndex: 1 };
    echartProps.latest.onEvents.mouseover(segment);
    echartProps.latest.onEvents.mouseover(segment);
    echartProps.latest.onEvents.mouseover({ ...segment, dataIndex: 2 });

    expect(chart.dispatchAction).toHaveBeenCalledTimes(2);
    expect(chart.dispatchAction).toHaveBeenNthCalledWith(1, { type: 'showTip', seriesIndex: 0, dataIndex: 1 });
    expect(chart.dispatchAction).toHaveBeenNthCalledWith(2, { type: 'showTip', seriesIndex: 0, dataIndex: 2 });
  });
});
