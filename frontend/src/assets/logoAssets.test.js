import providerCatalog from '../constants/providerCatalog.json';
import { getInstitutionLogoConfig, getProviderLogoConfig } from '../constants/providers';
import { LOGO_ASSETS } from './logoAssets';

const LAUNCHER_LOGO_ASSETS = [
  'bank-brokerage.png',
  'crypto-wallet.png',
  'manual-institution.png',
  'real-estate.png',
  'vehicles.png',
  'valuables.png',
  'private-investments.png',
  'other-assets.png',
  'cash.png',
  'debt.png',
];
const INTENTIONAL_PROMO_LOGO_ASSETS = ['canada-life.png'];

describe('provider logo asset ownership', () => {
  it('resolves every provider catalog logo through the registry', () => {
    Object.entries(providerCatalog).forEach(([provider, metadata]) => {
      const asset = metadata?.logo?.asset;
      if (!asset) return;
      expect(LOGO_ASSETS).toHaveProperty(asset);
      expect(getProviderLogoConfig(provider)).toMatchObject({ asset });
      expect(getInstitutionLogoConfig(metadata.displayName)).toMatchObject({ asset });
    });
  });

  it('keeps every registry entry owned by a provider, launcher, or intentional promo alias', () => {
    const providerAssets = Object.values(providerCatalog)
      .map((metadata) => metadata?.logo?.asset)
      .filter(Boolean);
    const ownedAssets = new Set([
      ...providerAssets,
      ...LAUNCHER_LOGO_ASSETS,
      ...INTENTIONAL_PROMO_LOGO_ASSETS,
    ]);

    expect(Object.keys(LOGO_ASSETS).sort()).toEqual([...ownedAssets].sort());
    LAUNCHER_LOGO_ASSETS.forEach((asset) => expect(LOGO_ASSETS).toHaveProperty(asset));
    expect(getInstitutionLogoConfig('Canada Life')).toMatchObject({ asset: 'canada-life.png' });
  });
});
