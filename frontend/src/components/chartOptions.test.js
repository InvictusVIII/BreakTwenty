import { describe, expect, it } from 'vitest';
import { buildAllocationPieOption } from './AllocationChart';
import { buildNetWorthOption, buildUniformTickIndexes } from './NetWorthChart';
import { buildCfDonutOption } from '../utils/cashFlowChart';

const chartColors = {
  grid: '#333333',
  highlightStroke: '#ffffff',
  highlightStrokeWidth: 2,
  segmentBorder: '#111111',
};

describe('dashboard chart options', () => {
  it('lets allocation donut tooltips be owned by explicit segment hover events', () => {
    const item = {
      legendLabel: 'Questrade',
      legendColor: '#f6902a',
      value: 292.61,
    };
    const option = buildAllocationPieOption([item], 292.61, false, 'CAD', chartColors);

    expect(option.tooltip.triggerOn).toBe('none');
    expect(option.tooltip.transitionDuration).toBe(0);
    expect(option.tooltip.hideDelay).toBe(80);
    expect(option.tooltip.formatter({ dataIndex: 0, data: { _item: item } })).toContain('Questrade');
  });

  it('lets cash-flow donut tooltips be owned by explicit segment hover events', () => {
    const option = buildCfDonutOption(
      [{ name: 'Income', value: 5.94, color: '#00cc66' }],
      { total: 5.94, currency: 'CAD', balancesHidden: false, chartColors },
    );

    expect(option.tooltip.triggerOn).toBe('none');
    expect(option.tooltip.transitionDuration).toBe(0);
    expect(option.tooltip.hideDelay).toBe(80);
    expect(option.tooltip.formatter({
      name: 'Income',
      value: 5.94,
      data: { itemStyle: { color: '#00cc66' } },
    })).toContain('Income');
  });

  it('keeps the net-worth grid uniform for irregular history dates', () => {
    const history = [
      { date: '2024-02-21', net_worth: 88000 },
      { date: '2024-02-29', net_worth: 95000 },
      { date: '2024-03-08', net_worth: 70000 },
      { date: '2024-04-01', net_worth: 0 },
    ];
    const option = buildNetWorthOption(history, {
      balancesHidden: false,
      color: '#f6902a',
      currency: 'CAD',
      multiYear: false,
      tooltipPosition: () => [0, 0],
      emptyFrame: false,
      chartColors,
    });

    expect(option.xAxis.type).toBe('category');
    expect(option.xAxis.data).toEqual(['2024-02-21', '2024-02-29', '2024-03-08', '2024-04-01']);
    expect(option.series[0].data).toEqual([88000, 95000, 70000, 0]);
    expect(option.series[0].showSymbol).toBe(false);
    expect(option.xAxis.axisLabel.hideOverlap).toBe(true);
    expect(option.xAxis.axisLabel.interval(0)).toBe(true);
    expect(option.xAxis.axisLabel.interval(1)).toBe(true);
    expect(option.xAxis.splitLine.interval(2)).toBe(true);
    expect(option.tooltip.trigger).toBe('axis');
    expect(option.tooltip).not.toHaveProperty('triggerOn');
  });

  it('chooses evenly distributed net-worth tick indexes for dense history', () => {
    expect([...buildUniformTickIndexes(10)]).toEqual([0, 2, 3, 5, 6, 8, 9]);
  });
});
