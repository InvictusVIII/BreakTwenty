import providerCatalog from './providerCatalog.json';

export const ACCOUNT_TYPE_LABELS = {
  margin: 'Margin',
  tfsa: 'TFSA',
  rrsp: 'RRSP',
  fhsa: 'FHSA',
  resp: 'RESP',
  lira: 'LIRA',
  rrif: 'RRIF',
  rdsp: 'RDSP',
  lrsp: 'LRSP',
  lif: 'LIF',
  lrif: 'LRIF',
  prif: 'PRIF',
  rpp: 'RPP',
  dpsp: 'DPSP',
  spp: 'SPP',
  nreg: 'Non-Registered',
  '401k': '401(k)',
  ira: 'IRA',
  roth_ira: 'Roth IRA',
  cash: 'Cash',
  chequing: 'Chequing',
  savings: 'Savings',
  credit_card: 'Credit Card',
  loc: 'Line of Credit',
  line_of_credit: 'Line of Credit',
  heloc: 'HELOC',
  mortgage: 'Mortgage',
  loan: 'Loan',
  student_loan: 'Student Loan',
  auto_loan: 'Auto Loan',
  personal_loan: 'Private Loan',
  other_debt: 'Other Debt',
  crypto: 'Crypto',
  real_estate: 'Real Estate',
  // Real-estate property sub-types (Add to Net Worth → Real Estate). All group
  // under the Real Estate bucket/category; these only refine the row badge label.
  primary_residence: 'Primary Residence',
  secondary_residence: 'Secondary Residence',
  rental_property: 'Rental Property',
  investment_property: 'Investment Property',
  land: 'Land',
  other_real_estate: 'Other Real Estate',
  // Vehicle sub-types (Add to Net Worth → Vehicles). Mirror Monarch's vehicle list; all
  // group under the Vehicles bucket/category — these only refine the row badge label.
  car: 'Car',
  boat: 'Boat',
  motorcycle: 'Motorcycle',
  snowmobile: 'Snowmobile',
  bicycle: 'Bicycle',
  other_vehicle: 'Other',
  // Valuable sub-types (Add to Net Worth → Valuables). Mirror Monarch's valuables list;
  // all group under the Valuables bucket/category — these only refine the row badge label.
  art: 'Art',
  jewelry: 'Jewelry',
  collectibles: 'Collectibles',
  furniture: 'Furniture',
  other_valuable: 'Other',
  // Private-investment sub-types (Add to Net Worth → Private Investments). Mirror
  // Wealthica's Alternative list; all group under the Private Investments bucket/category.
  private_equity: 'Private Equity',
  business: 'Business',
  private_equity_fund: 'Private Equity Fund',
  venture_capital_fund: 'Venture Capital Fund',
  private_loan: 'Private Loan',
  insurance: 'Insurance',
  other_investment: 'Other Investment',
  other_asset: 'Other Asset',
  other: 'Other',
};

function getProviderFrontendConfig(provider) {
  return providerCatalog?.[provider]?.frontend || null;
}

function normalizeInstitutionName(name) {
  return String(name || '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

function buildProviderMap(selectValue) {
  return Object.entries(providerCatalog).reduce((acc, [provider, metadata]) => {
    const value = selectValue(provider, metadata?.frontend || null, metadata || null);
    if (value) {
      acc[provider] = value;
    }
    return acc;
  }, {});
}

export const SCRAPER_ENDPOINTS = buildProviderMap((_provider, frontend) => frontend?.scraperEndpoints || null);

export const SCRAPER_FIELD_LABELS = buildProviderMap((_provider, frontend) => frontend?.scraperFieldLabels || null);

export const API_PROVIDER_CONFIG = buildProviderMap((_provider, frontend) => frontend?.apiConfig || null);

const SCRAPER_AUTH_CONFIG = buildProviderMap((_provider, frontend) => frontend?.scraperAuth || null);

const DESKTOP_VISIBLE_AUTH_PROVIDERS = new Set(
  Object.entries(providerCatalog)
    .filter(([, metadata]) => {
      const scraperAuth = metadata?.frontend?.scraperAuth || {};
      const desktopVisibleAuth = scraperAuth.desktopVisibleAuth || {};
      return (
        scraperAuth.flow === 'desktop_visible_auth' &&
        desktopVisibleAuth.enabled === true
      );
    })
    .map(([provider]) => provider)
);

const LOGO_CONFIG_BY_INSTITUTION_NAME = Object.entries(providerCatalog).reduce((acc, [provider, metadata]) => {
  const logo = metadata?.logo;
  if (!logo?.asset) {
    return acc;
  }
  [
    provider,
    metadata?.displayName,
    ...(logo.aliases || []),
  ].forEach((name) => {
    const normalized = normalizeInstitutionName(name);
    if (normalized) {
      acc[normalized] = {
        asset: logo.asset,
        wide: Boolean(logo.wide),
      };
    }
  });
  return acc;
}, {});

const PROMO_MANUAL_LOGO_CONFIG_BY_INSTITUTION_NAME = {
  'canada life': { asset: 'canada-life.png', wide: false },
};

export const SUPPORT_LOG_PROVIDER_OPTIONS = Object.entries(providerCatalog)
  .filter(([, metadata]) => metadata?.availableInAddList && metadata?.implemented)
  .map(([provider, metadata]) => ({
    provider,
    label: metadata?.displayName || provider,
  }))
  .sort((left, right) => left.label.localeCompare(right.label));

export function getBackgroundSyncEndpoint(provider) {
  return getProviderFrontendConfig(provider)?.backgroundSyncEndpoint || null;
}

export function getProviderDisplayName(provider) {
  return providerCatalog?.[provider]?.displayName || provider;
}

export function getInstitutionSuccessMessage(institution, action) {
  const name = institution?.name || getProviderDisplayName(institution?.provider);
  if (action === 'add') {
    return `${name} has been added successfully`;
  }
  return `${name} synced successfully`;
}

export function getInstitutionInterruptedMessage(institution, action) {
  const name = institution?.name || getProviderDisplayName(institution?.provider);
  if (action === 'add') {
    return `Adding ${name} was interrupted`;
  }
  return `Syncing ${name} was interrupted`;
}

export function getUserInitiatedSyncEndpoint(provider) {
  const frontend = getProviderFrontendConfig(provider);
  return frontend?.userInitiatedSyncEndpoint || frontend?.backgroundSyncEndpoint || null;
}

// A connector-less / manual institution: no sync endpoint of any kind — the Cash
// holder ('manual'), user manual institutions ('manual_custom'), and the net-worth
// group buckets (real_estate/vehicles/valuables/private_investments/other_assets/
// debt). Such institutions are "Manually added" and never show sync status or
// sync-history tooltips.
export function isManualProvider(provider) {
  return !getUserInitiatedSyncEndpoint(provider) && !getBackgroundSyncEndpoint(provider);
}

// The connector-less net-worth group buckets specifically (tangible assets + manual
// debt) — a subset of the manual providers above. Used to gate per-asset management
// (revalue / delete) in the institution settings modal. Mirrors backend
// `services/asset_groups.py::ASSET_GROUP_PROVIDERS`.
export const ASSET_GROUP_PROVIDERS = new Set([
  'real_estate', 'vehicles', 'valuables', 'private_investments', 'other_assets', 'debt',
]);

export function isAssetGroupProvider(provider) {
  return ASSET_GROUP_PROVIDERS.has(provider);
}

// Curated account_type (sub-class) options per asset-group bucket, in display order.
// One source for the Real Estate property-type picker on the add modal AND the
// type/badge edit in the institution settings modal, so the two never drift. Labels
// resolve through ACCOUNT_TYPE_LABELS.
export const ASSET_GROUP_ACCOUNT_TYPES = {
  real_estate: [
    'primary_residence', 'secondary_residence', 'rental_property',
    'investment_property', 'land', 'other_real_estate',
  ],
  vehicles: ['car', 'boat', 'motorcycle', 'snowmobile', 'bicycle', 'other_vehicle'],
  valuables: ['art', 'jewelry', 'collectibles', 'furniture', 'other_valuable'],
  private_investments: ['private_equity', 'business', 'private_equity_fund', 'venture_capital_fund', 'private_loan', 'insurance', 'other_investment'],
  other_assets: ['other_asset'],
  // Debt bucket = debt with NO lender institution (a Private Loan from a person, or any
  // non-institutional obligation). Institution-issued debt (mortgage, LOC/HELOC, auto/student
  // loan, credit card) is added under a (manual) institution instead — see ManualInstitutionWizard.
  debt: ['personal_loan', 'other_debt'],
};

export function getAssetGroupAccountTypeOptions(provider) {
  return (ASSET_GROUP_ACCOUNT_TYPES[provider] || []).map(
    (value) => ({ value, label: ACCOUNT_TYPE_LABELS[value] || value }),
  );
}

export function getProviderAddAuthModal(provider) {
  return getProviderFrontendConfig(provider)?.addAuthModal || null;
}

// Add-to-Net-Worth launcher grouping: 'bank_brokerage' | 'crypto_wallet' | null.
// Drives which top-level tile a syncable provider appears under.
export function getProviderAddCategory(provider) {
  return providerCatalog?.[provider]?.category || null;
}

export function getInstitutionLogoConfig(name) {
  const normalized = normalizeInstitutionName(name);
  return PROMO_MANUAL_LOGO_CONFIG_BY_INSTITUTION_NAME[normalized] || LOGO_CONFIG_BY_INSTITUTION_NAME[normalized] || null;
}

export function getProviderLogoConfig(provider) {
  const logo = providerCatalog?.[provider]?.logo;
  if (!logo?.asset) {
    return null;
  }

  return {
    asset: logo.asset,
    wide: Boolean(logo.wide),
  };
}

export function getScraperAuthConfig(provider) {
  return SCRAPER_AUTH_CONFIG[provider] || {};
}

export function getDesktopVisibleAuthConfig(provider) {
  return getScraperAuthConfig(provider).desktopVisibleAuth || null;
}

export function isDesktopVisibleAuthProvider(provider) {
  return DESKTOP_VISIBLE_AUTH_PROVIDERS.has(provider);
}

export function getSyncDisplayConfig(provider) {
  return getProviderFrontendConfig(provider)?.syncDisplay || {};
}

export function hasProviderTransactionImport(provider) {
  return providerCatalog?.[provider]?.backend?.transactionImport?.enabled === true;
}

export function getProviderAuthAction(provider, status, reason = 'credential_error') {
  const frontend = getProviderFrontendConfig(provider);
  if (!frontend) {
    return null;
  }
  const dashboardAuth = frontend.dashboardAuth || {};

  if (reason === 'auth_required' && dashboardAuth.authRequired) {
    return dashboardAuth.authRequired;
  }

  if (!frontend.addAuthModal) {
    return null;
  }

  const modal = frontend.addAuthModal === 'api' ? 'api' : 'scraper';

  if (reason === 'auth_required') {
    return {
      modal,
      skipAutoLogin: false,
    };
  }

  const statusAction = dashboardAuth.credentialErrorByStatus?.[status];
  if (statusAction) {
    return statusAction;
  }
  if (dashboardAuth.credentialErrorDefault) {
    return dashboardAuth.credentialErrorDefault;
  }

  return {
    modal,
    skipAutoLogin: modal === 'scraper',
  };
}
