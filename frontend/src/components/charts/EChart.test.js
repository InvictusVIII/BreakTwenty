import React from 'react';
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  core: { init: vi.fn(), use: vi.fn() },
  charts: {
    BarChart: Symbol('BarChart'),
    LineChart: Symbol('LineChart'),
    PieChart: Symbol('PieChart'),
  },
  components: {
    GridComponent: Symbol('GridComponent'),
    LegendPlainComponent: Symbol('LegendPlainComponent'),
    MarkLineComponent: Symbol('MarkLineComponent'),
    TooltipComponent: Symbol('TooltipComponent'),
  },
  renderers: { CanvasRenderer: Symbol('CanvasRenderer') },
}));

vi.mock('echarts/core', () => mocks.core);
vi.mock('echarts/charts', () => mocks.charts);
vi.mock('echarts/components', () => mocks.components);
vi.mock('echarts/renderers', () => mocks.renderers);

const { default: EChart } = await import('./EChart');

describe('EChart module registration', () => {
  it('registers the complete chart surface without the all-in-one ECharts bundle', () => {
    expect(mocks.core.use).toHaveBeenCalledOnce();
    expect(mocks.core.use).toHaveBeenCalledWith([
      mocks.charts.BarChart,
      mocks.charts.LineChart,
      mocks.charts.PieChart,
      mocks.components.GridComponent,
      mocks.components.LegendPlainComponent,
      mocks.components.MarkLineComponent,
      mocks.components.TooltipComponent,
      mocks.renderers.CanvasRenderer,
    ]);
  });
});

describe('EChart lifecycle', () => {
  let animationFrames;
  let chart;
  let chartRect;
  let nextAnimationFrameId;
  let resizeObservers;
  let visualViewport;

  function createChart() {
    const zr = {
      off: vi.fn(),
      on: vi.fn(),
      setCursorStyle: vi.fn(),
    };
    return {
      containPixel: vi.fn(() => false),
      convertFromPixel: vi.fn(() => null),
      dispose: vi.fn(),
      getZr: vi.fn(() => zr),
      isDisposed: vi.fn(() => false),
      off: vi.fn(),
      on: vi.fn(),
      resize: vi.fn(),
      setOption: vi.fn(),
      zr,
    };
  }

  function flushNextAnimationFrame() {
    const entry = animationFrames.entries().next().value;
    expect(entry).toBeDefined();
    const [frameId, callback] = entry;
    animationFrames.delete(frameId);
    act(() => callback(0));
    return frameId;
  }

  beforeEach(() => {
    animationFrames = new Map();
    nextAnimationFrameId = 1;
    resizeObservers = [];
    visualViewport = {
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      scale: 1,
    };
    class TestResizeObserver {
      constructor(callback) {
        this.callback = callback;
        this.disconnect = vi.fn();
        this.observe = vi.fn();
        resizeObservers.push(this);
      }
    }
    vi.stubGlobal('ResizeObserver', TestResizeObserver);
    vi.stubGlobal('devicePixelRatio', 1);
    vi.stubGlobal('visualViewport', visualViewport);
    vi.stubGlobal('requestAnimationFrame', vi.fn((callback) => {
      const frameId = nextAnimationFrameId;
      nextAnimationFrameId += 1;
      animationFrames.set(frameId, callback);
      return frameId;
    }));
    vi.stubGlobal('cancelAnimationFrame', vi.fn((frameId) => {
      animationFrames.delete(frameId);
    }));
    chartRect = {
      bottom: 220,
      height: 200,
      left: 10,
      right: 310,
      top: 20,
      width: 300,
      x: 10,
      y: 20,
      toJSON: () => ({}),
    };
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(() => chartRect);

    chart = createChart();
    mocks.core.init.mockReset();
    mocks.core.init.mockReturnValue(chart);
  });

  afterEach(() => {
    cleanup();
    mocks.core.init.mockReset();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('waits two animation frames before the one hidden initial option paint', () => {
    const initialOption = { series: [{ data: [1] }] };
    const nextOption = { series: [{ data: [2] }] };
    const view = render(<EChart option={initialOption} height={200} />);
    const root = view.container.firstElementChild;

    expect(root).toHaveStyle({ visibility: 'hidden' });
    expect(chart.setOption).not.toHaveBeenCalled();

    flushNextAnimationFrame();
    expect(chart.setOption).not.toHaveBeenCalled();
    expect(root).toHaveStyle({ visibility: 'hidden' });

    flushNextAnimationFrame();
    expect(chart.setOption).toHaveBeenCalledTimes(1);
    expect(chart.setOption).toHaveBeenLastCalledWith(
      { ...initialOption, animation: false },
      { notMerge: true },
    );
    expect(root).toHaveStyle({ visibility: 'visible' });
    expect(initialOption).not.toHaveProperty('animation');

    view.rerender(<EChart option={nextOption} height={200} />);
    expect(chart.setOption).toHaveBeenCalledTimes(2);
    expect(chart.setOption).toHaveBeenLastCalledWith(nextOption, { notMerge: true });
  });

  it('cancels a pending initial paint and disposes the chart on unmount', () => {
    const view = render(<EChart option={{ series: [] }} />);
    flushNextAnimationFrame();
    const pendingFrameId = [...animationFrames.keys()][0];

    view.unmount();

    expect(cancelAnimationFrame).toHaveBeenCalledWith(pendingFrameId);
    expect(chart.setOption).not.toHaveBeenCalled();
    expect(chart.dispose).toHaveBeenCalledOnce();
  });

  it('schedules a delayed initial paint when an option arrives after mount', () => {
    const view = render(<EChart option={null} />);
    flushNextAnimationFrame();
    flushNextAnimationFrame();
    flushNextAnimationFrame();
    expect(chart.setOption).not.toHaveBeenCalled();

    const option = { series: [{ data: [3] }] };
    view.rerender(<EChart option={option} />);
    expect(chart.setOption).not.toHaveBeenCalled();
    flushNextAnimationFrame();
    expect(chart.setOption).not.toHaveBeenCalled();
    flushNextAnimationFrame();

    expect(chart.setOption).toHaveBeenCalledOnce();
    expect(chart.setOption).toHaveBeenCalledWith(
      { ...option, animation: false },
      { notMerge: true },
    );
  });

  it('reinitializes for a DPR change and reapplies the current option after the delayed paint', () => {
    const initialOption = { series: [{ data: [1] }] };
    const currentOption = { series: [{ data: [2] }] };
    const replacementChart = createChart();
    mocks.core.init.mockReturnValueOnce(chart).mockReturnValueOnce(replacementChart);
    const view = render(<EChart option={initialOption} />);
    flushNextAnimationFrame();
    flushNextAnimationFrame();
    view.rerender(<EChart option={currentOption} />);
    expect(chart.setOption).toHaveBeenLastCalledWith(currentOption, { notMerge: true });

    vi.stubGlobal('devicePixelRatio', 3);
    act(() => window.dispatchEvent(new Event('resize')));

    expect(chart.dispose).toHaveBeenCalledOnce();
    expect(mocks.core.init).toHaveBeenCalledTimes(2);
    expect(mocks.core.init).toHaveBeenLastCalledWith(
      expect.any(HTMLElement),
      null,
      expect.objectContaining({ devicePixelRatio: 3 }),
    );
    expect(replacementChart.setOption).not.toHaveBeenCalled();

    flushNextAnimationFrame();
    flushNextAnimationFrame();
    flushNextAnimationFrame();
    expect(replacementChart.setOption).toHaveBeenCalledOnce();
    expect(replacementChart.setOption).toHaveBeenCalledWith(
      { ...currentOption, animation: false },
      { notMerge: true },
    );
  });

  it('coalesces ResizeObserver notifications, skips unchanged size, and resizes on a real change', () => {
    render(<EChart option={{ series: [] }} />);
    flushNextAnimationFrame();
    flushNextAnimationFrame();
    flushNextAnimationFrame();
    chart.resize.mockClear();

    act(() => {
      resizeObservers[0].callback();
      resizeObservers[0].callback();
    });
    expect(animationFrames.size).toBe(1);
    flushNextAnimationFrame();
    expect(chart.resize).not.toHaveBeenCalled();

    chartRect = { ...chartRect, bottom: 240, height: 220 };
    act(() => {
      resizeObservers[0].callback();
      resizeObservers[0].callback();
    });
    expect(animationFrames.size).toBe(1);
    flushNextAnimationFrame();
    expect(chart.resize).toHaveBeenCalledOnce();
    expect(chart.resize).toHaveBeenCalledWith({ width: 300, height: 220 });
  });

  it('keeps current event callback ownership and guards grid interaction until the model is ready', () => {
    const firstEventHandler = vi.fn();
    const currentEventHandler = vi.fn();
    const firstGridHandler = vi.fn();
    const currentGridHandler = vi.fn();
    const onChartReady = vi.fn();
    const view = render(
      <EChart
        option={{ series: [] }}
        onEvents={{ click: firstEventHandler }}
        onGridClick={firstGridHandler}
        onChartReady={onChartReady}
      />,
    );
    const eventHandler = chart.on.mock.calls.find(([name]) => name === 'click')[1];
    const gridClickHandler = chart.zr.on.mock.calls.find(([name]) => name === 'click')[1];
    const gridMoveHandler = chart.zr.on.mock.calls.find(([name]) => name === 'mousemove')[1];
    expect(onChartReady).toHaveBeenCalledWith(chart);

    gridClickHandler({ offsetX: 12, offsetY: 18 });
    gridMoveHandler({ offsetX: 12, offsetY: 18 });
    expect(chart.containPixel).not.toHaveBeenCalled();

    view.rerender(
      <EChart
        option={{ series: [] }}
        onEvents={{ click: currentEventHandler }}
        onGridClick={currentGridHandler}
        onChartReady={onChartReady}
      />,
    );
    eventHandler({ dataIndex: 4 });
    expect(firstEventHandler).not.toHaveBeenCalled();
    expect(currentEventHandler).toHaveBeenCalledWith({ dataIndex: 4 });

    flushNextAnimationFrame();
    flushNextAnimationFrame();
    chart.containPixel.mockReturnValueOnce(false).mockReturnValue(true);
    gridClickHandler({ offsetX: 12, offsetY: 18 });
    expect(chart.convertFromPixel).not.toHaveBeenCalled();

    chart.convertFromPixel.mockReturnValueOnce(null).mockReturnValueOnce([2.6, 8]);
    gridClickHandler({ offsetX: 12, offsetY: 18 });
    expect(currentGridHandler).not.toHaveBeenCalled();
    gridClickHandler({ offsetX: 12, offsetY: 18 });
    expect(currentGridHandler).toHaveBeenCalledWith(3);
    expect(firstGridHandler).not.toHaveBeenCalled();

    gridMoveHandler({ offsetX: 12, offsetY: 18 });
    expect(chart.zr.setCursorStyle).toHaveBeenCalledWith('pointer');
  });

  it('layers and positions the default body tooltip without mutating the caller option', () => {
    vi.stubGlobal('innerWidth', 300);
    vi.stubGlobal('innerHeight', 200);
    const option = { tooltip: { extraCssText: 'color:red' }, series: [] };
    render(<EChart option={option} />);
    flushNextAnimationFrame();
    flushNextAnimationFrame();

    const appliedOption = chart.setOption.mock.calls[0][0];
    const tooltipDom = document.createElement('div');
    const position = appliedOption.tooltip.position(
      [50, 50],
      {},
      tooltipDom,
      {},
      { contentSize: [40, 20] },
    );

    expect(position).toEqual([-2, 16]);
    expect(appliedOption.tooltip.appendToBody).toBe(true);
    expect(appliedOption.tooltip.extraCssText).toContain('color:red;');
    expect(appliedOption.tooltip.extraCssText).toContain('z-index:var(--app-tooltip-layer);');
    expect(appliedOption.tooltip.extraCssText).toContain('pointer-events:none;');
    expect(option.tooltip).not.toHaveProperty('position');
    expect(option.tooltip).not.toHaveProperty('appendToBody');
  });

  it('does not wrap custom tooltip positions', () => {
    const position = vi.fn(() => [10, 20]);
    const option = { tooltip: { position, extraCssText: 'box-shadow:none;' }, series: [] };
    render(<EChart option={option} />);
    flushNextAnimationFrame();
    flushNextAnimationFrame();

    const appliedOption = chart.setOption.mock.calls[0][0];

    expect(appliedOption.tooltip.position).toBe(position);
    expect(appliedOption.tooltip.appendToBody).toBe(true);
    expect(appliedOption.tooltip.extraCssText).toContain('box-shadow:none;');
    expect(appliedOption.tooltip.extraCssText).toContain('z-index:var(--app-tooltip-layer);');
    expect(appliedOption.tooltip.extraCssText).toContain('pointer-events:none;');
  });

  it('disconnects observers, timers, global listeners, and chart handlers on teardown', () => {
    const windowRemoveSpy = vi.spyOn(window, 'removeEventListener');
    const setIntervalSpy = vi.spyOn(window, 'setInterval');
    const clearIntervalSpy = vi.spyOn(window, 'clearInterval');
    const view = render(
      <EChart option={{ series: [] }} onEvents={{ click: vi.fn() }} onGridClick={vi.fn()} />,
    );
    const intervalId = setIntervalSpy.mock.results[0]?.value;
    const eventHandler = chart.on.mock.calls.find(([name]) => name === 'click')[1];
    const gridClickHandler = chart.zr.on.mock.calls.find(([name]) => name === 'click')[1];
    const gridMoveHandler = chart.zr.on.mock.calls.find(([name]) => name === 'mousemove')[1];

    view.unmount();

    expect(resizeObservers[0].disconnect).toHaveBeenCalledOnce();
    expect(windowRemoveSpy).toHaveBeenCalledWith('resize', expect.any(Function));
    expect(visualViewport.removeEventListener).toHaveBeenCalledWith('resize', expect.any(Function));
    expect(intervalId).toBeDefined();
    expect(clearIntervalSpy).toHaveBeenCalledWith(intervalId);
    expect(chart.off).toHaveBeenCalledWith('click', eventHandler);
    expect(chart.zr.off).toHaveBeenCalledWith('click', gridClickHandler);
    expect(chart.zr.off).toHaveBeenCalledWith('mousemove', gridMoveHandler);
    expect(chart.dispose).toHaveBeenCalledOnce();
  });
});
