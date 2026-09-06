import { getCategoryThemeVars, resolveCategoryAccentColor } from './categoryColors';

describe('category theme colors', () => {
  const category = { color_dark: '#FFE700', color_light: '#123456' };

  it('selects the independently stored dark color in dark mode', () => {
    expect(resolveCategoryAccentColor(category, 'dark', '#999999')).toBe('#ffe700');
    expect(getCategoryThemeVars(category, 'dark')).toMatchObject({
      '--cat-color': '#ffe700',
      '--cat-accent-color': '#ffe700',
    });
  });

  it('uses the stored light color literally for every light-mode surface', () => {
    const accentColor = resolveCategoryAccentColor(category, 'light', '#999999');

    expect(accentColor).toBe('#123456');
    expect(getCategoryThemeVars(category, 'light')).toMatchObject({
      '--cat-color': '#123456',
      '--cat-accent-color': '#123456',
      '--cat-color-bg': '#123456',
      '--cat-color-border': '#123456',
      '--cat-label-color': 'rgb(var(--rgb-white))',
    });
  });

  it('keeps dark user-selected light-theme colors dark in the preview', () => {
    expect(getCategoryThemeVars({ color_light: '#007325' }, 'light')).toMatchObject({
      '--cat-color': '#007325',
      '--cat-color-bg': '#007325',
      '--cat-label-color': 'rgb(var(--rgb-white))',
    });
  });

  it('uses the normal light-theme text color on bright category backgrounds', () => {
    expect(getCategoryThemeVars({ color_light: '#f4d35e' }, 'light')).toMatchObject({
      '--cat-color-bg': '#f4d35e',
      '--cat-label-color': 'var(--color-text-primary)',
    });
  });

  it('uses the caller fallback for invalid chart colors', () => {
    expect(resolveCategoryAccentColor(null, 'light', '#999999')).toBe('#999999');
  });
});
