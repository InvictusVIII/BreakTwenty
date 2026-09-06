function normalizeCategoryColor(value) {
  const color = String(value || '').trim();
  if (/^#[0-9a-f]{6}$/i.test(color)) return color.toLowerCase();
  if (/^#[0-9a-f]{3}$/i.test(color)) {
    return `#${color.slice(1).split('').map((char) => `${char}${char}`).join('')}`.toLowerCase();
  }
  return null;
}

function hexToRgb(hex) {
  const raw = hex.replace('#', '');
  return {
    r: parseInt(raw.slice(0, 2), 16),
    g: parseInt(raw.slice(2, 4), 16),
    b: parseInt(raw.slice(4, 6), 16),
  };
}

function getReadableLabelColor(hex) {
  const channels = Object.values(hexToRgb(hex)).map((channel) => {
    const value = channel / 255;
    return value <= 0.04045
      ? value / 12.92
      : ((value + 0.055) / 1.055) ** 2.4;
  });
  const luminance = (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2]);
  return luminance > 0.179
    ? 'var(--color-text-primary)'
    : 'rgb(var(--rgb-white))';
}

export function getCategoryIdentityColor(category, mode) {
  if (!category || typeof category !== 'object') return null;
  return mode === 'light' ? category.color_light : category.color_dark;
}

export function resolveCategoryAccentColor(category, mode, fallback = null) {
  const normalizedColor = normalizeCategoryColor(getCategoryIdentityColor(category, mode));
  if (!normalizedColor) return fallback;
  return normalizedColor;
}

export function getCategoryThemeVars(category, mode) {
  const normalizedColor = normalizeCategoryColor(getCategoryIdentityColor(category, mode));

  if (!normalizedColor) {
    const accentColor = mode === 'light'
      ? 'var(--color-text-faint)'
      : 'rgba(var(--rgb-white), 0.18)';
    return {
      '--cat-color': accentColor,
      '--cat-accent-color': accentColor,
      '--cat-label-color': 'var(--color-text-secondary)',
      '--cat-color-bg': mode === 'light'
        ? 'rgba(var(--rgb-border), 0.06)'
        : 'rgba(var(--rgb-white), 0.04)',
      '--cat-color-border': mode === 'light'
        ? 'rgba(var(--rgb-border), 0.28)'
        : 'rgba(var(--rgb-white), 0.2)',
    };
  }

  if (mode === 'light') {
    return {
      '--cat-color': normalizedColor,
      '--cat-accent-color': normalizedColor,
      '--cat-label-color': getReadableLabelColor(normalizedColor),
      '--cat-color-bg': normalizedColor,
      '--cat-color-border': normalizedColor,
    };
  }

  return {
    '--cat-color': normalizedColor,
    '--cat-accent-color': normalizedColor,
    '--cat-label-color': normalizedColor,
    '--cat-color-bg': `${normalizedColor}33`,
    '--cat-color-border': `${normalizedColor}88`,
  };
}
