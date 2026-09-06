import React, { useRef } from 'react';
import { createPortal } from 'react-dom';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import useBodyScrollLock from '../hooks/useBodyScrollLock';

const VIEWPORT_MARGIN = 8;
const ANCHOR_GAP = 6;

function joinClassNames(...values) {
  return values.filter(Boolean).join(' ');
}

// Shared anchored popover. Portals to <body>, positions itself position:fixed
// just below an anchor rect and clamped into the viewport, and owns dismiss +
// body-scroll-lock (the lock keeps the fixed panel from detaching from its
// anchor when the page scrolls). Consumers supply only content + dimensions:
//   width      — applied to the panel and used for horizontal clamping
//   height     — when set, fixes the panel height (else CSS controls it)
//   clampHeight — height used for vertical clamping (defaults to `height`)
//   placement  — 'bottom' (default), 'top', 'right', or 'left'
//   align      — 'left' lines the panel up with the anchor's left edge,
//                'center' centers it under the anchor, and 'right' aligns
//                the panel's right edge to the anchor's right edge
//   crossAlign — for side placement, 'start' or 'center'
//   offsetY    — final vertical adjustment after viewport clamping
export default function AnchoredPopover({
  isOpen,
  anchorRect = null,
  width,
  height = null,
  clampHeight = height,
  placement = 'bottom',
  align = 'left',
  crossAlign = 'start',
  offsetY = 0,
  onDismiss,
  disableDismiss = false,
  className = '',
  role = 'dialog',
  ariaLabel,
  children,
}) {
  const panelRef = useRef(null);

  // `disableDismiss` lets a popover suspend its own outside-click/Escape
  // dismissal while it hosts a nested layer (e.g. the category picker keeps
  // itself open while its emoji popover is up — a click in that sibling portal
  // would otherwise read as "outside" and close the picker).
  useDismissibleLayer({
    open: isOpen && !disableDismiss,
    ref: panelRef,
    onDismiss,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });
  useBodyScrollLock(isOpen);

  if (!isOpen) return null;

  let desiredTop = 100;
  let desiredLeft = 100;

  if (anchorRect) {
    if (placement === 'right' || placement === 'left') {
      desiredTop = crossAlign === 'center'
        ? anchorRect.top + (anchorRect.height / 2) - ((clampHeight ?? 0) / 2)
        : anchorRect.top;
      desiredLeft = placement === 'left'
        ? anchorRect.left - width - ANCHOR_GAP
        : anchorRect.right + ANCHOR_GAP;
    } else {
      desiredTop = placement === 'top'
        ? anchorRect.top - (clampHeight ?? 0) - ANCHOR_GAP
        : anchorRect.bottom + ANCHOR_GAP;
      if (align === 'right') {
        desiredLeft = anchorRect.right - width;
      } else if (align === 'center') {
        desiredLeft = anchorRect.left + (anchorRect.width / 2) - (width / 2);
      } else {
        desiredLeft = anchorRect.left;
      }
    }
  }
  const maxTop = window.innerHeight - (clampHeight ?? 0) - VIEWPORT_MARGIN;
  const maxLeft = window.innerWidth - width - VIEWPORT_MARGIN;
  const clampedTop = Math.max(VIEWPORT_MARGIN, Math.min(desiredTop, maxTop));
  const top = Math.max(VIEWPORT_MARGIN, Math.min(clampedTop + offsetY, maxTop));
  const left = Math.max(VIEWPORT_MARGIN, Math.min(desiredLeft, maxLeft));

  const style = { position: 'fixed', top, left, width };
  if (height != null) style.height = height;

  return createPortal(
    <div
      ref={panelRef}
      className={joinClassNames('app-anchored-popover', className)}
      style={style}
      role={role}
      aria-label={ariaLabel}
    >
      {children}
    </div>,
    document.body,
  );
}
