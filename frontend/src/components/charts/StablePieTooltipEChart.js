import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import EChart from './EChart';

export default function StablePieTooltipEChart({
  option,
  height = 300,
  className,
  onEvents = null,
  onChartReady = null,
  hideDelay = 60,
}) {
  const chartRef = useRef(null);
  const hoveredSliceRef = useRef(null);
  const hideTimerRef = useRef(0);

  const clearHideTimer = useCallback(() => {
    if (!hideTimerRef.current) return;
    window.clearTimeout(hideTimerRef.current);
    hideTimerRef.current = 0;
  }, []);

  const hideTooltip = useCallback(() => {
    clearHideTimer();
    hoveredSliceRef.current = null;
    chartRef.current?.dispatchAction?.({ type: 'hideTip' });
  }, [clearHideTimer]);

  const scheduleHideTooltip = useCallback(() => {
    clearHideTimer();
    hideTimerRef.current = window.setTimeout(hideTooltip, hideDelay);
  }, [clearHideTimer, hideDelay, hideTooltip]);

  const handleChartReady = useCallback((chart) => {
    chartRef.current = chart;
    onChartReady?.(chart);
  }, [onChartReady]);

  const mergedEvents = useMemo(() => ({
    ...(onEvents || {}),
    mouseover: (params) => {
      onEvents?.mouseover?.(params);
      if (params?.componentType !== 'series' || params?.seriesType !== 'pie') return;
      clearHideTimer();
      const nextSlice = `${params.seriesIndex}:${params.dataIndex}`;
      if (hoveredSliceRef.current === nextSlice) return;
      hoveredSliceRef.current = nextSlice;
      chartRef.current?.dispatchAction?.({
        type: 'showTip',
        seriesIndex: params.seriesIndex,
        dataIndex: params.dataIndex,
      });
    },
    mouseout: (params) => {
      onEvents?.mouseout?.(params);
      if (params?.componentType !== 'series' || params?.seriesType !== 'pie') return;
      if (hoveredSliceRef.current !== `${params.seriesIndex}:${params.dataIndex}`) return;
      scheduleHideTooltip();
    },
    globalout: (params) => {
      onEvents?.globalout?.(params);
      hideTooltip();
    },
  }), [clearHideTimer, hideTooltip, onEvents, scheduleHideTooltip]);

  useEffect(() => () => {
    clearHideTimer();
  }, [clearHideTimer]);

  return (
    <EChart
      option={option}
      height={height}
      className={className}
      onChartReady={handleChartReady}
      onEvents={mergedEvents}
    />
  );
}
