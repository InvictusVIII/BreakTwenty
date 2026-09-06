// SINGLE SOURCE OF TRUTH for all color tokens; the private development workspace owns the theme specification.
// scripts/generate-theme-css.js compiles breaktwentyCssVariables (+ the surface and typography maps)
// into src/theme/tokens.generated.css for first paint; applyBreakTwentyTheme() installs
// the same map at runtime. Keep this file dependency-free (no imports) — the
// generator evaluates it standalone.

export const BREAKTWENTY_DEFAULT_THEME_MODE = 'dark';

const breaktwentyPalettes = Object.freeze({
  dark: Object.freeze({
    appBackground: '#1f1f1e',
    panelSurface: '#222221',
    elevatedSurface: '#242322',
    hoverSurface: '#272624',
    border: '#4D4435',
    textPrimary: '#EDE7DD',
    textSecondary: '#e8bc92',
    textTertiary: '#eb8d35',
    textFaint: '#8E877B',
    brandWordmarkBreak: '#f2dfbd',
    brandWordmarkTwenty: '#d97812',
    accentPrimary: '#eb8d35',
    accentSecondary: '#e8bc92',
    activeBorder: '#eb8d35',
    activeText: '#eb8d35',
    activeInset: '#eb8d35',
    dataHighlightBorder: 'activeBorder',
    dataHighlightText: 'activeText',
    dataHighlightInset: 'activeInset',
    chartHighlightStrokeWidth: 3,
    positive: '#00c850',
    negative: '#d4343f',
    warning: '#FFB000',
    networkWarning: '#f97316',
    retryWarning: '#ff8a00',
    info: '#7FA7D9',
    statusManual: '#a78bfa',
    accountTypeBanking: '#4d89c9',
    accountTypeSpeculative: '#8e59c0',
  }),
  light: Object.freeze({
    appBackground: '#f6f5f3',
    panelSurface: '#fffbf5',
    elevatedSurface: '#f6f0e5',
    hoverSurface: '#fff5e6',
    border: '#723400',
    textPrimary: '#242221',
    textSecondary: '#723400',
    textTertiary: '#a5560b',
    textFaint: '#514b45',
    brandWordmarkBreak: '#5c350a',
    brandWordmarkTwenty: '#a5560b',
    accentPrimary: '#a5560b',
    accentSecondary: '#723400',
    activeBorder: '#723400',
    activeText: '#723400',
    activeInset: '#723400',
    dataHighlightBorder: 'accentSecondary',
    dataHighlightText: 'textTertiary',
    dataHighlightInset: 'accentSecondary',
    chartHighlightStrokeWidth: 4,
    positive: '#006327',
    negative: '#9a000d',
    warning: '#a5560b',
    networkWarning: '#c45a11',
    retryWarning: '#c96500',
    info: '#3c6f9d',
    statusManual: '#543e93',
    accountTypeBanking: '#2f6fa8',
    accountTypeSpeculative: '#744aa6',
  }),
});

function hexToRgbTriplet(hex) {
  const normalized = hex.replace('#', '');
  if (!/^[0-9a-fA-F]{6}$/.test(normalized)) {
    throw new Error(`Invalid BreakTwenty theme hex color: ${hex}`);
  }
  const value = Number.parseInt(normalized, 16);
  return `${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}`;
}

function resolvePaletteColor(palette, value) {
  if (typeof value === 'string' && value.startsWith('#')) {
    return value;
  }
  const resolved = palette[value];
  if (typeof resolved === 'string' && resolved.startsWith('#')) {
    return resolved;
  }
  throw new Error(`Invalid BreakTwenty theme color reference: ${value}`);
}

function buildBreakTwentyRgb(palette) {
  return Object.freeze({
    bg: hexToRgbTriplet(palette.appBackground),
    surface: hexToRgbTriplet(palette.panelSurface),
    surfaceElevated: hexToRgbTriplet(palette.elevatedSurface),
    hoverSurface: hexToRgbTriplet(palette.hoverSurface),
    border: hexToRgbTriplet(palette.border),
    textPrimary: hexToRgbTriplet(palette.textPrimary),
    textSecondary: hexToRgbTriplet(palette.textSecondary),
    textTertiary: hexToRgbTriplet(palette.textTertiary),
    textFaint: hexToRgbTriplet(palette.textFaint),
    brandWordmarkBreak: hexToRgbTriplet(palette.brandWordmarkBreak),
    brandWordmarkTwenty: hexToRgbTriplet(palette.brandWordmarkTwenty),
    accentPrimary: hexToRgbTriplet(palette.accentPrimary),
    accentSecondary: hexToRgbTriplet(palette.accentSecondary),
    activeBorder: hexToRgbTriplet(palette.activeBorder),
    activeText: hexToRgbTriplet(palette.activeText),
    activeInset: hexToRgbTriplet(palette.activeInset),
    dataHighlightBorder: hexToRgbTriplet(resolvePaletteColor(palette, palette.dataHighlightBorder)),
    dataHighlightText: hexToRgbTriplet(resolvePaletteColor(palette, palette.dataHighlightText)),
    dataHighlightInset: hexToRgbTriplet(resolvePaletteColor(palette, palette.dataHighlightInset)),
    positive: hexToRgbTriplet(palette.positive),
    negative: hexToRgbTriplet(palette.negative),
    warning: hexToRgbTriplet(palette.warning),
    networkWarning: hexToRgbTriplet(palette.networkWarning),
    info: hexToRgbTriplet(palette.info),
    // True white, for hairline borders / inset highlights via rgba(var(--rgb-white), alpha).
    // Distinct from --rgb-text-primary (warm off-white).
    white: '255, 255, 255',
  });
}

function buildBreakTwentyColors(palette, rgb) {
  return Object.freeze({
    bg: palette.appBackground,
    surface: palette.panelSurface,
    surfaceElevated: palette.elevatedSurface,
    surfaceHover: palette.hoverSurface,
    border: palette.border,
    textPrimary: palette.textPrimary,
    textSecondary: palette.textSecondary,
    textTertiary: palette.textTertiary,
    textFaint: palette.textFaint,
    brandWordmarkBreak: palette.brandWordmarkBreak,
    brandWordmarkTwenty: palette.brandWordmarkTwenty,
    accentPrimary: palette.accentPrimary,
    accentSecondary: palette.accentSecondary,
    activeBorder: palette.activeBorder,
    activeText: palette.activeText,
    activeInset: palette.activeInset,
    dataHighlightBorder: resolvePaletteColor(palette, palette.dataHighlightBorder),
    dataHighlightText: resolvePaletteColor(palette, palette.dataHighlightText),
    dataHighlightInset: resolvePaletteColor(palette, palette.dataHighlightInset),
    positive: palette.positive,
    negative: palette.negative,
    warning: palette.warning,
    networkWarning: palette.networkWarning,
    retryWarning: palette.retryWarning,
    info: palette.info,
    focusRing: `rgba(${rgb.accentPrimary}, 0.28)`,
  });
}

const chartPalette = Object.freeze({
  green: '#10b981',
  blue: '#3b82f6',
  amber: '#f59e0b',
  red: '#ef4444',
  purple: '#8b5cf6',
  cyan: '#06b6d4',
  pink: '#ec4899',
  lime: '#84cc16',
  orange: '#f97316',
  indigo: '#4f46e5',
  teal: '#14b8a6',
  brightGreen: '#22c55e',
  dividendGreen: 'rgb(33, 134, 62)',
  withholdingRed: 'rgb(171, 0, 0)',
});

const selectedDataBorderWidth = 3;

function buildBreakTwentyChartColors(colors, rgb, palette) {
  const chartHighlightStrokeWidth = palette.chartHighlightStrokeWidth ?? selectedDataBorderWidth;
  return Object.freeze({
    netWorth: colors.accentPrimary,
    // Lead asset-allocation slices use the primary accent so dashboard and
    // account-tray charts share the main brand chart color.
    assets: Object.freeze([
      colors.accentPrimary,
      chartPalette.blue,
      chartPalette.amber,
      chartPalette.purple,
      chartPalette.pink,
      chartPalette.teal,
      chartPalette.orange,
    ]),
    liabilities: Object.freeze([
      chartPalette.red,
      chartPalette.blue,
      chartPalette.amber,
      chartPalette.purple,
      chartPalette.pink,
      chartPalette.teal,
      chartPalette.orange,
    ]),
    liabilityTypes: Object.freeze([
      chartPalette.red,
      chartPalette.blue,
      chartPalette.amber,
      chartPalette.purple,
      chartPalette.pink,
      chartPalette.teal,
      chartPalette.orange,
    ]),
    holdingsBars: Object.freeze([
      chartPalette.blue,
      chartPalette.amber,
      chartPalette.green,
      chartPalette.red,
      chartPalette.purple,
      chartPalette.cyan,
      chartPalette.pink,
      chartPalette.lime,
      chartPalette.orange,
      chartPalette.indigo,
      chartPalette.teal,
      chartPalette.brightGreen,
    ]),
    holdingsPie: Object.freeze([
      colors.accentPrimary,
      chartPalette.blue,
      chartPalette.amber,
      chartPalette.red,
      chartPalette.purple,
      chartPalette.pink,
      chartPalette.teal,
      chartPalette.lime,
      chartPalette.cyan,
      chartPalette.orange,
      chartPalette.brightGreen,
      chartPalette.indigo,
    ]),
    // Neutral "Other"/fallback fill shares the single UI faint/grey knob
    // (--theme-text-faint -> --color-text-faint), so one dial drives every grey
    // in the app, chart fills included.
    other: colors.textFaint,
    net: chartPalette.blue,
    dividendIncome: chartPalette.dividendGreen,
    withholdingTax: chartPalette.withholdingRed,
    interestReceived: chartPalette.green,
    interestPaid: chartPalette.red,
    tick: colors.textPrimary,
    label: colors.textPrimary,
    dataLabel: colors.textPrimary,
    grid: `rgba(${rgb.border}, 0.36)`,
    tooltipBackground: 'var(--tooltip-bg)',
    tooltipBorder: '1px solid var(--tooltip-border-color)',
    tooltipShadow: 'var(--tooltip-shadow)',
    segmentBorder: colors.bg,
    subtleGrid: `rgba(${rgb.textPrimary}, 0.06)`,
    cursorFill: `rgba(${rgb.textPrimary}, 0.03)`,
    labelLine: `rgba(${rgb.textPrimary}, 0.18)`,
    highlightStroke: colors.dataHighlightBorder,
    highlightStrokeWidth: chartHighlightStrokeWidth,
    hoverStroke: colors.dataHighlightBorder,
    hoverStrokeWidth: chartHighlightStrokeWidth,
  });
}

// The CSS custom-property map. Layer 1 (--theme-*/--rgb-*) holds literals; the
// derived layers (--color-*, --shell-*, --account-type-*) are var() REFERENCE
// strings so the generated stylesheet and the runtime installer both preserve
// the re-skin cascade (change Layer 1, everything follows).
function buildBreakTwentyCssVariables(palette, rgb) {
  return Object.freeze({
    '--theme-app-background': palette.appBackground,
    '--theme-panel-surface': palette.panelSurface,
    '--theme-elevated-surface': palette.elevatedSurface,
    '--theme-hover-surface': palette.hoverSurface,
    '--theme-border': palette.border,
    '--theme-text-primary': palette.textPrimary,
    '--theme-text-secondary': palette.textSecondary,
    '--theme-text-tertiary': palette.textTertiary,
    '--theme-text-faint': palette.textFaint,
    '--theme-brand-wordmark-break': palette.brandWordmarkBreak,
    '--theme-brand-wordmark-twenty': palette.brandWordmarkTwenty,
    '--theme-accent-primary': palette.accentPrimary,
    '--theme-accent-secondary': palette.accentSecondary,
    '--theme-active-border': palette.activeBorder,
    '--theme-active-text': palette.activeText,
    '--theme-active-inset': palette.activeInset,
    '--theme-data-highlight-border': resolvePaletteColor(palette, palette.dataHighlightBorder),
    '--theme-data-highlight-text': resolvePaletteColor(palette, palette.dataHighlightText),
    '--theme-data-highlight-inset': resolvePaletteColor(palette, palette.dataHighlightInset),
    '--theme-positive': palette.positive,
    '--theme-negative': palette.negative,
    '--theme-warning': palette.warning,
    '--theme-network-warning': palette.networkWarning,
    '--theme-retry-warning': palette.retryWarning,
    '--theme-info': palette.info,
    '--theme-status-manual': palette.statusManual,
    '--theme-account-type-banking': palette.accountTypeBanking,
    '--theme-account-type-speculative': palette.accountTypeSpeculative,
    '--rgb-bg': rgb.bg,
    '--rgb-surface': rgb.surface,
    '--rgb-surface-elevated': rgb.surfaceElevated,
    '--rgb-surface-hover': rgb.hoverSurface,
    '--rgb-border': rgb.border,
    '--rgb-text-primary': rgb.textPrimary,
    '--rgb-text-secondary': rgb.textSecondary,
    '--rgb-text-tertiary': rgb.textTertiary,
    '--rgb-text-faint': rgb.textFaint,
    '--rgb-brand-wordmark-break': rgb.brandWordmarkBreak,
    '--rgb-brand-wordmark-twenty': rgb.brandWordmarkTwenty,
    '--rgb-accent-primary': rgb.accentPrimary,
    '--rgb-accent-secondary': rgb.accentSecondary,
    '--rgb-active-border': rgb.activeBorder,
    '--rgb-active-text': rgb.activeText,
    '--rgb-active-inset': rgb.activeInset,
    '--rgb-data-highlight-border': rgb.dataHighlightBorder,
    '--rgb-data-highlight-text': rgb.dataHighlightText,
    '--rgb-data-highlight-inset': rgb.dataHighlightInset,
    '--rgb-shell-accent': 'var(--rgb-accent-primary)',
    '--rgb-positive': rgb.positive,
    '--rgb-negative': rgb.negative,
    '--rgb-warning': rgb.warning,
    '--rgb-network-warning': rgb.networkWarning,
    '--rgb-info': rgb.info,
    '--rgb-white': rgb.white,
    '--color-bg': 'var(--theme-app-background)',
    '--color-surface': 'var(--theme-panel-surface)',
    '--color-surface-elevated': 'var(--theme-elevated-surface)',
    '--color-surface-hover': 'var(--theme-hover-surface)',
    '--color-border': 'var(--theme-border)',
    '--color-text-primary': 'var(--theme-text-primary)',
    '--color-text-secondary': 'var(--theme-text-secondary)',
    '--color-text-tertiary': 'var(--theme-text-tertiary)',
    '--color-text-faint': 'var(--theme-text-faint)',
    '--color-brand-wordmark-break': 'var(--theme-brand-wordmark-break)',
    '--color-brand-wordmark-twenty': 'var(--theme-brand-wordmark-twenty)',
    '--color-accent-primary': 'var(--theme-accent-primary)',
    '--color-accent-secondary': 'var(--theme-accent-secondary)',
    '--color-active-border': 'var(--theme-active-border)',
    '--color-active-text': 'var(--theme-active-text)',
    '--color-active-inset': 'var(--theme-active-inset)',
    '--color-data-highlight-border': 'var(--theme-data-highlight-border)',
    '--color-data-highlight-text': 'var(--theme-data-highlight-text)',
    '--color-data-highlight-inset': 'var(--theme-data-highlight-inset)',
    '--color-positive': 'var(--theme-positive)',
    '--color-negative': 'var(--theme-negative)',
    '--color-warning': 'var(--theme-warning)',
    '--color-network-warning': 'var(--theme-network-warning)',
    '--color-retry-warning': 'var(--theme-retry-warning)',
    '--color-info': 'var(--theme-info)',
    '--color-status-manual': 'var(--theme-status-manual)',
    '--color-focus-ring': 'rgba(var(--rgb-accent-primary), 0.28)',
    '--selected-data-border-width': `${selectedDataBorderWidth}px`,
    '--account-type-registered': 'var(--color-positive)',
    '--account-type-banking': 'var(--theme-account-type-banking)',
    '--account-type-liability': 'var(--color-negative)',
    '--account-type-speculative': 'var(--theme-account-type-speculative)',
    '--account-type-neutral': 'var(--color-text-faint)',
    // Physical / tangible assets (real estate, and future vehicles/valuables) — brand accent.
    // Single knob: re-point to a literal amber to decouple from the broader accent.
    '--account-type-physical': 'var(--color-accent-primary)',
  });
}

function buildBreakTwentyColorTheme(palette) {
  const rgb = buildBreakTwentyRgb(palette);
  const colors = buildBreakTwentyColors(palette, rgb);
  return Object.freeze({
    palette,
    rgb,
    colors,
    chartColors: buildBreakTwentyChartColors(colors, rgb, palette),
    cssVariables: buildBreakTwentyCssVariables(palette, rgb),
  });
}

function buildBreakTwentyColorThemes(palettes) {
  const themes = {};
  Object.entries(palettes).forEach(([mode, palette]) => {
    themes[mode] = buildBreakTwentyColorTheme(palette);
  });
  return Object.freeze(themes);
}

function defaultThemeTarget() {
  return typeof document !== 'undefined' ? document.documentElement : null;
}

export const breaktwentyColorThemes = buildBreakTwentyColorThemes(breaktwentyPalettes);

export const breaktwentyCssVariables = breaktwentyColorThemes[BREAKTWENTY_DEFAULT_THEME_MODE].cssVariables;

export function normalizeBreakTwentyThemeMode(mode) {
  return Object.prototype.hasOwnProperty.call(breaktwentyColorThemes, mode)
    ? mode
    : BREAKTWENTY_DEFAULT_THEME_MODE;
}

export function getBreakTwentyColorTheme(mode = BREAKTWENTY_DEFAULT_THEME_MODE) {
  return breaktwentyColorThemes[normalizeBreakTwentyThemeMode(mode)];
}

export function applyBreakTwentyTheme(mode = BREAKTWENTY_DEFAULT_THEME_MODE, target = defaultThemeTarget()) {
  if (!target?.style) return;

  const normalizedMode = normalizeBreakTwentyThemeMode(mode);

  Object.entries(getBreakTwentyColorTheme(normalizedMode).cssVariables).forEach(([name, value]) => {
    target.style.setProperty(name, value);
  });

  return normalizedMode;
}
