import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  BREAKTWENTY_DEFAULT_THEME_MODE,
  breaktwentyColorThemes,
  breaktwentyCssVariables,
} from './colors';
import { breaktwentyRuntimeCssVariables } from './runtimeTokens';
import { breaktwentySurfaceCssVariables, breaktwentySurfaceThemes } from './surfaces';
import { breaktwentyTypographyCssVariables } from './typography';

// The JS maps in colors.js/surfaces.js/typography.js are the SINGLE source of truth for the
// mirrored token families. scripts/generate-theme-css.js compiles them into
// src/theme/tokens.generated.css (the first-paint :root mirror, @imported at the
// top of App.css); applyBreakTwentyTheme/applyBreakTwentySurfaces/applyBreakTwentyTypography install the same maps
// at runtime. These guards keep that contract honest:
//   1. staleness — the committed generated file must match a fresh in-memory
//      serialization of the JS maps byte-for-byte (catches "edited colors.js,
//      forgot to regenerate/commit" and any hand-edit of the generated file);
//   2. no dual source — App.css's own :root must not (re)declare any generated
//      token, so the old two-copies-drift failure mode cannot return.

const require = createRequire(import.meta.url);
const { serializeThemeCss } = require('../../scripts/generate-theme-css');
const currentDir = path.dirname(fileURLToPath(import.meta.url));
const TEST_MODULE_PATTERN = /\.(?:test|spec)\.[cm]?[jt]sx?$/;

const jsTokens = { ...breaktwentyCssVariables, ...breaktwentySurfaceCssVariables, ...breaktwentyTypographyCssVariables };

function filesBelow(directory, predicate) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return filesBelow(entryPath, predicate);
    return predicate(entryPath) ? [entryPath] : [];
  });
}

const runtimeCustomPropertyPatterns = [
  // Holdings writes these measured row-overlay coordinates through a templated
  // setProperty() call; they are live geometry inputs, not theme tokens.
  /^--holdings-(?:hover|selected)-row-highlight-(?:top|height)$/,
];

test('theme registries keep mode and token-name parity', () => {
  const colorModes = Object.keys(breaktwentyColorThemes).sort();
  expect(colorModes).toContain(BREAKTWENTY_DEFAULT_THEME_MODE);
  expect(Object.keys(breaktwentySurfaceThemes).sort()).toEqual(colorModes);
  expect(Object.keys(breaktwentyRuntimeCssVariables).sort()).toEqual(colorModes);

  const registries = [
    Object.fromEntries(Object.entries(breaktwentyColorThemes).map(
      ([mode, theme]) => [mode, theme.cssVariables],
    )),
    breaktwentySurfaceThemes,
  ];

  registries.forEach((registry) => {
    const defaultTokenNames = Object.keys(registry[BREAKTWENTY_DEFAULT_THEME_MODE]).sort();
    expect(defaultTokenNames.length).toBeGreaterThan(0);
    colorModes.forEach((mode) => {
      expect(Object.keys(registry[mode]).sort()).toEqual(defaultTokenNames);
    });
  });
});

test('tokens.generated.css is fresh relative to the JS token maps', () => {
  const generatedPath = path.join(currentDir, 'tokens.generated.css');
  expect(fs.existsSync(generatedPath)).toBe(true);

  // Sanity floor so an accidentally emptied map cannot pass vacuously.
  expect(Object.keys(jsTokens).length).toBeGreaterThan(80);

  const committed = fs.readFileSync(generatedPath, 'utf8');
  const expected = serializeThemeCss(jsTokens);
  if (committed !== expected) {
    throw new Error(
      'src/theme/tokens.generated.css is stale or hand-edited. ' +
      'Run `npm run generate:theme` (or restart `npm start`) and commit the result.'
    );
  }
});

test('App.css :root does not redeclare generated tokens (single-source guard)', () => {
  const appCss = fs.readFileSync(path.join(currentDir, '..', 'App.css'), 'utf8');

  expect(appCss).toContain("@import './theme/tokens.generated.css';");

  const withoutComments = appCss.replace(/\/\*[\s\S]*?\*\//g, '');
  const root = withoutComments.match(/:root\s*\{([^}]*)\}/);
  expect(root).not.toBeNull();

  const cssOnlyTokens = [];
  for (const declaration of root[1].split(';')) {
    const match = declaration.match(/(--[\w-]+)\s*:/);
    if (match) cssOnlyTokens.push(match[1].trim());
  }
  // App.css :root still owns the CSS-only families (layout, spacing, tooltip,
  // controls, panel/shell). If this floor breaks, the block was gutted by accident.
  expect(cssOnlyTokens.length).toBeGreaterThan(50);

  const redeclared = cssOnlyTokens.filter((name) =>
    Object.prototype.hasOwnProperty.call(jsTokens, name)
  );
  expect(redeclared).toEqual([]);
});

test('CSS custom-property references resolve to a stylesheet or runtime supplier', () => {
  const sourceRoot = path.join(currentDir, '..');
  const cssFiles = filesBelow(sourceRoot, (filePath) => filePath.endsWith('.css'));
  const runtimeFiles = filesBelow(
    sourceRoot,
    (filePath) => /\.(?:js|jsx)$/.test(filePath) && !TEST_MODULE_PATTERN.test(filePath)
  );
  const declarations = new Set();
  const runtimeSuppliers = new Set();
  const references = [];

  for (const filePath of cssFiles) {
    const source = fs.readFileSync(filePath, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    for (const match of source.matchAll(/(--[\w-]+)\s*:/g)) declarations.add(match[1]);
    for (const match of source.matchAll(/var\(\s*(--[\w-]+)(\s*,)?/g)) {
      // A fallback makes the custom property explicitly optional; only bare
      // references can invalidate the declaration when their supplier drifts.
      if (!match[2]) references.push({ filePath, name: match[1] });
    }
  }

  for (const filePath of runtimeFiles) {
    const source = fs.readFileSync(filePath, 'utf8');
    for (const match of source.matchAll(/['"](--[\w-]+)['"]\s*:/g)) {
      runtimeSuppliers.add(match[1]);
    }
    for (const match of source.matchAll(/setProperty\(\s*['"](--[\w-]+)['"]/g)) {
      runtimeSuppliers.add(match[1]);
    }
    // HorizontalScrollProxy accepts custom-property names as configuration and
    // supplies them through its generic setProperty/style handoff.
    for (const match of source.matchAll(
      /\b(?:contentWidthProperty|controllerMeasurementProperty|targetScrollProperty|targetViewportProperty)\s*:\s*['"](--[\w-]+)['"]/g
    )) {
      runtimeSuppliers.add(match[1]);
    }
  }

  const unresolved = references
    .filter(({ name }) => (
      !declarations.has(name)
      && !runtimeSuppliers.has(name)
      && !runtimeCustomPropertyPatterns.some((pattern) => pattern.test(name))
    ))
    .map(({ filePath, name }) => `${path.relative(sourceRoot, filePath)}: ${name}`)
    .filter((value, index, values) => values.indexOf(value) === index)
    .sort();

  expect(unresolved).toEqual([]);
});
