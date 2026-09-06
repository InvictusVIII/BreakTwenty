import { useEffect } from 'react';

export const APP_NON_DISMISS_INTERACTION_SELECTOR = '[data-app-non-dismiss-interaction]';

const SCROLLABLE_OVERFLOW_VALUES = new Set(['auto', 'scroll']);

function getPointerClientPoint(event) {
  if (typeof event.clientX === 'number' && typeof event.clientY === 'number') {
    return { clientX: event.clientX, clientY: event.clientY };
  }
  const touch = event.touches?.[0] || event.changedTouches?.[0];
  if (touch && typeof touch.clientX === 'number' && typeof touch.clientY === 'number') {
    return { clientX: touch.clientX, clientY: touch.clientY };
  }
  return null;
}

function isNativeScrollbarInteraction(event) {
  if (typeof window === 'undefined') {
    return false;
  }

  const target = event.target;
  if (target?.closest?.('.accounts-table-horizontal-scrollbar, .transactions-table-horizontal-scrollbar, .recent-transactions-horizontal-scrollbar')) {
    return true;
  }

  const point = getPointerClientPoint(event);
  if (!point) {
    return false;
  }

  let node = target?.nodeType === 3 ? target.parentElement : target;
  while (node && node !== document.documentElement) {
    if (typeof node.getBoundingClientRect !== 'function') {
      break;
    }

    const style = window.getComputedStyle(node);
    const scrollsX = node.scrollWidth > node.clientWidth + 1 && SCROLLABLE_OVERFLOW_VALUES.has(style.overflowX);
    const scrollsY = node.scrollHeight > node.clientHeight + 1 && SCROLLABLE_OVERFLOW_VALUES.has(style.overflowY);
    if (scrollsX || scrollsY) {
      const rect = node.getBoundingClientRect();
      const horizontalScrollbarSize = Math.max(0, (node.offsetHeight || 0) - node.clientHeight);
      const verticalScrollbarSize = Math.max(0, (node.offsetWidth || 0) - node.clientWidth);
      const onHorizontalScrollbar = scrollsX
        && horizontalScrollbarSize > 0
        && point.clientY >= rect.bottom - horizontalScrollbarSize
        && point.clientY <= rect.bottom
        && point.clientX >= rect.left
        && point.clientX <= rect.right;
      const onVerticalScrollbar = scrollsY
        && verticalScrollbarSize > 0
        && point.clientX >= rect.right - verticalScrollbarSize
        && point.clientX <= rect.right
        && point.clientY >= rect.top
        && point.clientY <= rect.bottom;

      if (onHorizontalScrollbar || onVerticalScrollbar) {
        return true;
      }
    }

    node = node.parentElement;
  }

  return false;
}

export default function useDismissibleLayer({
  open,
  ref,
  refs,
  onDismiss,
  pointerEvent = 'pointerdown',
  includeTouch = false,
  capture = false,
  ignoreSelector = null,
  ignoreAppChrome = false,
}) {
  useEffect(() => {
    if (!open) {
      return undefined;
    }

    const layerRefs = refs || (ref ? [ref] : []);
    const containsTarget = (target) => layerRefs.some((layerRef) => layerRef.current?.contains(target));
    // Optional escape hatch: pointerdowns on elements matching `ignoreSelector`
    // (e.g. the controls that themselves open/switch this layer) are treated as
    // "inside" so they don't dismiss — letting the click re-target instead.
    const matchesIgnore = (target) => {
      if (typeof target?.closest !== 'function') {
        return false;
      }
      if (ignoreAppChrome && target.closest('.floating-nav-rail') != null) {
        return true;
      }
      if (target.closest(APP_NON_DISMISS_INTERACTION_SELECTOR) != null) {
        return true;
      }
      return ignoreSelector != null && target.closest(ignoreSelector) != null;
    };

    const handlePointerDown = (event) => {
      if (!containsTarget(event.target) && !matchesIgnore(event.target) && !isNativeScrollbarInteraction(event)) {
        onDismiss(event);
      }
    };

    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        onDismiss(event);
      }
    };

    document.addEventListener(pointerEvent, handlePointerDown, capture);
    if (includeTouch) {
      document.addEventListener('touchstart', handlePointerDown, capture);
    }
    document.addEventListener('keydown', handleKeyDown, capture);

    return () => {
      document.removeEventListener(pointerEvent, handlePointerDown, capture);
      if (includeTouch) {
        document.removeEventListener('touchstart', handlePointerDown, capture);
      }
      document.removeEventListener('keydown', handleKeyDown, capture);
    };
  }, [capture, ignoreAppChrome, includeTouch, onDismiss, open, pointerEvent, ref, refs, ignoreSelector]);
}
