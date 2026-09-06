const BRAND_MARK_SRC_BY_THEME = Object.freeze({
  dark: '/assets/brand/breaktwenty-mark-dark.png',
  light: '/assets/brand/breaktwenty-mark-light.png',
});

const BRAND_WORDMARK_SRC_BY_THEME = Object.freeze({
  dark: Object.freeze({
    oneX: '/assets/brand/breaktwenty-wordmark-dark-160.png',
    twoX: '/assets/brand/breaktwenty-wordmark-dark-320.png',
  }),
  light: Object.freeze({
    oneX: '/assets/brand/breaktwenty-wordmark-light-160.png',
    twoX: '/assets/brand/breaktwenty-wordmark-light-320.png',
  }),
});

const VERSION_KEYS_BY_THEME = Object.freeze({
  dark: Object.freeze({ mark: 'darkLogo', wordmark: 'darkWordmark' }),
  light: Object.freeze({ mark: 'lightLogo', wordmark: 'lightWordmark' }),
});

function versionBrandAssetSrc(src, version) {
  if (!version) return src;
  return `${src}?v=${encodeURIComponent(version)}`;
}

export function getBrandImageAssets(themeMode, versions = {}) {
  const mode = themeMode;
  const versionKeys = VERSION_KEYS_BY_THEME[mode];
  const markVersion = versions[versionKeys.mark];
  const wordmarkVersion = versions[versionKeys.wordmark];
  const wordmarkSources = BRAND_WORDMARK_SRC_BY_THEME[mode];
  const wordmarkOneXSrc = versionBrandAssetSrc(wordmarkSources.oneX, wordmarkVersion);
  const wordmarkTwoXSrc = versionBrandAssetSrc(wordmarkSources.twoX, wordmarkVersion);

  return {
    mark: {
      src: versionBrandAssetSrc(BRAND_MARK_SRC_BY_THEME[mode], markVersion),
      version: markVersion,
    },
    wordmark: {
      src: wordmarkOneXSrc,
      srcSet: `${wordmarkOneXSrc} 1x, ${wordmarkTwoXSrc} 2x`,
      version: wordmarkVersion,
    },
  };
}
