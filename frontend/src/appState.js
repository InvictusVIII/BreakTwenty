import { createContext, useContext, useEffect, useLayoutEffect, useState } from 'react';
import { BREAKTWENTY_DEFAULT_THEME_MODE } from './theme/applyTheme';
import { persistentStorage } from './utils/persistentStorage';

export const RightTrayContext = createContext({
  register: () => () => {},
  activeKey: null,
});

export const CurrencyContext = createContext({
  primaryCurrency: 'CAD',
  fxRates: {},
  setPrimaryCurrency: async () => {},
  convert: (amount) => amount,
  refreshRates: () => {},
});

export const ThemeContext = createContext({
  mode: BREAKTWENTY_DEFAULT_THEME_MODE,
  colors: Object.freeze({}),
  chartColors: Object.freeze({}),
});

export function useCurrency() {
  return useContext(CurrencyContext);
}

export function useTheme() {
  return useContext(ThemeContext);
}

export function useRightTrayReservation(key, isOpen) {
  const { register } = useContext(RightTrayContext);
  // useLayoutEffect (not useEffect) so the register call — and the resulting
  // .has-right-tray-open class on .app-shell — lands in the SAME paint as
  // the consumer's render commit. Without this, the page paints once with
  // the class missing and then again with it applied, which causes any
  // viewport-based measurement (e.g. tray top) taken in a sibling
  // useLayoutEffect to read pre-class layout values.
  useLayoutEffect(() => {
    if (!isOpen || !key) return undefined;
    return register(key);
  }, [register, key, isOpen]);
}

// Lets a tray's consumer gate its own measurement effect on the shell class
// having actually been applied. Tray internals that need to read post-class
// layout (e.g. the chart section's viewport top after the column has reflowed
// to make room for the tray) should depend on this in their useLayoutEffect.
export function useActiveRightTrayKey() {
  const { activeKey } = useContext(RightTrayContext);
  return activeKey;
}

export function useRightTrayOpenState(key, isOpen) {
  useRightTrayReservation(key, isOpen);
  const activeKey = useActiveRightTrayKey();
  const reservationActive = activeKey === key;
  const [animatedOpen, setAnimatedOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let frameId = null;

    if (!isOpen || !reservationActive) {
      Promise.resolve().then(() => {
        if (!cancelled) setAnimatedOpen(false);
      });
      return undefined;
    }

    frameId = window.requestAnimationFrame(() => {
      if (!cancelled) setAnimatedOpen(true);
    });

    return () => {
      cancelled = true;
      if (frameId !== null) window.cancelAnimationFrame(frameId);
    };
  }, [isOpen, reservationActive]);

  return {
    activeKey,
    reservationActive,
    animatedOpen: Boolean(isOpen && reservationActive && animatedOpen),
  };
}

const PANEL_COLLAPSED_STORAGE_PREFIX = 'breaktwenty_panel_collapsed:';

function readPanelCollapsed(storageKey, defaultCollapsed) {
  if (typeof window === 'undefined') return Boolean(defaultCollapsed);

  try {
    const stored = persistentStorage.getItem(storageKey);
    if (stored === 'true') return true;
    if (stored === 'false') return false;
  } catch {
    return Boolean(defaultCollapsed);
  }

  return Boolean(defaultCollapsed);
}

export function usePersistentPanelCollapsed(panelKey, defaultCollapsed = false) {
  const storageKey = `${PANEL_COLLAPSED_STORAGE_PREFIX}${panelKey}`;
  const [collapsed, setCollapsed] = useState(() => readPanelCollapsed(storageKey, defaultCollapsed));

  useEffect(() => {
    if (typeof window === 'undefined') return;
    try {
      persistentStorage.setItem(storageKey, String(Boolean(collapsed)));
    } catch {
      // Ignore unavailable local storage; the panel still works for the session.
    }
  }, [storageKey, collapsed]);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const handleStorage = (event) => {
      if (event.key !== storageKey) return;
      if (event.newValue === 'true') setCollapsed(true);
      if (event.newValue === 'false') setCollapsed(false);
    };
    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, [storageKey]);

  return [collapsed, setCollapsed];
}
