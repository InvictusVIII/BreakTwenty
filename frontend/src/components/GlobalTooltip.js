import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

const GAP = 8; // space between the trigger and the tooltip
const SIDE_GAP = GAP * 2;
const EDGE = 8; // minimum gap kept from the viewport edge

/**
 * One app-wide tooltip. Listens (capture phase) for hover or focus on any
 * element carrying a `data-tooltip` attribute, renders a single bubble into a
 * body-level portal, and positions it with JS so it ALWAYS stays inside the
 * viewport and on top of everything. This replaces the old pure-CSS
 * `[data-tooltip]::after` tooltip, which could run off-screen and get trapped
 * by an ancestor's stacking context. Triggers opt in exactly as before — no
 * markup changes, just the existing `data-tooltip` attribute.
 */
export default function GlobalTooltip() {
  const [tip, setTip] = useState(null); // { text, rect, placement }
  const [coords, setCoords] = useState(null); // { left, top } once measured
  const bubbleRef = useRef(null);
  const hoveredRef = useRef(null);
  const focusedRef = useRef(null);

  useEffect(() => {
    const clearActive = () => {
      setTip(null);
      setCoords(null);
    };
    const hide = () => {
      hoveredRef.current = null;
      focusedRef.current = null;
      clearActive();
    };
    const show = (el) => {
      const text = el?.getAttribute('data-tooltip');
      if (!text) {
        clearActive();
        return;
      }
      setCoords(null); // measure first, then place (see layout effect)
      setTip({ text, rect: el.getBoundingClientRect(), placement: el.getAttribute('data-tooltip-placement') });
    };
    const restoreActive = () => {
      if (hoveredRef.current && !hoveredRef.current.isConnected) hoveredRef.current = null;
      if (focusedRef.current && !focusedRef.current.isConnected) focusedRef.current = null;
      const next = hoveredRef.current || focusedRef.current;
      if (next) show(next);
      else clearActive();
    };
    const onOver = (e) => {
      const el = e.target.closest && e.target.closest('[data-tooltip]');
      if (!el || el === hoveredRef.current) return;
      hoveredRef.current = el;
      show(el);
    };
    const onOut = (e) => {
      const el = e.target.closest && e.target.closest('[data-tooltip]');
      if (!el || el !== hoveredRef.current) return;
      // ignore moves that stay within the active trigger's own subtree
      if (e.relatedTarget && el.contains(e.relatedTarget)) return;
      hoveredRef.current = null;
      restoreActive();
    };
    const onFocusIn = (e) => {
      const el = e.target.closest && e.target.closest('[data-tooltip]');
      if (!el || el === focusedRef.current) return;
      if (el.hasAttribute('data-tooltip-hover-only')) return;
      focusedRef.current = el;
      if (!hoveredRef.current) show(el);
    };
    const onFocusOut = (e) => {
      const el = e.target.closest && e.target.closest('[data-tooltip]');
      if (!el || el !== focusedRef.current) return;
      // ignore focus changes that stay within the active trigger's subtree
      if (e.relatedTarget && el.contains(e.relatedTarget)) return;
      focusedRef.current = null;
      restoreActive();
    };
    document.addEventListener('pointerover', onOver, true);
    document.addEventListener('pointerout', onOut, true);
    document.addEventListener('focusin', onFocusIn, true);
    document.addEventListener('focusout', onFocusOut, true);
    // Hide on press too: a trigger that unmounts/navigates on click (e.g. opening a
    // sub-view) never fires pointerout, so without this the bubble gets stranded.
    document.addEventListener('pointerdown', hide, true);
    document.addEventListener('click', hide, true);
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide, true);
    document.addEventListener('keydown', hide, true);
    return () => {
      document.removeEventListener('pointerover', onOver, true);
      document.removeEventListener('pointerout', onOut, true);
      document.removeEventListener('focusin', onFocusIn, true);
      document.removeEventListener('focusout', onFocusOut, true);
      document.removeEventListener('pointerdown', hide, true);
      document.removeEventListener('click', hide, true);
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('resize', hide, true);
      document.removeEventListener('keydown', hide, true);
    };
  }, []);

  useLayoutEffect(() => {
    if (!tip || !bubbleRef.current) return;
    const b = bubbleRef.current.getBoundingClientRect();
    const r = tip.rect;
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    let left;
    let top;
    if (tip.placement === 'right') {
      left = r.right + SIDE_GAP;
      top = r.top + r.height / 2 - b.height / 2;
      if (left + b.width > vw - EDGE) {
        left = r.left - b.width - SIDE_GAP;
      }
    } else {
      // Default: centered above the trigger; flip below if it would clip the top.
      left = r.left + r.width / 2 - b.width / 2;
      top = r.top - b.height - GAP;
      if (top < EDGE) top = r.bottom + GAP;
    }
    // Clamp inside the viewport on both axes so it never leaves the window.
    left = Math.min(Math.max(left, EDGE), vw - b.width - EDGE);
    top = Math.min(Math.max(top, EDGE), vh - b.height - EDGE);
    setCoords({ left, top });
  }, [tip]);

  if (!tip) return null;
  return createPortal(
    <div
      ref={bubbleRef}
      className="app-tooltip"
      role="tooltip"
      style={{
        left: coords ? coords.left : 0,
        top: coords ? coords.top : 0,
        visibility: coords ? 'visible' : 'hidden',
      }}
    >
      {tip.text}
    </div>,
    document.body,
  );
}
