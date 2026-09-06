import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, LineChart, PieChart } from 'echarts/charts';
import {
  GridComponent,
  LegendPlainComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendPlainComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
]);

// Thin, reusable React wrapper around a raw ECharts instance. One instance per
// mount; options are applied with notMerge so callers fully own the config.
// Resize is handled by a ResizeObserver. `onEvents` is a map of ECharts event name ->
// handler; handlers are looked up through a ref so the latest closure runs
// without re-binding (the SET of event names is assumed stable per chart).

const TOOLTIP_LAYER_EXTRA_CSS = 'z-index:var(--app-tooltip-layer); pointer-events:none;';

function appendTooltipLayerCss(extraCssText) {
  const existing = String(extraCssText || '').trim();
  return existing
    ? `${existing}${existing.endsWith(';') ? '' : ';'} ${TOOLTIP_LAYER_EXTRA_CSS}`
    : TOOLTIP_LAYER_EXTRA_CSS;
}

// Default tooltips anchor up-and-left of the cursor and clamp on-screen. Charts
// with deliberate positions keep their own behavior; the shared wrapper only
// adds the app tooltip layer/body escape. The caller's option is never mutated.
function withUpLeftTooltip(option, container) {
  if (!option || !option.tooltip) return option;
  const place = (tt) => {
    if (!tt) return tt;
    const layeredTooltip = {
      ...tt,
      appendToBody: tt.appendToBody ?? true,
      extraCssText: appendTooltipLayerCss(tt.extraCssText),
    };
    if (tt.position !== undefined) return layeredTooltip;
    return {
      ...layeredTooltip,
      position: (point, params, dom, rect, size) => {
        const offset = 14;
        const [w, h] = size.contentSize;
        let left = point[0] - w - offset;
        let top = point[1] - h - offset;
        const box = container?.getBoundingClientRect?.();
        if (box) {
          left = Math.max(8 - box.left, Math.min(left, window.innerWidth - 8 - w - box.left));
          top = Math.max(8 - box.top, Math.min(top, window.innerHeight - 8 - h - box.top));
        }
        return [left, top];
      },
    };
  };
  const tooltip = Array.isArray(option.tooltip) ? option.tooltip.map(place) : place(option.tooltip);
  return { ...option, tooltip };
}

// Canvas is the app-wide ECharts renderer; SVG made chart-heavy scroll visibly worse in Chromium.
const CHART_RENDERER = 'canvas';
const INITIAL_PAINT_FRAMES = 2;
const MAX_CHART_PIXEL_RATIO = 6;
const LOW_DENSITY_CHART_THRESHOLD = 1.5;
const LOW_DENSITY_SUPERSAMPLE = 2;

function getChartPixelRatio() {
  const viewportScale = window.visualViewport?.scale || 1;
  const ratio = (window.devicePixelRatio || 1) * viewportScale;
  if (!Number.isFinite(ratio) || ratio <= 0) return 1;
  const backingRatio = ratio < LOW_DENSITY_CHART_THRESHOLD
    ? ratio * LOW_DENSITY_SUPERSAMPLE
    : ratio;
  return Math.min(MAX_CHART_PIXEL_RATIO, backingRatio);
}

function measureChart(element) {
  const rect = element.getBoundingClientRect();
  return { width: rect.width, height: rect.height };
}

function chartSizeChanged(previousSize, nextSize) {
  return Math.abs(nextSize.width - previousSize.width) >= 0.1
    || Math.abs(nextSize.height - previousSize.height) >= 0.1;
}

export default function EChart({
  option,
  height = 300,
  className,
  onEvents = null,
  onGridClick = null,
  onChartReady = null,
}) {
  const elementRef = useRef(null);
  const chartRef = useRef(null);
  const optionRef = useRef(option);
  const onEventsRef = useRef(onEvents);
  const onGridClickRef = useRef(onGridClick);
  const onChartReadyRef = useRef(onChartReady);
  const scheduleInitialPaintRef = useRef(null);
  const initialPaintDoneRef = useRef(false);
  const [isPaintReady, setIsPaintReady] = useState(false);
  useLayoutEffect(() => {
    optionRef.current = option;
    onEventsRef.current = onEvents;
    onGridClickRef.current = onGridClick;
    onChartReadyRef.current = onChartReady;
  }, [option, onEvents, onGridClick, onChartReady]);
  // `setOption` runs in a separate effect (after paint) than `echarts.init`
  // (layout effect). Until the first option is applied the instance has no
  // model, so `containPixel`/`convertFromPixel` would dereference an undefined
  // model and throw. This guards the zr handlers across that window (and across
  // an unmount→remount triggered by, e.g., a currency switch that empties data).
  const modelReadyRef = useRef(false);
  const isChartLive = () => {
    const chart = chartRef.current;
    return Boolean(chart && !chart.isDisposed() && modelReadyRef.current);
  };

  useLayoutEffect(() => {
    let currentPixelRatio = getChartPixelRatio();
    let resizeFrame = 0;
    let lastChartSize = { width: 0, height: 0 };
    let initialPaintFrames = [];
    let boundHandlers = {};
    let zrClickHandler = null;
    let zrMoveHandler = null;

    const scheduleResize = (force = false) => {
      if (resizeFrame) return;
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = 0;
        const chart = chartRef.current;
        const element = elementRef.current;
        if (!chart || !element) return;
        const nextSize = measureChart(element);
        if (!force && !chartSizeChanged(lastChartSize, nextSize)) {
          return;
        }
        lastChartSize = nextSize;
        chart.resize(nextSize);
      });
    };

    const applyCurrentOption = ({ initial = false } = {}) => {
      if (chartRef.current && optionRef.current) {
        const nextOption = withUpLeftTooltip(optionRef.current, elementRef.current);
        chartRef.current.setOption(
          initial ? { ...nextOption, animation: false } : nextOption,
          { notMerge: true }
        );
        modelReadyRef.current = true;
        if (initial) {
          initialPaintDoneRef.current = true;
          setIsPaintReady(true);
        }
      }
    };

    const cancelInitialPaint = () => {
      initialPaintFrames.forEach((frameId) => window.cancelAnimationFrame(frameId));
      initialPaintFrames = [];
    };

    const scheduleInitialPaint = () => {
      cancelInitialPaint();
      let framesRemaining = INITIAL_PAINT_FRAMES;
      const run = () => {
        framesRemaining -= 1;
        if (framesRemaining > 0) {
          initialPaintFrames = [window.requestAnimationFrame(run)];
          return;
        }
        initialPaintFrames = [];
        applyCurrentOption({ initial: true });
        scheduleResize(true);
      };
      initialPaintFrames = [window.requestAnimationFrame(run)];
    };
    scheduleInitialPaintRef.current = () => {
      if (initialPaintFrames.length === 0) scheduleInitialPaint();
    };

    const unbindHandlers = () => {
      const chart = chartRef.current;
      if (!chart) return;
      Object.keys(boundHandlers).forEach((eventName) => chart.off(eventName, boundHandlers[eventName]));
      chart.getZr()?.off('click', zrClickHandler);
      chart.getZr()?.off('mousemove', zrMoveHandler);
      boundHandlers = {};
      zrClickHandler = null;
      zrMoveHandler = null;
    };

    const disposeChart = () => {
      const chart = chartRef.current;
      if (!chart) return;
      modelReadyRef.current = false;
      initialPaintDoneRef.current = false;
      setIsPaintReady(false);
      cancelInitialPaint();
      unbindHandlers();
      chart.dispose();
      chartRef.current = null;
    };

    const initChart = () => {
      currentPixelRatio = getChartPixelRatio();
      const initialSize = measureChart(elementRef.current);
      const chart = echarts.init(elementRef.current, null, {
        renderer: CHART_RENDERER,
        devicePixelRatio: currentPixelRatio,
        width: initialSize.width,
        height: initialSize.height,
      });
      chartRef.current = chart;
      lastChartSize = initialSize;
      if (onChartReadyRef.current) onChartReadyRef.current(chart);

      Object.keys(onEventsRef.current || {}).forEach((eventName) => {
        const handler = (params) => {
          const fn = (onEventsRef.current || {})[eventName];
          if (fn) fn(params);
        };
        boundHandlers[eventName] = handler;
        chart.on(eventName, handler);
      });

      // Grid-wide click: fires for clicks anywhere in the plot area (not only on a
      // series item) and resolves the category index under the cursor, so callers
      // can treat the whole hovered column as the click target.
      zrClickHandler = (event) => {
        const fn = onGridClickRef.current;
        if (!fn || !isChartLive()) return;
        const pixel = [event.offsetX, event.offsetY];
        if (!chartRef.current.containPixel('grid', pixel)) return;
        const coord = chartRef.current.convertFromPixel({ seriesIndex: 0 }, pixel);
        if (!coord) return;
        const dataIndex = Math.round(coord[0]);
        if (Number.isFinite(dataIndex)) fn(dataIndex);
      };
      chart.getZr().on('click', zrClickHandler);

      // When a grid click is wired up, show a pointer cursor across the whole plot
      // area (not just on bars) so the hovered column reads as clickable.
      zrMoveHandler = (event) => {
        if (!onGridClickRef.current || !isChartLive()) return;
        if (chartRef.current.containPixel('grid', [event.offsetX, event.offsetY])) {
          chartRef.current.getZr().setCursorStyle('pointer');
        }
      };
      chart.getZr().on('mousemove', zrMoveHandler);
      scheduleInitialPaint();
    };

    const handlePixelRatioChange = () => {
      const nextPixelRatio = getChartPixelRatio();
      if (Math.abs(nextPixelRatio - currentPixelRatio) < 0.001) {
        return;
      }
      if (resizeFrame) {
        window.cancelAnimationFrame(resizeFrame);
        resizeFrame = 0;
      }
      disposeChart();
      initChart();
      scheduleResize(true);
    };

    initChart();

    const resizeObserver = new ResizeObserver(() => scheduleResize());
    resizeObserver.observe(elementRef.current);
    window.addEventListener('resize', handlePixelRatioChange);
    window.visualViewport?.addEventListener('resize', handlePixelRatioChange);
    const pixelRatioInterval = window.setInterval(handlePixelRatioChange, 500);

    return () => {
      scheduleInitialPaintRef.current = null;
      window.clearInterval(pixelRatioInterval);
      window.removeEventListener('resize', handlePixelRatioChange);
      window.visualViewport?.removeEventListener('resize', handlePixelRatioChange);
      resizeObserver.disconnect();
      cancelInitialPaint();
      if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
      disposeChart();
    };
  }, []);

  useEffect(() => {
    if (chartRef.current && option) {
      if (!initialPaintDoneRef.current) {
        scheduleInitialPaintRef.current?.();
        return;
      }
      chartRef.current.setOption(withUpLeftTooltip(option, elementRef.current), { notMerge: true });
      modelReadyRef.current = true;
    }
  }, [option]);

  return (
    <div
      key={CHART_RENDERER}
      ref={elementRef}
      className={className}
      data-echarts-renderer={CHART_RENDERER}
      style={{ width: '100%', height, visibility: isPaintReady ? 'visible' : 'hidden' }}
    />
  );
}
