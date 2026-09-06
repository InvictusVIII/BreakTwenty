import React, { useLayoutEffect, useRef, useState } from 'react';
import './HorizontalScrollProxy.css';

const SCROLL_TOLERANCE = 0.5;

function joinClassNames(...values) {
  return values.filter(Boolean).join(' ');
}

function getElementWidth(element) {
  return Math.max(element.scrollWidth, element.getBoundingClientRect().width);
}

function getDefaultContentWidth({ target, contentElements }) {
  return Math.max(target.clientWidth, ...contentElements.map(getElementWidth));
}

function useHorizontalScrollProxy(options) {
  const controllerRef = useRef(null);
  const visibleRef = useRef(false);
  const targetContentWidthRef = useRef(0);
  const [visible, setVisible] = useState(false);
  const [contentWidth, setContentWidth] = useState(0);

  useLayoutEffect(() => {
    const controller = controllerRef.current;
    const target = controller ? options.resolveTarget(controller) : null;
    if (!controller || !target) return undefined;

    let syncing = false;
    let syncFrame = null;
    let lastTargetWidth = null;

    const getContentElements = () => (
      options.getContentElements?.({ controller, target }) || []
    ).filter(Boolean);
    const updateTargetScrollMetric = () => {
      if (options.targetScrollProperty) {
        target.style.setProperty(options.targetScrollProperty, `${target.scrollLeft}px`);
      }
    };
    const getTargetMaxScrollLeft = () => Math.max(
      0,
      targetContentWidthRef.current - target.clientWidth,
    );
    const getControllerMaxScrollLeft = () => Math.max(
      0,
      controller.scrollWidth - controller.clientWidth,
    );
    const getControllerScrollLeft = () => {
      if (options.mapping !== 'proportional') return target.scrollLeft;
      const targetMaximum = getTargetMaxScrollLeft();
      const controllerMaximum = getControllerMaxScrollLeft();
      return targetMaximum > 0 && controllerMaximum > 0
        ? (target.scrollLeft / targetMaximum) * controllerMaximum
        : 0;
    };
    const getTargetScrollLeft = () => {
      if (options.mapping !== 'proportional') return controller.scrollLeft;
      const targetMaximum = getTargetMaxScrollLeft();
      const controllerMaximum = getControllerMaxScrollLeft();
      return targetMaximum > 0 && controllerMaximum > 0
        ? (controller.scrollLeft / controllerMaximum) * targetMaximum
        : 0;
    };
    const syncControllerFromTarget = () => {
      const nextScrollLeft = getControllerScrollLeft();
      if (Math.abs(controller.scrollLeft - nextScrollLeft) > SCROLL_TOLERANCE) {
        controller.scrollLeft = nextScrollLeft;
      }
    };
    const resetScrollLeft = () => {
      if (target.scrollLeft !== 0) target.scrollLeft = 0;
      if (controller.scrollLeft !== 0) controller.scrollLeft = 0;
    };
    const clampTargetScrollLeft = (nextVisible) => {
      const maximumScrollLeft = getTargetMaxScrollLeft();
      const nextScrollLeft = nextVisible ? Math.min(target.scrollLeft, maximumScrollLeft) : 0;
      const clampTolerance = options.clampTolerance ?? SCROLL_TOLERANCE;
      if (Math.abs(target.scrollLeft - nextScrollLeft) > clampTolerance) {
        target.scrollLeft = nextScrollLeft;
      }
      syncControllerFromTarget();
    };
    const measure = () => {
      const targetWidth = target.clientWidth;
      if (
        options.resetOnTargetWidthChange
        && (lastTargetWidth === null || Math.abs(targetWidth - lastTargetWidth) > SCROLL_TOLERANCE)
      ) {
        resetScrollLeft();
      }
      lastTargetWidth = targetWidth;

      const visibilityBuffer = options.getVisibilityBuffer?.({ controller, target }) || 0;
      const minimumControllerRange = options.getMinimumControllerRange?.({ controller, target }) || 0;
      if (options.controllerMeasurementProperty) {
        controller.style.setProperty(options.controllerMeasurementProperty, '0px');
      }
      const contentElements = getContentElements();
      const measuredContentWidth = Math.max(
        targetWidth,
        (options.getContentWidth || getDefaultContentWidth)({
          controller,
          target,
          contentElements,
        }),
      );
      const nextVisible = measuredContentWidth > targetWidth + visibilityBuffer + 1;
      const nextContentWidth = nextVisible
        ? Math.max(measuredContentWidth, targetWidth + minimumControllerRange)
        : Math.max(measuredContentWidth, targetWidth);
      targetContentWidthRef.current = measuredContentWidth;
      if (options.controllerMeasurementProperty) {
        controller.style.setProperty(
          options.controllerMeasurementProperty,
          `${nextContentWidth}px`,
        );
      }
      const contentWidthTolerance = options.contentWidthTolerance ?? SCROLL_TOLERANCE;
      setContentWidth((current) => (
        Math.abs(current - nextContentWidth) > contentWidthTolerance ? nextContentWidth : current
      ));
      clampTargetScrollLeft(nextVisible);
      if (options.targetViewportProperty) {
        target.style.setProperty(options.targetViewportProperty, `${targetWidth}px`);
      }
      updateTargetScrollMetric();
      syncControllerFromTarget();
      if (visibleRef.current !== nextVisible) {
        visibleRef.current = nextVisible;
        setVisible(nextVisible);
      }
    };
    const scheduleSync = (afterSync) => {
      if (syncing) return;
      if (syncFrame !== null) window.cancelAnimationFrame(syncFrame);
      syncFrame = window.requestAnimationFrame(() => {
        syncFrame = null;
        syncing = true;
        afterSync();
        syncing = false;
      });
    };
    const syncTarget = () => {
      if (options.deferTargetScrollSync) {
        scheduleSync(() => clampTargetScrollLeft(visibleRef.current));
        return;
      }
      clampTargetScrollLeft(visibleRef.current);
      updateTargetScrollMetric();
      scheduleSync(syncControllerFromTarget);
    };
    const syncController = () => {
      scheduleSync(() => {
        const maximumScrollLeft = getTargetMaxScrollLeft();
        const nextScrollLeft = visibleRef.current
          ? Math.min(getTargetScrollLeft(), maximumScrollLeft)
          : 0;
        if (Math.abs(target.scrollLeft - nextScrollLeft) > SCROLL_TOLERANCE) {
          target.scrollLeft = nextScrollLeft;
        }
        updateTargetScrollMetric();
        syncControllerFromTarget();
      });
    };

    measure();
    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(measure);
    const observedElements = options.getObservedElements?.({
      controller,
      target,
      contentElements: getContentElements(),
    }) || [target, ...getContentElements()];
    [...new Set(observedElements.filter(Boolean))].forEach((element) => {
      resizeObserver?.observe(element);
    });
    target.addEventListener('scroll', syncTarget, { passive: true });
    controller.addEventListener('scroll', syncController, { passive: true });
    window.addEventListener('resize', measure, { passive: true });
    window.visualViewport?.addEventListener('resize', measure, { passive: true });

    return () => {
      if (syncFrame !== null) window.cancelAnimationFrame(syncFrame);
      resizeObserver?.disconnect();
      if (options.targetViewportProperty) {
        target.style.removeProperty(options.targetViewportProperty);
      }
      if (options.targetScrollProperty) {
        target.style.removeProperty(options.targetScrollProperty);
      }
      if (options.controllerMeasurementProperty) {
        controller.style.removeProperty(options.controllerMeasurementProperty);
      }
      target.removeEventListener('scroll', syncTarget);
      controller.removeEventListener('scroll', syncController);
      window.removeEventListener('resize', measure);
      window.visualViewport?.removeEventListener('resize', measure);
    };
  }, [options]);

  return { contentWidth, controllerRef, visible };
}

export default function HorizontalScrollProxy({ options, className = '' }) {
  const { contentWidth, controllerRef, visible } = useHorizontalScrollProxy(options);
  const style = options.contentWidthProperty
    ? { [options.contentWidthProperty]: `${contentWidth}px` }
    : undefined;

  return (
    <div
      ref={controllerRef}
      className={joinClassNames(options.className, visible ? 'is-visible' : '', className)}
      style={style}
      aria-hidden="true"
    >
      <div className={options.innerClassName} />
    </div>
  );
}
