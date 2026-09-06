import {
  BREAKTWENTY_DEFAULT_THEME_MODE as DEFAULT_THEME_MODE,
  applyBreakTwentyTheme as applyBreakTwentyThemeFromModule,
  getBreakTwentyColorTheme as getBreakTwentyColorThemeFromModule,
  normalizeBreakTwentyThemeMode as normalizeBreakTwentyThemeModeFromModule,
} from './colors';
import { applyBreakTwentySurfaces as applyBreakTwentySurfacesFromModule } from './surfaces';
import {
  applyBreakTwentyTypography as applyBreakTwentyTypographyFromModule,
  breaktwentyChartText as breaktwentyChartTextFromModule,
  breaktwentyTypography as breaktwentyTypographyFromModule,
} from './typography';
import { breaktwentyRuntimeCssVariables as breaktwentyRuntimeCssVariablesFromModule } from './runtimeTokens';

export const BREAKTWENTY_DEFAULT_THEME_MODE = DEFAULT_THEME_MODE;
export const BREAKTWENTY_THEME_HMR_EVENT = 'breaktwenty:theme-hmr';

let activeColorThemeApi = {
  applyBreakTwentyTheme: applyBreakTwentyThemeFromModule,
  getBreakTwentyColorTheme: getBreakTwentyColorThemeFromModule,
  normalizeBreakTwentyThemeMode: normalizeBreakTwentyThemeModeFromModule,
};
let activeSurfaceThemeApi = {
  applyBreakTwentySurfaces: applyBreakTwentySurfacesFromModule,
};
let activeTypographyThemeApi = {
  applyBreakTwentyTypography: applyBreakTwentyTypographyFromModule,
  breaktwentyChartText: breaktwentyChartTextFromModule,
  breaktwentyTypography: breaktwentyTypographyFromModule,
};
let activeRuntimeCssVariables = breaktwentyRuntimeCssVariablesFromModule;
let appliedRuntimeCssVariableNames = new Set();

function defaultThemeTarget() {
  return typeof document !== 'undefined' ? document.documentElement : null;
}

export function normalizeBreakTwentyThemeMode(mode) {
  return activeColorThemeApi.normalizeBreakTwentyThemeMode(mode);
}

export function getAppliedBreakTwentyColorTheme(mode = BREAKTWENTY_DEFAULT_THEME_MODE) {
  return activeColorThemeApi.getBreakTwentyColorTheme(normalizeBreakTwentyThemeMode(mode));
}

export function getAppliedBreakTwentyChartColors(mode = BREAKTWENTY_DEFAULT_THEME_MODE) {
  return getAppliedBreakTwentyColorTheme(mode).chartColors;
}

export function getAppliedBreakTwentyTypography() {
  return activeTypographyThemeApi.breaktwentyTypography;
}

export function getAppliedBreakTwentyChartText() {
  return activeTypographyThemeApi.breaktwentyChartText;
}

function runtimeCssVariableNames(runtimeCssVariables) {
  return new Set(
    Object.values(runtimeCssVariables).flatMap((variables) => Object.keys(variables || {}))
  );
}

function applyRuntimeCssVariables(mode, target) {
  const nextRuntimeCssVariableNames = runtimeCssVariableNames(activeRuntimeCssVariables);
  new Set([...appliedRuntimeCssVariableNames, ...nextRuntimeCssVariableNames]).forEach((name) => {
    target.style.removeProperty(name);
  });
  Object.entries(activeRuntimeCssVariables[mode] || activeRuntimeCssVariables.dark).forEach(([name, value]) => {
    target.style.setProperty(name, value);
  });
  appliedRuntimeCssVariableNames = nextRuntimeCssVariableNames;
}

function applyDocumentThemeColor(mode, target) {
  const ownerDocument = target?.ownerDocument || (typeof document !== 'undefined' ? document : null);
  if (!ownerDocument || target !== ownerDocument.documentElement) return;
  const themeColorMeta = ownerDocument.querySelector('meta[name="theme-color"]');
  if (!themeColorMeta) return;
  themeColorMeta.setAttribute('content', getAppliedBreakTwentyColorTheme(mode).palette.appBackground);
}

export function applyBreakTwentyThemeMode(mode = BREAKTWENTY_DEFAULT_THEME_MODE, target = defaultThemeTarget()) {
  if (!target?.style) return BREAKTWENTY_DEFAULT_THEME_MODE;

  const normalizedMode = normalizeBreakTwentyThemeMode(mode);
  activeColorThemeApi.applyBreakTwentyTheme(normalizedMode, target);
  activeSurfaceThemeApi.applyBreakTwentySurfaces(normalizedMode, target);
  activeTypographyThemeApi.applyBreakTwentyTypography(target);
  applyRuntimeCssVariables(normalizedMode, target);
  target.style.colorScheme = normalizedMode === 'light' ? 'light' : 'dark';
  target.dataset.breaktwentyTheme = normalizedMode;
  applyDocumentThemeColor(normalizedMode, target);
  return normalizedMode;
}

function dispatchThemeHmr(mode) {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(BREAKTWENTY_THEME_HMR_EVENT, { detail: { mode } }));
}

if (import.meta.hot) {
  import.meta.hot.accept(['./colors', './surfaces', './typography', './runtimeTokens'], ([
    nextColors,
    nextSurfaces,
    nextTypography,
    nextRuntimeTokens,
  ]) => {
    if (nextColors) {
      activeColorThemeApi = {
        applyBreakTwentyTheme: nextColors.applyBreakTwentyTheme,
        getBreakTwentyColorTheme: nextColors.getBreakTwentyColorTheme,
        normalizeBreakTwentyThemeMode: nextColors.normalizeBreakTwentyThemeMode,
      };
    }
    if (nextSurfaces) {
      activeSurfaceThemeApi = {
        applyBreakTwentySurfaces: nextSurfaces.applyBreakTwentySurfaces,
      };
    }
    if (nextTypography) {
      activeTypographyThemeApi = {
        applyBreakTwentyTypography: nextTypography.applyBreakTwentyTypography,
        breaktwentyChartText: nextTypography.breaktwentyChartText,
        breaktwentyTypography: nextTypography.breaktwentyTypography,
      };
    }
    if (nextRuntimeTokens) {
      activeRuntimeCssVariables = nextRuntimeTokens.breaktwentyRuntimeCssVariables;
    }

    const target = defaultThemeTarget();
    if (!target?.style) return;

    const activeMode = normalizeBreakTwentyThemeMode(target.dataset.breaktwentyTheme || BREAKTWENTY_DEFAULT_THEME_MODE);

    activeColorThemeApi.applyBreakTwentyTheme(activeMode, target);
    activeSurfaceThemeApi.applyBreakTwentySurfaces(activeMode, target);
    activeTypographyThemeApi.applyBreakTwentyTypography(target);
    applyRuntimeCssVariables(activeMode, target);
    target.style.colorScheme = activeMode === 'light' ? 'light' : 'dark';
    target.dataset.breaktwentyTheme = activeMode;
    applyDocumentThemeColor(activeMode, target);
    dispatchThemeHmr(activeMode);
  });
}
