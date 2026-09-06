import { describe, expect, it } from 'vitest';
import { brandAssetVersions } from '../generatedBrandAssets';
import { getBrandImageAssets } from './brandImageAssets';

describe('brand image assets', () => {
  it('builds versioned light-theme mark and wordmark density sources', () => {
    const assets = getBrandImageAssets('light', brandAssetVersions);

    expect(assets.mark).toEqual({
      src: `/assets/brand/breaktwenty-mark-light.png?v=${brandAssetVersions.lightLogo}`,
      version: brandAssetVersions.lightLogo,
    });
    expect(assets.wordmark).toEqual({
      src: `/assets/brand/breaktwenty-wordmark-light-160.png?v=${brandAssetVersions.lightWordmark}`,
      srcSet: `/assets/brand/breaktwenty-wordmark-light-160.png?v=${brandAssetVersions.lightWordmark} 1x, /assets/brand/breaktwenty-wordmark-light-320.png?v=${brandAssetVersions.lightWordmark} 2x`,
      version: brandAssetVersions.lightWordmark,
    });
  });

  it('builds versioned dark-theme mark and wordmark density sources', () => {
    expect(getBrandImageAssets('dark', brandAssetVersions)).toEqual({
      mark: {
        src: `/assets/brand/breaktwenty-mark-dark.png?v=${brandAssetVersions.darkLogo}`,
        version: brandAssetVersions.darkLogo,
      },
      wordmark: {
        src: `/assets/brand/breaktwenty-wordmark-dark-160.png?v=${brandAssetVersions.darkWordmark}`,
        srcSet: `/assets/brand/breaktwenty-wordmark-dark-160.png?v=${brandAssetVersions.darkWordmark} 1x, /assets/brand/breaktwenty-wordmark-dark-320.png?v=${brandAssetVersions.darkWordmark} 2x`,
        version: brandAssetVersions.darkWordmark,
      },
    });
  });
});
