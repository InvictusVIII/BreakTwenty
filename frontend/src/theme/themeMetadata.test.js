import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { applyBreakTwentyThemeMode } from './applyTheme';
import { BREAKTWENTY_DEFAULT_THEME_MODE, getBreakTwentyColorTheme } from './colors';

const currentDir = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.join(currentDir, '..', '..');

test('entry and manifest colors match the canonical default background', () => {
  const defaultBackground = getBreakTwentyColorTheme(BREAKTWENTY_DEFAULT_THEME_MODE).palette.appBackground;
  const indexHtml = fs.readFileSync(path.join(frontendDir, 'index.html'), 'utf8');
  const manifest = JSON.parse(fs.readFileSync(path.join(frontendDir, 'public', 'manifest.json'), 'utf8'));
  const themeMeta = indexHtml.match(/<meta\s+name="theme-color"\s+content="([^"]+)"\s*\/>/);

  expect(themeMeta?.[1]).toBe(defaultBackground);
  expect(manifest.theme_color).toBe(defaultBackground);
  expect(manifest.background_color).toBe(defaultBackground);
});

test('applying a mode updates browser theme chrome from the active palette', () => {
  const themeMeta = document.createElement('meta');
  themeMeta.setAttribute('name', 'theme-color');
  document.head.appendChild(themeMeta);

  applyBreakTwentyThemeMode('light', document.documentElement);
  expect(themeMeta.getAttribute('content')).toBe(getBreakTwentyColorTheme('light').palette.appBackground);

  applyBreakTwentyThemeMode('dark', document.documentElement);
  expect(themeMeta.getAttribute('content')).toBe(getBreakTwentyColorTheme('dark').palette.appBackground);
  themeMeta.remove();
});
