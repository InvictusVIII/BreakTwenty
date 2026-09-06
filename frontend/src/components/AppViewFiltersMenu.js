import React, { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { LuListFilter } from 'react-icons/lu';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import ControlChevron from './ControlChevron';

const PANEL_EDGE_GAP = 8;
const PANEL_TOP_GAP = 8;

function getPanelPortalTarget(anchor) {
  if (typeof document === 'undefined') {
    return null;
  }

  return anchor?.closest('.app-main') || document.querySelector('.app-main') || document.body;
}

function AppViewFiltersMenu({
  id,
  ariaLabel,
  rows,
  className = '',
  triggerLabel = 'View & Filters',
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [panelStyle, setPanelStyle] = useState(null);
  const [panelPortalTarget, setPanelPortalTarget] = useState(null);
  const menuRef = useRef(null);
  const triggerRef = useRef(null);
  const panelRef = useRef(null);
  const visibleRows = useMemo(
    () => (rows || []).filter((row) => row && !row.hidden && row.control),
    [rows],
  );
  const closeMenu = useCallback(() => setIsOpen(false), []);
  const updatePanelPosition = useCallback(() => {
    if (typeof window === 'undefined' || typeof document === 'undefined' || !triggerRef.current) {
      return;
    }

    const portalTarget = getPanelPortalTarget(triggerRef.current);
    if (!portalTarget) {
      return;
    }

    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    const triggerRect = triggerRef.current.getBoundingClientRect();
    const portalRect = portalTarget === document.body
      ? { left: 0, right: viewportWidth, top: 0 }
      : portalTarget.getBoundingClientRect();
    const scrollX = portalTarget === document.body
      ? (window.scrollX || document.documentElement.scrollLeft || 0)
      : portalTarget.scrollLeft;
    const scrollY = portalTarget === document.body
      ? (window.scrollY || document.documentElement.scrollTop || 0)
      : portalTarget.scrollTop;
    const maxWidth = Math.max(0, viewportWidth - PANEL_EDGE_GAP * 2);
    const viewportRight = Math.min(triggerRect.right, viewportWidth - PANEL_EDGE_GAP);
    const viewportTop = Math.max(PANEL_EDGE_GAP, triggerRect.bottom + PANEL_TOP_GAP);

    setPanelStyle({
      position: 'absolute',
      top: `${viewportTop - portalRect.top + scrollY}px`,
      left: 'auto',
      right: `${Math.max(0, portalRect.right - viewportRight + scrollX)}px`,
      width: 'max-content',
      maxWidth: `${maxWidth}px`,
      maxHeight: `${Math.max(160, viewportHeight - viewportTop - PANEL_EDGE_GAP)}px`,
      overflow: 'visible',
      transform: isOpen ? 'translateY(0)' : 'translateY(-8px)',
      zIndex: 'var(--app-popover-elevated-layer)',
    });
  }, [isOpen]);

  useDismissibleLayer({
    open: isOpen,
    refs: [menuRef, panelRef],
    onDismiss: closeMenu,
  });

  useLayoutEffect(() => {
    setPanelPortalTarget(getPanelPortalTarget(triggerRef.current));
  }, []);

  useLayoutEffect(() => {
    if (!isOpen) {
      return undefined;
    }

    updatePanelPosition();
    const handlePositionChange = () => updatePanelPosition();
    window.addEventListener('resize', handlePositionChange);

    return () => {
      window.removeEventListener('resize', handlePositionChange);
    };
  }, [isOpen, updatePanelPosition]);

  if (visibleRows.length === 0) {
    return null;
  }

  const panelId = `${id}-panel`;
  const resolvedPanelPortalTarget = panelPortalTarget || (typeof document !== 'undefined' ? document.body : null);
  const panel = (
    <div
      id={panelId}
      ref={panelRef}
      role="dialog"
      aria-label={ariaLabel}
      className={`investments-filter-panel investments-view-filter-panel app-view-filter-panel app-surface-button-scope ${isOpen ? 'is-open' : ''}`.trim()}
      aria-hidden={!isOpen}
      style={panelStyle || undefined}
    >
      {visibleRows.map((row) => (
        <div key={row.key || row.label} className={`investments-view-filter-row app-view-filter-row ${row.className || ''}`.trim()}>
          <span className="investments-view-filter-label app-view-filter-label">{row.label}</span>
          <div className="investments-view-filter-control app-view-filter-control">
            {row.control}
          </div>
        </div>
      ))}
    </div>
  );

  return (
    <div className={`app-view-filter-popover investments-view-filter-popover ${className}`.trim()} ref={menuRef}>
      <button
        type="button"
        ref={triggerRef}
        className={`investments-filter-trigger investments-view-filter-trigger app-view-filter-trigger app-control-root ${isOpen ? 'is-open' : ''}`.trim()}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        aria-controls={panelId}
        onClick={() => setIsOpen((previous) => !previous)}
      >
        <span className="investments-filter-trigger-icon app-view-filter-trigger-icon app-control-icon" aria-hidden="true">
          <LuListFilter />
        </span>
        <span className="investments-view-filter-trigger-label app-view-filter-trigger-label app-control-label">{triggerLabel}</span>
        <span className={`investments-filter-trigger-chevron app-control-chevron ${isOpen ? 'is-open' : ''}`.trim()} aria-hidden="true">
          <ControlChevron />
        </span>
      </button>

      {resolvedPanelPortalTarget ? createPortal(panel, resolvedPanelPortalTarget) : null}
    </div>
  );
}

export default AppViewFiltersMenu;
