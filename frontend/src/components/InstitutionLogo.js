import React, { useEffect, useState } from 'react';
import { MdHomeWork, MdDirectionsCar, MdDiamond, MdBusinessCenter, MdCategory, MdCreditCard } from 'react-icons/md';
import { LOGO_ASSETS } from '../assets/logoAssets';
import { getInstitutionLogoConfig, getProviderLogoConfig } from '../constants/providers';
import { apiFetch, isBackendApiUrl } from '../utils/apiTransport';

// Connector-less net-worth group buckets render a category icon instead of an
// initial badge (Cash keeps its catalog logo asset). Keyed by provider so the
// icon survives institution renames.
const GROUP_PROVIDER_ICONS = {
  real_estate: MdHomeWork,
  vehicles: MdDirectionsCar,
  valuables: MdDiamond,
  private_investments: MdBusinessCenter,
  other_assets: MdCategory,
  debt: MdCreditCard,
};

const STAGED_LOGO_ASSETS = new Set([
  'bank-brokerage.png',
  'cash.png',
  'crypto-wallet.png',
  'debt.png',
  'manual-institution.png',
  'other-assets.png',
  'private-investments.png',
  'real-estate.png',
  'valuables.png',
  'vehicles.png',
]);

function logoSizeValue(size) {
  return typeof size === 'number' ? `${size}px` : size;
}

function logoArtStyle(size) {
  return { '--institution-logo-size': logoSizeValue(size) };
}

function logoFilterKey(value) {
  if (!value) return null;
  return String(value)
    .replace(/\.[a-z0-9]+$/i, '')
    .replace(/[^a-z0-9]+/gi, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase();
}

function useAuthenticatedImageUrl(sourceUrl) {
  const requiresAuthentication = Boolean(sourceUrl) && isBackendApiUrl(sourceUrl);
  const [resolvedImage, setResolvedImage] = useState({ sourceUrl: '', objectUrl: '' });

  useEffect(() => {
    if (!requiresAuthentication) {
      return undefined;
    }
    let cancelled = false;
    let createdUrl = '';
    const controller = new AbortController();
    void apiFetch(sourceUrl, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Institution logo returned HTTP ${response.status}.`);
        return response.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        createdUrl = URL.createObjectURL(blob);
        setResolvedImage({ sourceUrl, objectUrl: createdUrl });
      })
      .catch((error) => {
        if (!cancelled && error?.name !== 'AbortError') {
          setResolvedImage({ sourceUrl, objectUrl: '' });
        }
      });
    return () => {
      cancelled = true;
      controller.abort();
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [requiresAuthentication, sourceUrl]);

  if (!requiresAuthentication) return sourceUrl;
  return resolvedImage.sourceUrl === sourceUrl ? resolvedImage.objectUrl : '';
}

export function AuthenticatedInstitutionImage({ src, alt = '', ...props }) {
  const resolvedSource = useAuthenticatedImageUrl(src);
  if (!resolvedSource) return null;
  return <img {...props} src={resolvedSource} alt={alt} />;
}

function InstitutionLogo({
  name,
  size = 28,
  provider = null,
  logoUrl = null,
  boxed = false,
  staged = false,
  stageKey = null,
}) {
  const resolvedLogoUrl = useAuthenticatedImageUrl(logoUrl);
  const label = String(name || 'Institution');
  const fallbackFontSize = typeof size === 'number' ? size * 0.45 : `calc(${size} * 0.45)`;
  const fallbackIconSize = typeof size === 'number' ? Math.round(size * 0.58) : `calc(${size} * 0.58)`;
  const renderStage = (children, filterSource = stageKey) => {
    const filterTokenKey = logoFilterKey(filterSource);
    const style = { '--institution-logo-stage-size': logoSizeValue(size) };

    if (filterTokenKey) {
      style['--institution-logo-filter-saturation'] = (
        `var(--institution-logo-${filterTokenKey}-saturation)`
      );
      style['--institution-logo-filter-contrast'] = (
        `var(--institution-logo-${filterTokenKey}-contrast)`
      );
      style['--institution-logo-filter-brightness'] = (
        `var(--institution-logo-${filterTokenKey}-brightness)`
      );
    }

    return (
      <span className="institution-logo-stage" style={style}>
        {children}
      </span>
    );
  };

  if (logoUrl && resolvedLogoUrl) {
    if (staged) {
      return renderStage(<img src={resolvedLogoUrl} alt={label} className="institution-logo-stage-img" />);
    }

    // `boxed` keeps a uniform size×size square and `contain`s the art so table
    // rows line up; the default `cover` fill is unchanged for other callers.
    return (
      <img
        src={resolvedLogoUrl}
        alt={label}
        className={`institution-logo-art is-square ${boxed ? 'is-boxed' : 'is-cover'}`}
        style={logoArtStyle(size)}
      />
    );
  }
  const logoConfig = provider === 'manual_custom'
    ? getInstitutionLogoConfig(label) || getProviderLogoConfig(provider)
    : getProviderLogoConfig(provider) || getInstitutionLogoConfig(label);
  const logo = logoConfig?.asset ? LOGO_ASSETS[logoConfig.asset] : null;

  if (logo) {
    if (staged || STAGED_LOGO_ASSETS.has(logoConfig.asset)) {
      return renderStage(<img src={logo} alt={label} className="institution-logo-stage-img" />, logoConfig.asset);
    }

    // `boxed` forces even wide wordmark assets into the size×size square (so the
    // logo column is one fixed width); otherwise wide assets keep natural width.
    return (
      <img
        src={logo}
        alt={label}
        className={`institution-logo-art ${boxed ? 'is-square is-boxed' : (logoConfig.wide ? 'is-wide' : 'is-square')}`}
        style={logoArtStyle(size)}
      />
    );
  }

  const GroupIcon = provider ? GROUP_PROVIDER_ICONS[provider] : null;
  if (GroupIcon) {
    return renderStage(<GroupIcon size={fallbackIconSize} aria-hidden="true" />, provider);
  }

  return (
    <div
      className="institution-logo-fallback"
      style={{
        '--institution-logo-size': logoSizeValue(size),
        '--institution-logo-fallback-font-size': logoSizeValue(fallbackFontSize),
      }}
    >
      {label.charAt(0).toUpperCase()}
    </div>
  );
}

export default InstitutionLogo;
