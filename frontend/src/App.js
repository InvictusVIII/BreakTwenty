import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { BrowserRouter as Router, NavLink, Route, useLocation, useNavigate } from 'react-router-dom';
import {
  MdAccessTime,
  MdCheck,
  MdStar,
} from 'react-icons/md';
import {
  FaPatreon,
} from 'react-icons/fa6';
import { SiKofi } from 'react-icons/si';
import './App.css';
import {
  BREAKTWENTY_DEFAULT_THEME_MODE,
  BREAKTWENTY_THEME_HMR_EVENT,
  normalizeBreakTwentyThemeMode,
  applyBreakTwentyThemeMode,
  getAppliedBreakTwentyColorTheme,
} from './theme/applyTheme';
import AccountsIconSvg from './assets/icons/accounts-icon.svg?react';
import AddNetWorthIconPng from './assets/icons/add-net-worth-icon.png';
import AddTransactionIconPng from './assets/icons/add-transaction-icon.png';
import CashFlowIconPng from './assets/icons/cash-flow-icon.png';
import DashboardIconPng from './assets/icons/dashboard-icon.png';
import DiagnosticsIconSvg from './assets/icons/diagnostics-icon.svg?react';
import FaqIconSvg from './assets/icons/faq-icon.svg?react';
import InvestmentsIconSvg from './assets/icons/investments-icon.svg?react';
import SettingsIconSvg from './assets/icons/settings-icon.svg?react';
import TransactionsIconSvg from './assets/icons/transactions-icon.svg?react';
import {
  PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
  PORTFOLIO_TIMEFRAMES,
  DEFAULT_PORTFOLIO_TIMEFRAME,
} from './utils/portfolioViewUtils';
import AddToNetWorthModal from './components/AddToNetWorthModal';
import ManualInstitutionWizard from './components/ManualInstitutionWizard';
import TangibleAssetModal from './components/TangibleAssetModal';
import CashOpeningModal from './components/CashOpeningModal';
import AddTransactionModal from './components/AddTransactionModal';
import WelcomeModal from './components/WelcomeModal';
import { TOUR_DEMO_NOW_ISO } from './components/tourDemoData';
import {
  installPromoDemoFetch,
  isPromoDemoActive,
  PROMO_DEMO_CHANGE_EVENT,
  PROMO_DEMO_NOW_ISO,
  setPromoDemoActive,
} from './components/promoDemoEnvironment';
import ScraperAuthModal from './components/ScraperAuthModal';
import ApiAuthModal from './components/ApiAuthModal';
import InstitutionLogo from './components/InstitutionLogo';
import GlobalTooltip from './components/GlobalTooltip';
import AppStatusNotice from './components/AppStatusNotice';
import AppRoutes from './components/AppRoutes';
import TimelineTrigger from './components/TimelineTrigger';
import TimelineCustomRangePicker from './components/TimelineCustomRangePicker';
import BalancesToggleButton from './components/BalancesToggleButton';
import AppViewFiltersMenu from './components/AppViewFiltersMenu';
import BrandName from './components/BrandName';
import ControlChevron from './components/ControlChevron';
import TriangleIcon from './components/TriangleIcon';
import useDismissibleLayer from './hooks/useDismissibleLayer';
import useTimelineCustomRangeDraft from './hooks/useTimelineCustomRangeDraft';
import { API } from './config';
import { makeCurrencyConverter } from './utils/currencyView';
import { SELECTABLE_CURRENCIES } from './constants/currencies';
import CurrencyViewPicker from './components/CurrencyViewPicker';
import {
  getBackgroundSyncEndpoint,
  getProviderAddAuthModal,
  getProviderDisplayName,
  SUPPORT_LOG_PROVIDER_OPTIONS,
} from './constants/providers';
import { APP_BRAND_NAME, APP_WEBSITE_URL } from './constants/brand';
import {
  cleanupPendingInstitutionAddOnStartup,
  getPendingInstitutionAddProviders,
  clearInterruptedInstitutionAdd,
} from './utils/incompleteInstitutionAdds';
import {
  AUTO_SYNC_COOLDOWN_MS,
  AUTO_SYNC_LAST_TRIGGER_STORAGE_KEY,
  recordAutoSyncTrigger,
  shouldRunStartupAutoSync,
} from './utils/autoSyncCadence';
import {
  DEFAULT_USER_TIME_FORMAT,
  DEFAULT_USER_TIMEZONE,
  getBrowserTimezone,
  normalizeUserTimeFormat,
} from './utils/timezone';
import { getAppNow } from './utils/appClock';
import {
  acknowledgeBackendRecovery,
  exportDesktopAppDiagnostic,
  getDesktopUpdateStatus,
  getMainWindowZoomStatus,
  listDesktopAppDiagnostics,
  subscribeDesktopUpdateStatus,
  subscribeBackendRecovery,
  subscribeMainWindowZoomStatus,
} from './utils/desktopBridge';
import { presentSupportArchive } from './utils/supportArchive';
import {
  fetchActiveSyncBatches,
  fetchSyncBatch,
  forgetMonitoredSyncBatchId,
  getSyncNetworkBlocker,
  hasActiveManualSyncBatch,
  isTerminalSyncBatch,
  loadMonitoredSyncBatchIds,
  mergeInstitutionAccounts,
  rememberMonitoredSyncBatchId,
  reconcileActiveSyncBatches,
  runSyncBatchUntilDone,
} from './utils/syncBatch';
import {
  admitAutoSyncRun,
  startIndependentSyncLanes,
} from './utils/syncOrchestration';
import { formatSyncErrorMessage, getClientSyncFailureState } from './utils/clientSyncFailure';
import {
  reconnectServerEvents,
  SERVER_EVENT_TYPES,
  subscribeServerEvent,
} from './utils/serverEvents';
import { reconcileRendererAfterBackendRecovery } from './utils/backendRecovery';
import {
  getActiveSyncActivityKeys,
  hasSettledSyncActivity,
} from './utils/syncActivity';
import {
  getTransactionImportIssueMessage,
  getTransactionImportSettlementStatus,
  isProviderAuthStatus,
  TRANSACTION_IMPORT_RETRY_STATUS,
} from './utils/syncDisplayState';
import { getBrandImageAssets } from './utils/brandImageAssets';
import loadingScreenConfig from './constants/loadingScreen.generated.json';
import {
  createLatestRequestCoordinator,
  fetchAppSnapshot,
  shouldRefreshSettledTransactionImports,
} from './utils/appSnapshot';
import { fetchScopeInstitutions } from './utils/scopeInstitutions';
import {
  CurrencyContext,
  RightTrayContext,
  ThemeContext,
} from './appState';
import { brandAssetVersions } from './generatedBrandAssets';

const Accounts = React.lazy(() => import('./pages/Accounts'));
const CashFlow = React.lazy(() => import('./pages/CashFlow'));
const CategoriesSettings = React.lazy(() => import('./pages/CategoriesSettings'));
const Dashboard = React.lazy(() => import('./pages/Dashboard'));
const Faq = React.lazy(() => import('./pages/Faq'));
const Holdings = React.lazy(() => import('./pages/Holdings'));
const Licenses = React.lazy(() => import('./pages/Licenses'));
const Settings = React.lazy(() => import('./pages/Settings'));
const Transactions = React.lazy(() => import('./pages/Transactions'));

const BREAKTWENTY_THEME_STORAGE_KEY = 'breaktwenty_theme_mode_v1';
const RIGHT_TRAY_RESERVATION_POLICIES = Object.freeze({
  'transaction-detail': 'force',
  'position-detail': 'auto',
  'income-detail': 'auto',
  'cashflow-detail': 'auto',
  'accounts-detail': 'force',
});

function getRightTrayReservationPolicy(key) {
  return RIGHT_TRAY_RESERVATION_POLICIES[key] || 'auto';
}

function readStoredBreakTwentyThemeMode() {
  if (typeof window === 'undefined') return BREAKTWENTY_DEFAULT_THEME_MODE;
  try {
    return normalizeBreakTwentyThemeMode(window.localStorage.getItem(BREAKTWENTY_THEME_STORAGE_KEY));
  } catch {
    return BREAKTWENTY_DEFAULT_THEME_MODE;
  }
}

let controlSlotAuditFrame = null;

function getRectCenter(rect) {
  return {
    x: rect.left + (rect.width / 2),
    y: rect.top + (rect.height / 2),
  };
}

function scheduleControlSlotAlignmentAudit() {
  if (!import.meta.env.DEV || typeof window === 'undefined' || typeof document === 'undefined') {
    return;
  }
  if (controlSlotAuditFrame !== null) {
    window.cancelAnimationFrame(controlSlotAuditFrame);
  }
  controlSlotAuditFrame = window.requestAnimationFrame(() => {
    controlSlotAuditFrame = window.requestAnimationFrame(() => {
      controlSlotAuditFrame = null;
      const deviceScale = Number.parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue('--app-effective-device-scale')
      ) || 1;
      const tolerance = 0.5 / deviceScale;
      const violations = [];

      document.querySelectorAll('.app-control-root').forEach((root) => {
        const rootRect = root.getBoundingClientRect();
        if (rootRect.width <= 0 || rootRect.height <= 0) {
          return;
        }
        const rootCenter = getRectCenter(rootRect);
        const rootStyle = getComputedStyle(root);
        const slots = Array.from(root.children).filter((child) => (
          child.matches('.app-control-icon, .app-control-label, .app-control-chevron')
          && getComputedStyle(child).display !== 'none'
          && child.getBoundingClientRect().width > 0
        ));

        if (slots.length > 1 && rootStyle.justifyContent === 'center') {
          const slotRects = slots.map((slot) => slot.getBoundingClientRect());
          const groupCenterX = (
            Math.min(...slotRects.map((rect) => rect.left))
            + Math.max(...slotRects.map((rect) => rect.right))
          ) / 2;
          if (Math.abs(groupCenterX - rootCenter.x) > tolerance) {
            violations.push({ control: root.className, invariant: 'content-group-center' });
          }
        }

        for (const slot of slots) {
          const slotRect = slot.getBoundingClientRect();
          const slotCenter = getRectCenter(slotRect);
          if (Math.abs(slotCenter.y - rootCenter.y) > tolerance) {
            violations.push({ control: root.className, invariant: 'slot-block-center' });
          }

          if (slot.matches('.app-control-label')) {
            continue;
          }

          const glyph = slot.firstElementChild;
          if (!glyph) {
            continue;
          }
          const glyphCenter = getRectCenter(glyph.getBoundingClientRect());
          if (
            Math.abs(glyphCenter.x - slotCenter.x) > tolerance
            || Math.abs(glyphCenter.y - slotCenter.y) > tolerance
          ) {
            violations.push({ control: root.className, invariant: 'glyph-box-center' });
          }
        }
      });

      document.querySelectorAll(
        '.floating-nav-btn-icon > svg.floating-nav-icon-graphic, .floating-nav-toggle > .floating-nav-toggle-icon'
      ).forEach((glyph) => {
        const slot = glyph.matches('.floating-nav-toggle-icon')
          ? glyph.closest('.floating-nav-toggle')
          : glyph.closest('.floating-nav-btn-icon');
        const slotRect = slot?.getBoundingClientRect();
        const glyphRect = glyph.getBoundingClientRect();
        if (
          !slotRect
          || slotRect.width <= 0
          || slotRect.height <= 0
          || glyphRect.width <= 0
          || glyphRect.height <= 0
        ) {
          return;
        }
        const slotCenter = getRectCenter(slotRect);
        const glyphCenter = getRectCenter(glyphRect);
        if (
          Math.abs(glyphCenter.x - slotCenter.x) > tolerance
          || Math.abs(glyphCenter.y - slotCenter.y) > tolerance
        ) {
          violations.push({
            control: slot.className,
            invariant: 'sidebar-glyph-box-center',
          });
        }
      });

      document.querySelectorAll('.app-theme-toggle-thumb > svg').forEach((glyph) => {
        const thumb = glyph.parentElement;
        const thumbRect = thumb.getBoundingClientRect();
        const glyphRect = glyph.getBoundingClientRect();
        if (thumbRect.width <= 0 || thumbRect.height <= 0 || glyphRect.width <= 0 || glyphRect.height <= 0) {
          return;
        }
        const thumbCenter = getRectCenter(thumbRect);
        const glyphCenter = getRectCenter(glyphRect);
        if (
          Math.abs(glyphCenter.x - thumbCenter.x) > tolerance
          || Math.abs(glyphCenter.y - thumbCenter.y) > tolerance
        ) {
          violations.push({
            control: thumb.className,
            invariant: 'theme-thumb-glyph-box-center',
          });
        }
      });

      if (violations.length > 0) {
        console.warn(`${APP_BRAND_NAME} control-slot alignment invariant failed`, violations);
      }
    });
  });
}

function applyRendererDeviceScale(zoomStatus) {
  if (typeof document === 'undefined') {
    return;
  }
  const rendererScale = Number(window.devicePixelRatio);
  const displayScale = Number(zoomStatus?.display?.scaleFactor);
  const zoomFactor = Number(zoomStatus?.currentZoomFactor);
  const reportedScale = displayScale > 0 && zoomFactor > 0
    ? displayScale * zoomFactor
    : 1;
  const effectiveDeviceScale = Number.isFinite(rendererScale) && rendererScale > 0
    ? rendererScale
    : reportedScale;
  document.documentElement.style.setProperty(
    '--app-effective-device-scale',
    String(effectiveDeviceScale)
  );
  document.documentElement.style.setProperty(
    '--app-device-pixel',
    `${1 / effectiveDeviceScale}px`
  );
  scheduleControlSlotAlignmentAudit();
}

function applyDesktopZoomRimScale(zoomStatus) {
  if (typeof document === 'undefined') return;
  const zoomFactor = Number(zoomStatus?.currentZoomFactor);
  const normalizedZoomFactor = Number.isFinite(zoomFactor) && zoomFactor > 0
    ? zoomFactor
    : 1;
  document.documentElement.style.setProperty('--app-zoom-factor', String(normalizedZoomFactor));
  document.documentElement.style.setProperty('--app-zoom-rim-scale', String(1 / normalizedZoomFactor));
  applyRendererDeviceScale(zoomStatus);
}

const INITIAL_BREAKTWENTY_THEME_MODE = readStoredBreakTwentyThemeMode();
const INITIAL_LOADING_BRAND_ASSETS = getBrandImageAssets(INITIAL_BREAKTWENTY_THEME_MODE, brandAssetVersions);
const LOADING_CATCHPHRASE = loadingScreenConfig.catchphrase;
const PROMO_DEMO_ENABLED = import.meta.env.DEV;

applyBreakTwentyThemeMode(INITIAL_BREAKTWENTY_THEME_MODE);
if (PROMO_DEMO_ENABLED) {
  installPromoDemoFetch();
}

const STATIC_MATERIAL_TEXTURE_URLS = [];

const CSS_MATERIAL_TEXTURE_TOKENS = [
  '--app-canvas-material',
  '--app-canvas-material-light',
  '--panel-material',
  '--panel-material-light',
  '--modal-material',
  '--modal-material-light',
  '--inset-layer-material',
  '--inset-layer-material-light',
  '--sidebar-material',
  '--sidebar-material-light',
  '--button-app-material',
  '--button-app-material-light',
  '--button-panel-material',
  '--button-panel-material-light',
  '--button-selected-material',
  '--button-selected-material-light',
];

let materialTexturePreloadPromise = null;

function readCssUrlToken(styles, token) {
  const value = styles.getPropertyValue(token);
  const match = value.match(/url\((['"]?)(.*?)\1\)/);
  return match?.[2] || '';
}

function getMaterialTextureUrls() {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return STATIC_MATERIAL_TEXTURE_URLS;
  }

  const styles = window.getComputedStyle(document.documentElement);
  const cssUrls = CSS_MATERIAL_TEXTURE_TOKENS
    .map((token) => readCssUrlToken(styles, token))
    .filter(Boolean)
    .map((src) => new URL(src, window.location.href).href);

  return [...new Set([...STATIC_MATERIAL_TEXTURE_URLS, ...cssUrls])];
}

function warmPaintMaterialTextures(urls) {
  if (typeof document === 'undefined' || !urls.length) {
    return () => {};
  }

  const removeWarmup = (host) => {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        host.remove();
      });
    });
  };

  const attachWarmup = () => {
    const host = document.createElement('div');
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = [
      'position:fixed',
      'left:0',
      'top:0',
      'width:1px',
      'height:1px',
      'overflow:hidden',
      'pointer-events:none',
      'opacity:0.001',
      'z-index:0',
      'contain:strict',
    ].join(';');

    urls.forEach((src) => {
      const tile = document.createElement('div');
      tile.style.position = 'absolute';
      tile.style.inset = '0';
      tile.style.width = '1px';
      tile.style.height = '1px';
      tile.style.backgroundImage = `url("${src.replace(/"/g, '\\"')}")`;
      tile.style.backgroundPosition = 'center';
      tile.style.backgroundRepeat = 'no-repeat';
      tile.style.backgroundSize = 'cover';
      host.appendChild(tile);
    });

    document.body.appendChild(host);
    return () => removeWarmup(host);
  };

  if (document.body) {
    return attachWarmup();
  }

  let release = () => {};
  document.addEventListener('DOMContentLoaded', () => {
    release = attachWarmup();
  }, { once: true });
  return () => release();
}

function preloadMaterialTextures() {
  if (materialTexturePreloadPromise || typeof Image === 'undefined') {
    return materialTexturePreloadPromise;
  }

  const textureUrls = getMaterialTextureUrls();
  const releaseWarmup = warmPaintMaterialTextures(textureUrls);

  materialTexturePreloadPromise = Promise.allSettled(textureUrls.map((src) => (
    new Promise((resolve) => {
      const image = new Image();
      image.decoding = 'async';
      image.onload = () => {
        if (typeof image.decode === 'function') {
          image.decode().then(resolve, resolve);
          return;
        }
        resolve();
      };
      image.onerror = resolve;
      image.src = src;
    })
  ))).finally(releaseWarmup);

  return materialTexturePreloadPromise;
}

preloadMaterialTextures();

const AUTO_SYNC_OPTIMISTIC_TTL_MS = 2 * 60 * 1000;
const AUTO_SYNC_INFLIGHT_STORAGE_KEY = 'breaktwenty_auto_sync_inflight_v1';
const AUTO_SYNC_INFLIGHT_TTL_MS = 15 * 60 * 1000;
const SYNC_BATCH_DATA_STATUSES = new Set([
  'ok',
  'skipped',
  'auth_required',
  'different_profile_detected',
  'network_error',
  'error',
  'error_flex',
  'error_scraper',
  'already_syncing',
]);
const INITIAL_DATA_RETRY_BASE_MS = 2000;
const INITIAL_DATA_RETRY_MAX_MS = 10000;
const SIDEBAR_EXPANDED_STORAGE_KEY = 'breaktwenty_sidebar_expanded_v1';
const SUPPORT_LINKS = [
  {
    label: 'Support on Patreon',
    href: 'https://www.patreon.com/cw/BreakTwenty',
    icon: FaPatreon,
  },
  {
    label: 'Support on Ko-fi',
    href: 'https://ko-fi.com/breaktwenty',
    icon: SiKofi,
  },
];
const BRAND_ASSET_VERSION_MANIFEST_SRC = '/assets/brand/brand-asset-versions.json';
const GLOBAL_TIMEFRAME_STORAGE_KEY = 'breaktwenty_global_timeframe_v1';
const GLOBAL_TIMEFRAME_CUSTOM_RANGE_STORAGE_KEY = 'breaktwenty_global_timeframe_custom_range_v1';
const ACTIVE_SYNC_STATUSES = new Set(['queued', 'running']);

function normalizeBrandAssetVersions(value) {
  if (!value || typeof value !== 'object') return brandAssetVersions;
  return {
    darkLogo: String(value.darkLogo || brandAssetVersions.darkLogo || '0'),
    lightLogo: String(value.lightLogo || brandAssetVersions.lightLogo || '0'),
    darkWordmark: String(value.darkWordmark || brandAssetVersions.darkWordmark || '0'),
    lightWordmark: String(value.lightWordmark || brandAssetVersions.lightWordmark || '0'),
  };
}

function brandAssetVersionsEqual(a, b) {
  return (
    a.darkLogo === b.darkLogo &&
    a.lightLogo === b.lightLogo &&
    a.darkWordmark === b.darkWordmark &&
    a.lightWordmark === b.lightWordmark
  );
}

const brandAssetVersionListeners = new Set();

if (import.meta.hot) {
  void import.meta.hot.accept('./generatedBrandAssets', (nextModule) => {
    const normalized = normalizeBrandAssetVersions(nextModule?.brandAssetVersions);
    brandAssetVersionListeners.forEach((listener) => listener(normalized));
  });
}

function subscribeBrandAssetVersionUpdates(listener) {
  brandAssetVersionListeners.add(listener);
  return () => brandAssetVersionListeners.delete(listener);
}

class BreakTwentyErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, errorInfo) {
    console.error(`${APP_BRAND_NAME} renderer error:`, error, errorInfo);
  }

  componentDidUpdate(prevProps) {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render() {
    const { error } = this.state;
    if (!error) {
      return this.props.children;
    }

    return (
      <div className="app-error-state" role="alert">
        <h2><BrandName /> hit a renderer error.</h2>
        <p>
          The desktop shell and backend are still running, but the current view could not render.
          Check the desktop log for the captured browser error.
        </p>
        <pre>{String(error?.message || error || 'Unknown renderer error')}</pre>
      </div>
    );
  }
}

function RouteLoadingState() {
  return (
    <div className="app-route-loading" role="status">
      Loading view...
    </div>
  );
}

function AppBrandLoadingState() {
  return (
    <div className="app loading-screen" role="status" aria-label={`${APP_BRAND_NAME} is loading`}>
      <img
        className="loading-screen-logo"
        src={INITIAL_LOADING_BRAND_ASSETS.mark.src}
        alt=""
      />
      <img
        className="loading-screen-wordmark"
        src={INITIAL_LOADING_BRAND_ASSETS.wordmark.src}
        srcSet={INITIAL_LOADING_BRAND_ASSETS.wordmark.srcSet}
        alt={APP_BRAND_NAME}
      />
      <p className="loading-screen-catchphrase">{LOADING_CATCHPHRASE}</p>
      <span className="loading-screen-spinner" aria-hidden="true" />
    </div>
  );
}

function readStoredDashboardCustomDateRange() {
  try {
    const saved = window.localStorage.getItem(GLOBAL_TIMEFRAME_CUSTOM_RANGE_STORAGE_KEY);
    if (!saved) return { start: '', end: '' };
    const parsed = JSON.parse(saved);
    return {
      start: typeof parsed?.start === 'string' ? parsed.start : '',
      end: typeof parsed?.end === 'string' ? parsed.end : '',
    };
  } catch (_) {
    return { start: '', end: '' };
  }
}

function isCompleteCustomDateRange(range) {
  return Boolean(range?.start && range?.end);
}

function acquireAutoSyncLease() {
  const now = Date.now();
  const token = `${now}:${Math.random().toString(36).slice(2)}`;
  try {
    const existing = String(localStorage.getItem(AUTO_SYNC_INFLIGHT_STORAGE_KEY) || '');
    const existingStartedAt = Number(existing.split(':')[0] || '0');
    if (
      existingStartedAt > 0
      && Number.isFinite(existingStartedAt)
      && now - existingStartedAt < AUTO_SYNC_INFLIGHT_TTL_MS
    ) {
      return null;
    }
    localStorage.setItem(AUTO_SYNC_INFLIGHT_STORAGE_KEY, token);
    return localStorage.getItem(AUTO_SYNC_INFLIGHT_STORAGE_KEY) === token ? token : null;
  } catch (_) {
    return token;
  }
}

function releaseAutoSyncLease(token) {
  if (!token) return;
  try {
    if (localStorage.getItem(AUTO_SYNC_INFLIGHT_STORAGE_KEY) === token) {
      localStorage.removeItem(AUTO_SYNC_INFLIGHT_STORAGE_KEY);
    }
  } catch (_) {}
}

function getActiveTransactionImportKeysFromPayload(payload) {
  const institutions = Array.isArray(payload?.institutions) ? payload.institutions : [];
  const seen = new Set();

  institutions.forEach((institution) => {
    const currentFetchStatus = String(institution?.current_fetch_status || '').trim().toLowerCase();
    const jobStatus = String(institution?.transaction_import_job_status || '').trim().toLowerCase();
    if (!ACTIVE_SYNC_STATUSES.has(currentFetchStatus) && !ACTIVE_SYNC_STATUSES.has(jobStatus)) {
      return;
    }

    const provider = String(institution?.provider || '').trim();
    const label = String(
      institution?.institution
      || getProviderDisplayName(provider)
      || 'Institution'
    ).trim();
    const key = `${provider || 'institution'}:${institution?.institution_id || label}`;
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
  });

  return seen;
}

function getActiveProviderActivityItems(syncActivity) {
  const activities = Array.isArray(syncActivity?.active) ? syncActivity.active : [];
  return activities
    .filter((activity) => ACTIVE_SYNC_STATUSES.has(String(activity?.status || '').trim().toLowerCase()))
    .map((activity) => {
      const provider = String(activity?.provider || '').trim();
      const label = String(activity?.label || getProviderDisplayName(provider) || 'Institution').trim();
      const isTransactionImport = activity?.kind === 'transaction_import';
      return {
        key: String(activity.activity_id),
        provider,
        institutionId: activity?.institution_id || null,
        label,
        tooltip: isTransactionImport
          ? `Fetching ${label} transactions`
          : `Syncing ${label}`,
      };
    })
    .sort((left, right) => left.label.localeCompare(right.label));
}

// Surface providers the client already knows are syncing (optimistic auto-sync state)
// so the rail notifier appears immediately instead of waiting for the first
// server-sent sync_activity event. Server-confirmed items win on provider conflicts.
function mergeOptimisticSyncActivities(activityItems, autoSyncStates, accountSyncActivities = []) {
  const items = Array.isArray(activityItems) ? activityItems : [];
  const seenConnections = new Set(
    items.map((item) => String(item.institutionId || item.provider || '').trim()).filter(Boolean)
  );
  const optimistic = [];
  Object.entries(autoSyncStates || {}).forEach(([connectionKey, state]) => {
    const normalizedProvider = String(state?.provider || '').trim();
    const identity = String(state?.institutionId || connectionKey || '').trim();
    const status = String(state?.status || '').trim().toLowerCase();
    if (!normalizedProvider || !identity || seenConnections.has(identity)) {
      return;
    }
    if (status !== 'syncing' && !ACTIVE_SYNC_STATUSES.has(status)) {
      return;
    }
    seenConnections.add(identity);
    const label = String(state?.label || getProviderDisplayName(normalizedProvider) || 'Institution').trim();
    optimistic.push({
      key: `optimistic:${identity}`,
      provider: normalizedProvider,
      institutionId: state?.institutionId || null,
      label,
      tooltip: `Syncing ${label}`,
    });
  });
  (Array.isArray(accountSyncActivities) ? accountSyncActivities : []).forEach((activity) => {
    const identity = String(activity?.institutionId || activity?.provider || '').trim();
    if (!identity || seenConnections.has(identity)) return;
    seenConnections.add(identity);
    optimistic.push(activity);
  });
  return [...items, ...optimistic].sort((left, right) => left.label.localeCompare(right.label));
}

function getTransactionImportStatusForSignature(transactionImportStatus) {
  if (!transactionImportStatus || typeof transactionImportStatus !== 'object' || Array.isArray(transactionImportStatus)) {
    return transactionImportStatus;
  }

  const signaturePayload = { ...transactionImportStatus };
  delete signaturePayload.generated_at;
  return signaturePayload;
}

function getTransactionImportStatusSignature(transactionImportStatus) {
  return JSON.stringify(getTransactionImportStatusForSignature(transactionImportStatus));
}

function getDataSignature(nextData) {
  return JSON.stringify({
    ...nextData,
    transactionImportStatus: getTransactionImportStatusForSignature(nextData?.transactionImportStatus),
  });
}

function getSyncBatchDataStatus(result) {
  const status = String(result?.status || '').trim().toLowerCase();
  if (status === 'skipped') return 'ok';
  return SYNC_BATCH_DATA_STATUSES.has(status) ? status : '';
}

function applySyncBatchStatusesToInstitutions(institutions, statusByInstitutionId) {
  if (!Array.isArray(institutions) || statusByInstitutionId.size === 0) {
    return institutions;
  }
  let changed = false;
  const nextInstitutions = institutions.map((institution) => {
    const nextStatus = statusByInstitutionId.get(Number(institution?.id || 0));
    if (!nextStatus || institution.sync_status === nextStatus) {
      return institution;
    }
    changed = true;
    return { ...institution, sync_status: nextStatus };
  });
  return changed ? nextInstitutions : institutions;
}

function applySyncBatchResultsToAppData(previousData, batch) {
  const results = Object.values(batch?.results || {});
  if (results.length === 0) return previousData;
  const statusByInstitutionId = new Map();
  let nextAccounts = previousData.accounts;
  results.forEach((result) => {
    const institutionId = Number(result?.institution_id || 0);
    if (institutionId <= 0) return;
    const nextStatus = getSyncBatchDataStatus(result);
    if (nextStatus) {
      statusByInstitutionId.set(institutionId, nextStatus);
    }
    if (nextStatus === 'ok' && Array.isArray(result?.accounts)) {
      nextAccounts = mergeInstitutionAccounts(nextAccounts, institutionId, result.accounts);
    }
  });
  const nextInstitutions = applySyncBatchStatusesToInstitutions(
    previousData.institutions,
    statusByInstitutionId,
  );
  const nextAllInstitutions = applySyncBatchStatusesToInstitutions(
    previousData.allInstitutions,
    statusByInstitutionId,
  );
  if (
    nextAccounts === previousData.accounts
    && nextInstitutions === previousData.institutions
    && nextAllInstitutions === previousData.allInstitutions
  ) {
    return previousData;
  }
  return {
    ...previousData,
    accounts: nextAccounts,
    institutions: nextInstitutions,
    allInstitutions: nextAllInstitutions,
  };
}

function getSyncActivitySignature(syncActivity) {
  const activities = Array.isArray(syncActivity?.active) ? syncActivity.active : [];
  return JSON.stringify(activities);
}

const APP_SYNC_FAILURE_OPTIONS = { fallback: 'Sync failed' };

async function persistInitialUserTimezone(userTimezone, userTimeFormat) {
  const normalizedTimezone = String(userTimezone || '').trim();
  if (!normalizedTimezone) {
    return false;
  }
  try {
    const resp = await fetch(`${API}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_timezone: normalizedTimezone,
        user_time_format: normalizeUserTimeFormat(userTimeFormat),
      }),
    });
    return resp.ok;
  } catch (_) {
    // The next startup will try again if the first-run settings save failed.
    return false;
  }
}

const PRIMARY_NAV_ITEMS = [
  {
    to: '/',
    end: true,
    label: 'Dashboard',
    icon: DashboardSidebarIcon,
  },
  {
    to: '/accounts',
    label: 'Accounts',
    icon: AccountsSidebarIcon,
  },
  {
    to: '/cash-flow',
    label: 'Cash Flow',
    icon: CashFlowSidebarIcon,
  },
  {
    to: '/holdings',
    label: 'Investments',
    icon: InvestmentsSidebarIcon,
  },
  {
    to: '/transactions',
    label: 'Transactions',
    icon: TransactionsSidebarIcon,
  },
];

const UTILITY_NAV_ITEMS = [
  {
    to: '/faq',
    label: 'Help',
    icon: FaqSidebarIcon,
    activePaths: ['/faq', '/licenses'],
  },
  {
    // No `end`: stays active across the whole /settings/* section, including
    // the Categories sub-page.
    to: '/settings',
    label: 'Settings',
    icon: SettingsSidebarIcon,
  },
];

function PngMaskSidebarIcon({ asset, className, iconClassName }) {
  return (
    <span
      className={`${className} floating-nav-icon-mask ${iconClassName}`}
      style={{ '--floating-nav-icon-mask-image': `url(${asset})` }}
      aria-hidden="true"
    />
  );
}

function DashboardSidebarIcon({ className }) {
  return (
    <PngMaskSidebarIcon
      asset={DashboardIconPng}
      className={className}
      iconClassName="floating-nav-icon-dashboard"
    />
  );
}

function AccountsSidebarIcon({ className }) {
  return (
    <AccountsIconSvg
      className={`${className} floating-nav-icon-accounts`}
      aria-hidden="true"
      focusable="false"
    />
  );
}

function InvestmentsSidebarIcon({ className }) {
  return (
    <InvestmentsIconSvg
      className={`${className} floating-nav-icon-investments`}
      aria-hidden="true"
      focusable="false"
    />
  );
}

function TransactionsSidebarIcon({ className }) {
  return (
    <TransactionsIconSvg
      className={`${className} floating-nav-icon-transactions`}
      aria-hidden="true"
      focusable="false"
    />
  );
}

function FaqSidebarIcon({ className }) {
  return (
    <FaqIconSvg
      className={`${className} floating-nav-icon-faq`}
      aria-hidden="true"
      focusable="false"
    />
  );
}

function SettingsSidebarIcon({ className }) {
  return (
    <SettingsIconSvg
      className={`${className} floating-nav-icon-settings`}
      aria-hidden="true"
      focusable="false"
    />
  );
}

function DiagnosticsSidebarIcon({ className }) {
  return (
    <DiagnosticsIconSvg
      className={`${className} floating-nav-icon-diagnostics`}
      aria-hidden="true"
      focusable="false"
    />
  );
}

function CashFlowSidebarIcon({ className }) {
  return (
    <PngMaskSidebarIcon
      asset={CashFlowIconPng}
      className={className}
      iconClassName="floating-nav-icon-cash-flow"
    />
  );
}

function AddNetWorthSidebarIcon({ className }) {
  return (
    <PngMaskSidebarIcon
      asset={AddNetWorthIconPng}
      className={className}
      iconClassName="floating-nav-icon-add-net-worth"
    />
  );
}

function AddTransactionSidebarIcon({ className }) {
  return (
    <PngMaskSidebarIcon
      asset={AddTransactionIconPng}
      className={className}
      iconClassName="floating-nav-icon-add-transaction"
    />
  );
}

function SidebarToggleIcon({ expanded }) {
  return <TriangleIcon direction={expanded ? 'left' : 'right'} className="floating-nav-toggle-icon" />;
}

const THEME_MODE_ICON_PATHS = Object.freeze({
  dark: 'M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z',
  light: 'M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zM2 13h2c.55 0 1-.45 1-1s-.45-1-1-1H2c-.55 0-1 .45-1 1s.45 1 1 1zm18 0h2c.55 0 1-.45 1-1s-.45-1-1-1h-2c-.55 0-1 .45-1 1s.45 1 1 1zM11 2v2c0 .55.45 1 1 1s1-.45 1-1V2c0-.55-.45-1-1-1s-1 .45-1 1zm0 18v2c0 .55.45 1 1 1s1-.45 1-1v-2c0-.55-.45-1-1-1s-1 .45-1 1zM5.99 4.58a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41L5.99 4.58zm12.37 12.37a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0a.996.996 0 0 0 0-1.41l-1.06-1.06zm1.06-10.96a.996.996 0 0 0 0-1.41.996.996 0 0 0-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06zM7.05 18.36a.996.996 0 0 0 0-1.41.996.996 0 0 0-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06z',
});

function ThemeModeIcon({ mode }) {
  return (
    <svg
      className={`app-theme-mode-icon app-theme-mode-icon-${mode}`.trim()}
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
    >
      <path d={THEME_MODE_ICON_PATHS[mode] || THEME_MODE_ICON_PATHS.dark} />
    </svg>
  );
}

function FloatingNavItem({ to, end, icon: Icon, label, sidebarExpanded = false, activePaths = [] }) {
  const location = useLocation();

  return (
    <NavLink
      to={to}
      end={end}
      aria-label={label}
      data-tooltip={sidebarExpanded ? null : label}
      data-tooltip-placement="right"
      data-tour-id={to}
      className={({ isActive }) => {
        const isSectionActive = isActive || activePaths.includes(location.pathname);
        return `floating-nav-btn ${isSectionActive ? 'is-active' : ''}`.trim();
      }}
    >
      <span className="floating-nav-btn-icon">
        <Icon className="floating-nav-icon-graphic" />
      </span>
      <span className="floating-nav-label">{label}</span>
    </NavLink>
  );
}

function FloatingNavAction({ icon: Icon, label, onClick, className = '', sidebarExpanded = false }) {
  return (
    <button
      type="button"
      aria-label={label}
      data-tooltip={sidebarExpanded ? null : label}
      data-tooltip-placement="right"
      className={`floating-nav-btn floating-nav-action-btn ${className}`.trim()}
      onClick={onClick}
    >
      <span className="floating-nav-btn-icon">
        <Icon className="floating-nav-icon-graphic" />
      </span>
      <span className="floating-nav-label">{label}</span>
    </button>
  );
}

function FloatingNavExternalLink({ icon: Icon, label, href, sidebarExpanded = false }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      aria-label={label}
      data-tooltip={sidebarExpanded ? null : label}
      data-tooltip-placement="right"
      className="floating-nav-btn floating-nav-support-link"
    >
      <span className="floating-nav-btn-icon">
        <Icon className="floating-nav-icon-graphic" />
      </span>
      <span className="floating-nav-label">{label}</span>
    </a>
  );
}

function FloatingNavSupportLinks({ sidebarExpanded = false }) {
  return (
    <div
      className="floating-nav-group floating-nav-group-support"
      role="group"
      aria-label="Support BreakTwenty"
    >
      {SUPPORT_LINKS.map((item) => (
        <FloatingNavExternalLink key={item.label} {...item} sidebarExpanded={sidebarExpanded} />
      ))}
    </div>
  );
}

function hasSidebarUpdateNotification(status) {
  return status?.enabled !== false && ['available', 'downloaded'].includes(status?.status);
}

function ToolbarChip({ label, accent = false }) {
  return (
    <span className={`app-toolbar-status-chip app-control-root ${accent ? 'is-accent' : ''}`.trim()}>
      <span className="app-control-label">{label}</span>
    </span>
  );
}

function AppThemeToggle({ themeMode, onThemeModeChange }) {
  const isDarkMode = themeMode === 'dark';

  return (
    <div
      className={`app-theme-toggle ${isDarkMode ? 'is-dark' : 'is-light'}`}
      role="group"
      aria-label="App theme"
      data-app-non-dismiss-interaction
    >
      <span className="app-theme-toggle-thumb" aria-hidden="true">
        <ThemeModeIcon mode={isDarkMode ? 'dark' : 'light'} />
      </span>
      <button
        type="button"
        className={`app-theme-toggle-btn app-control-root ${isDarkMode ? 'is-active' : ''}`.trim()}
        aria-label="Use dark theme"
        aria-pressed={isDarkMode}
        data-tooltip="Dark theme"
        data-tooltip-hover-only
        onClick={() => onThemeModeChange('dark')}
      >
        <span className="app-control-icon" aria-hidden="true">
          <ThemeModeIcon mode="dark" />
        </span>
      </button>
      <button
        type="button"
        className={`app-theme-toggle-btn app-control-root ${!isDarkMode ? 'is-active' : ''}`.trim()}
        aria-label="Use light theme"
        aria-pressed={!isDarkMode}
        data-tooltip="Light theme"
        data-tooltip-hover-only
        onClick={() => onThemeModeChange('light')}
      >
        <span className="app-control-icon" aria-hidden="true">
          <ThemeModeIcon mode="light" />
        </span>
      </button>
    </div>
  );
}

function DevCaptureToggle({ enabled, onChange }) {
  return (
    <button
      type="button"
      className={`support-logs-dev-toggle-switch ${enabled ? 'is-on' : 'is-off'}`}
      aria-label={enabled ? 'Turn dev capture off' : 'Turn dev capture on'}
      aria-pressed={enabled}
      data-app-non-dismiss-interaction
      data-tooltip={enabled ? 'Dev capture on' : 'Dev capture off'}
      data-tooltip-hover-only
      onClick={() => onChange(!enabled)}
    >
      <span className="support-logs-dev-toggle-thumb" aria-hidden="true" />
    </button>
  );
}

function TransactionImportRailNotifier({ activeImports }) {
  const [expandedActivityKey, setExpandedActivityKey] = useState('');
  const [popoutPosition, setPopoutPosition] = useState(null);
  const toggleRef = useRef(null);
  const activeCount = activeImports.length;
  const activeActivityKey = activeImports
    .map((item) => item.key || item.provider || item.label)
    .join('|') || String(activeCount);
  const isExpanded = activeCount > 0 && expandedActivityKey === activeActivityKey;

  useLayoutEffect(() => {
    if (!isExpanded) {
      return undefined;
    }

    let frameId = null;
    const updatePopoutPosition = () => {
      const rect = toggleRef.current?.getBoundingClientRect();
      if (!rect) return;
      const left = rect.right + 8;
      setPopoutPosition({
        left,
        top: rect.top + rect.height / 2,
        minHeight: rect.height,
        maxWidth: Math.max(0, window.innerWidth - left - 16),
      });
    };
    const schedulePopoutPosition = () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      frameId = window.requestAnimationFrame(() => {
        frameId = null;
        updatePopoutPosition();
      });
    };

    schedulePopoutPosition();
    window.addEventListener('resize', schedulePopoutPosition);
    window.addEventListener('scroll', schedulePopoutPosition, true);
    window.visualViewport?.addEventListener('resize', schedulePopoutPosition);
    return () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      window.removeEventListener('resize', schedulePopoutPosition);
      window.removeEventListener('scroll', schedulePopoutPosition, true);
      window.visualViewport?.removeEventListener('resize', schedulePopoutPosition);
    };
  }, [isExpanded]);

  const singleImport = activeCount === 1 ? activeImports[0] : null;
  const tooltip = singleImport
    ? singleImport.tooltip
    : `Syncing ${activeCount} institutions`;

  if (activeCount === 0) {
    return <div className="transaction-import-rail-notifier is-idle" aria-hidden="true" />;
  }

  return (
    <div className={`transaction-import-rail-notifier ${isExpanded ? 'is-expanded' : ''}`.trim()}>
      <button
        ref={toggleRef}
        type="button"
        className="transaction-import-rail-toggle"
        // The app-wide GlobalTooltip (data-tooltip) renders into a body-level
        // portal and clamps to the viewport, so the label never runs off the
        // left edge of the screen the way the old pure-CSS pill did. Suppress
        // it while expanded — the provider chips carry their own labels then.
        data-tooltip={isExpanded ? null : tooltip}
        aria-label={`${tooltip}. ${isExpanded ? 'Collapse provider list' : 'Show provider list'}`}
        aria-expanded={isExpanded}
        onClick={() => setExpandedActivityKey((current) => (
          current === activeActivityKey ? '' : activeActivityKey
        ))}
      >
        <span className="transaction-import-rail-content">
          {singleImport ? (
            <InstitutionLogo name={singleImport.label} size={24} />
          ) : (
            <span className="transaction-import-rail-count">{activeCount}</span>
          )}
          <span className="transaction-import-rail-ring" aria-hidden="true" />
        </span>
      </button>
      {isExpanded && popoutPosition && createPortal(
        <div
          className="transaction-import-rail-popout"
          role="list"
          aria-label="Institutions syncing"
          style={{
            left: `${popoutPosition.left}px`,
            top: `${popoutPosition.top}px`,
            minHeight: `${popoutPosition.minHeight}px`,
            maxWidth: `${popoutPosition.maxWidth}px`,
          }}
        >
          {activeImports.map((item) => (
            <span
              key={item.key}
              className="transaction-import-provider-chip"
              role="listitem"
              data-tooltip={item.tooltip}
              aria-label={item.tooltip}
            >
              <InstitutionLogo name={item.label} size={24} />
            </span>
          ))}
        </div>,
        document.body
      )}
    </div>
  );
}

const DASHBOARD_TIMEZONE_LABELS = {
  EDT: 'ET',
  EST: 'ET',
  'Eastern Time': 'ET',
  'Eastern Standard Time': 'ET',
  'Eastern Daylight Time': 'ET',
  CDT: 'CT',
  CST: 'CT',
  'Central Time': 'CT',
  'Central Standard Time': 'CT',
  'Central Daylight Time': 'CT',
  MDT: 'MT',
  MST: 'MT',
  'Mountain Time': 'MT',
  'Mountain Standard Time': 'MT',
  'Mountain Daylight Time': 'MT',
  PDT: 'PT',
  PST: 'PT',
  'Pacific Time': 'PT',
  'Pacific Standard Time': 'PT',
  'Pacific Daylight Time': 'PT',
  ADT: 'AT',
  AST: 'AT',
  'Atlantic Time': 'AT',
  'Atlantic Standard Time': 'AT',
  'Atlantic Daylight Time': 'AT',
  NDT: 'NT',
  NST: 'NT',
  'Newfoundland Time': 'NT',
  'Newfoundland Standard Time': 'NT',
  'Newfoundland Daylight Time': 'NT',
};

function formatDashboardTimezoneLabel(value, userTimezone) {
  const normalizedValue = String(value || '').replace(/\s+/g, ' ').trim();
  const normalizedTimezone = String(userTimezone || '').trim();
  if (normalizedValue && !normalizedTimezone.startsWith('America/')) {
    return normalizedValue;
  }
  return DASHBOARD_TIMEZONE_LABELS[normalizedValue] || normalizedValue;
}

function formatDashboardClock(
  date,
  userTimezone = DEFAULT_USER_TIMEZONE,
  userTimeFormat = DEFAULT_USER_TIME_FORMAT,
) {
  const normalizedTimeFormat = normalizeUserTimeFormat(userTimeFormat);
  const use12Hour = normalizedTimeFormat === '12h';
  const formatterOptions = {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: use12Hour ? 'numeric' : '2-digit',
    minute: '2-digit',
    timeZoneName: 'shortGeneric',
  };

  if (use12Hour) {
    formatterOptions.hour12 = true;
  } else {
    formatterOptions.hourCycle = 'h23';
  }

  let parts;

  try {
    parts = new Intl.DateTimeFormat('en-CA', {
      ...formatterOptions,
      timeZone: userTimezone || DEFAULT_USER_TIMEZONE,
    }).formatToParts(date);
  } catch {
    try {
      parts = new Intl.DateTimeFormat('en-CA', {
        ...formatterOptions,
        timeZoneName: 'short',
        timeZone: userTimezone || DEFAULT_USER_TIMEZONE,
      }).formatToParts(date);
    } catch {
      parts = new Intl.DateTimeFormat('en-CA', {
        ...formatterOptions,
        timeZoneName: 'short',
      }).formatToParts(date);
    }
  }

  const dateParts = parts.reduce((acc, part) => {
    acc[part.type] = part.value;
    return acc;
  }, {});
  const hourText = use12Hour && dateParts.hour ? String(Number(dateParts.hour)) : dateParts.hour;
  const periodText = use12Hour && dateParts.dayPeriod
    ? ` ${String(dateParts.dayPeriod).replace(/\./g, '').toUpperCase()}`
    : '';
  const timezoneText = formatDashboardTimezoneLabel(dateParts.timeZoneName, userTimezone);
  const timeText = `${hourText}:${dateParts.minute}${periodText}${timezoneText ? ` ${timezoneText}` : ''}`;
  const dateText = `${dateParts.weekday}, ${dateParts.month} ${dateParts.day}`;

  return {
    display: `${dateText} · ${timeText}`,
    label: `${dateText}, ${dateParts.year}, ${timeText}`,
  };
}

function DashboardClock({ userTimezone, userTimeFormat, frozenNow = null }) {
  const [liveNow, setLiveNow] = useState(() => new Date());

  useEffect(() => {
    if (frozenNow) {
      return undefined;
    }
    let timerId;
    let intervalId;

    const scheduleNextMinute = () => {
      const msUntilNextMinute = 60000 - (Date.now() % 60000);
      timerId = window.setTimeout(() => {
        setLiveNow(new Date());
        intervalId = window.setInterval(() => {
          setLiveNow(new Date());
        }, 60000);
      }, msUntilNextMinute);
    };

    scheduleNextMinute();

    return () => {
      window.clearTimeout(timerId);
      window.clearInterval(intervalId);
    };
  }, [frozenNow]);

  const now = frozenNow ? new Date(frozenNow) : liveNow;
  const clock = formatDashboardClock(now, userTimezone, userTimeFormat);

  return (
    <div className="dashboard-date-chip app-control-root" aria-label={clock.label}>
      <span className="dashboard-date-chip-icon app-control-icon" aria-hidden="true">
        <MdAccessTime />
      </span>
      <span className="dashboard-date-chip-text app-control-label">{clock.display}</span>
    </div>
  );
}

function formatDashboardTimelineDate(value) {
  if (!value) return '';
  const [year, month, day] = value.split('-').map(Number);
  const parsed = year && month && day ? new Date(year, month - 1, day) : null;
  if (!parsed || Number.isNaN(parsed.getTime())) return value;

  return parsed.toLocaleDateString('en-CA', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function getDashboardTimeframeSummary(timeframe, customDateRange) {
  if (timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY) {
    const { start, end } = customDateRange || {};
    if (start && end) return `${formatDashboardTimelineDate(start)} - ${formatDashboardTimelineDate(end)}`;
    if (start) return `From ${formatDashboardTimelineDate(start)}`;
    if (end) return `Until ${formatDashboardTimelineDate(end)}`;
    return 'Custom Range';
  }

  return PORTFOLIO_TIMEFRAMES.find((preset) => preset.label === timeframe)?.triggerLabel || 'Timeline';
}

function DashboardTimelineControl({
  timeframe,
  customDateRange,
  onTimeframeChange,
  onCustomDateRangeChange,
}) {
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef(null);
  const summary = getDashboardTimeframeSummary(timeframe, customDateRange);
  const {
    isCustomCommitted,
    isCustomSelected,
    openCustomRangeDraft,
    clearCustomRangeDraft,
  } = useTimelineCustomRangeDraft({
    isOpen,
    committedKey: timeframe,
    customKey: PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
  });
  const closeMenu = useCallback(() => {
    clearCustomRangeDraft();
    setIsOpen(false);
  }, [clearCustomRangeDraft]);

  useDismissibleLayer({
    open: isOpen,
    ref: menuRef,
    onDismiss: closeMenu,
  });

  const handleSelectTimeframe = (nextTimeframe) => {
    if (nextTimeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY) {
      openCustomRangeDraft();
      return;
    }

    onTimeframeChange(nextTimeframe);
    clearCustomRangeDraft();
    setIsOpen(false);
  };

  const handleApplyCustomDateRange = (nextRange) => {
    onCustomDateRangeChange(nextRange);
    onTimeframeChange(PORTFOLIO_CUSTOM_TIMEFRAME_KEY);
    clearCustomRangeDraft();
    setIsOpen(false);
  };

  const handleCancelCustomDateRange = () => {
    clearCustomRangeDraft();
    setIsOpen(false);
  };

  return (
    <div className="investments-filter-popover dashboard-timeline-popover" ref={menuRef}>
      <TimelineTrigger
        isOpen={isOpen}
        summary={summary}
        controls="dashboard-timeline-filter-panel"
        className="dashboard-timeline-trigger"
        ariaLabel="Dashboard timeline"
        showSummaryTitle={false}
        onClick={() => setIsOpen((previous) => !previous)}
      />

      <div
        id="dashboard-timeline-filter-panel"
        role="dialog"
        aria-label="Dashboard timeline filters"
        className={`investments-filter-panel dashboard-timeline-panel timeline-range-panel ${isOpen ? 'is-open' : ''}`.trim()}
        aria-hidden={!isOpen}
      >
        <div className="income-timeline-menu-list" role="menu" aria-label="Dashboard timeline ranges">
          {PORTFOLIO_TIMEFRAMES.map((preset) => {
            const isSelected = timeframe === preset.label;

            return (
              <button
                key={preset.label}
                type="button"
                role="menuitemradio"
                aria-checked={isSelected}
                className={`income-timeline-menu-item ${isSelected ? 'is-selected' : ''}`.trim()}
                onClick={() => handleSelectTimeframe(preset.label)}
              >
                <span className="income-timeline-menu-item-copy">
                  <span className="income-timeline-menu-item-label">{preset.menuLabel}</span>
                </span>
                <span className={`income-timeline-checkbox ${isSelected ? 'is-selected' : ''}`.trim()} aria-hidden="true">
                  {isSelected ? <MdCheck size={14} /> : null}
                </span>
              </button>
            );
          })}

          <button
            type="button"
            role="menuitemradio"
            aria-checked={isCustomCommitted}
            className={`income-timeline-menu-item ${isCustomSelected ? 'is-selected' : ''}`.trim()}
            onClick={() => handleSelectTimeframe(PORTFOLIO_CUSTOM_TIMEFRAME_KEY)}
          >
            <span className="income-timeline-menu-item-copy">
              <span className="income-timeline-menu-item-label">Custom Range</span>
            </span>
            <span className={`income-timeline-checkbox ${isCustomCommitted ? 'is-selected' : ''}`.trim()} aria-hidden="true">
              {isCustomCommitted ? <MdCheck size={14} /> : null}
            </span>
          </button>
        </div>

        {isOpen && isCustomSelected ? (
          <div
            className="timeline-range-picker-popover is-open"
            aria-label="Custom dashboard timeline range"
          >
            <TimelineCustomRangePicker
              startDate={timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY ? customDateRange.start : ''}
              endDate={timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY ? customDateRange.end : ''}
              onApply={handleApplyCustomDateRange}
              onCancel={handleCancelCustomDateRange}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

const EXPORT_EVERYTHING_RECENT_MINUTES = 15;
const SUPPORT_DIAGNOSTICS_REFRESH_INTERVAL_MS = 3000;
const SUPPORT_LOGS_PICKER_MENU_WIDTH_REM = 22;
const SUPPORT_LOGS_PICKER_MENU_EDGE_GAP = 8;
const SUPPORT_LOGS_PICKER_MENU_ANCHOR_GAP = 8;
const DEV_CAPTURE_STORAGE_KEY = 'breaktwenty.devCaptureOn';

const RUN_TRIGGER_LABELS = {
  sync_result_ok: 'Synced',
  sync_result_skipped: 'Skipped',
  sync_result_auth_required: "Couldn't sign in",
  sync_result_network_error: "Couldn't connect",
  sync_result_error: 'Sync error',
  sync_result_different_profile_detected: 'Different account detected',
  sync_exception: 'Sync crashed',
  tximport_task_crashed: 'Background task crashed',
  tximport_stale_running_requeued: 'Background task stuck — retried',
  tximport_stale_running_failed: 'Background task stuck — gave up',
  tximport_orphan_queued_revived: 'Background task lost — recovered',
  tximport_mark_job_failed: 'Background task failed',
  tximport_mark_job_auth_required: 'Background task needs sign-in',
};

function formatRunTrigger(trigger) {
  const key = String(trigger || '').trim();
  if (!key) return 'Sync';
  if (RUN_TRIGGER_LABELS[key]) return RUN_TRIGGER_LABELS[key];
  if (key.startsWith('tximport_')) return 'Background task issue';
  if (key.startsWith('sync_result_')) return 'Sync issue';
  return 'Sync';
}

function _runTriggerSeverity(trigger) {
  const key = String(trigger || '').trim();
  if (!key) return 0;
  if (key === 'sync_result_ok' || key === 'sync_result_skipped') return 0;
  if (key === 'sync_result_auth_required' || key === 'tximport_mark_job_auth_required') return 2;
  if (key === 'sync_exception' || key === 'tximport_task_crashed') return 4;
  if (key.startsWith('tximport_')) return 3;
  return 3;
}

function groupRunsBySyncId(runs) {
  const groups = new Map();
  const standalone = [];
  (runs || []).forEach((run) => {
    const syncId = String(run?.sync_id || '').trim();
    if (!syncId) {
      standalone.push({ key: `__run__:${run.run_id}`, syncId: null, archives: [run] });
      return;
    }
    if (!groups.has(syncId)) {
      groups.set(syncId, { key: syncId, syncId, archives: [] });
    }
    groups.get(syncId).archives.push(run);
  });
  const list = [];
  groups.forEach((group) => {
    group.archives.sort(
      (a, b) =>
        Date.parse(a?.modified_at || a?.generated_at || 0)
        - Date.parse(b?.modified_at || b?.generated_at || 0),
    );
    list.push(group);
  });
  list.push(...standalone);
  list.forEach((group) => {
    const primary = group.archives[0] || null;
    group.primaryRun = primary;
    group.primaryTimestamp = primary?.modified_at || primary?.generated_at || '';
    const worst = group.archives.reduce(
      (acc, run) => (_runTriggerSeverity(run?.trigger) > _runTriggerSeverity(acc?.trigger) ? run : acc),
      primary,
    );
    group.worstRun = worst;
    group.label = formatRunTrigger(worst?.trigger);
  });
  list.sort(
    (a, b) =>
      Date.parse(b.primaryTimestamp || 0) - Date.parse(a.primaryTimestamp || 0),
  );
  return list;
}

function SupportLogsProviderPicker({
  value,
  onChange,
  placeholder,
  groups,
  disabled = false,
  emptyMessage = 'No providers available.',
  ariaLabel,
}) {
  const containerRef = useRef(null);
  const triggerRef = useRef(null);
  const menuRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [menuStyle, setMenuStyle] = useState(null);
  const closePicker = useCallback(() => setOpen(false), []);
  const isOpen = open && !disabled;

  const normalizedGroups = useMemo(
    () => (groups || []).filter((group) => (group?.options || []).length > 0),
    [groups],
  );

  const selectedOption = useMemo(
    () => normalizedGroups
      .flatMap((group) => group.options || [])
      .find((option) => option.provider === value) || null,
    [normalizedGroups, value],
  );

  useDismissibleLayer({
    open: isOpen,
    refs: [containerRef, menuRef],
    onDismiss: closePicker,
    pointerEvent: 'mousedown',
  });

  const updateMenuPosition = useCallback(() => {
    if (typeof window === 'undefined' || typeof document === 'undefined' || !triggerRef.current) {
      return;
    }

    const triggerRect = triggerRef.current.getBoundingClientRect();
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    const rootFontSize = Number.parseFloat(window.getComputedStyle(document.documentElement).fontSize) || 16;
    const preferredWidth = SUPPORT_LOGS_PICKER_MENU_WIDTH_REM * rootFontSize;
    const width = Math.min(
      preferredWidth,
      Math.max(0, viewportWidth - (SUPPORT_LOGS_PICKER_MENU_EDGE_GAP * 2)),
    );
    const desiredLeft = triggerRect.right + SUPPORT_LOGS_PICKER_MENU_ANCHOR_GAP;
    const left = Math.max(
      SUPPORT_LOGS_PICKER_MENU_EDGE_GAP,
      Math.min(desiredLeft, viewportWidth - SUPPORT_LOGS_PICKER_MENU_EDGE_GAP - width),
    );
    const top = Math.max(SUPPORT_LOGS_PICKER_MENU_EDGE_GAP, triggerRect.top);

    const availableHeight = Math.max(
      0,
      viewportHeight - top - SUPPORT_LOGS_PICKER_MENU_EDGE_GAP,
    );

    setMenuStyle({
      position: 'fixed',
      top: `${top}px`,
      left: `${left}px`,
      right: 'auto',
      width: `${width}px`,
      height: `${availableHeight}px`,
      maxHeight: `${availableHeight}px`,
    });
  }, []);

  useLayoutEffect(() => {
    if (!isOpen) {
      return undefined;
    }

    updateMenuPosition();
    window.addEventListener('resize', updateMenuPosition);
    window.addEventListener('scroll', updateMenuPosition, true);
    window.visualViewport?.addEventListener('resize', updateMenuPosition);

    return () => {
      window.removeEventListener('resize', updateMenuPosition);
      window.removeEventListener('scroll', updateMenuPosition, true);
      window.visualViewport?.removeEventListener('resize', updateMenuPosition);
    };
  }, [isOpen, updateMenuPosition]);

  const hasOptions = normalizedGroups.length > 0;

  return (
    <div ref={containerRef} className={`support-logs-picker ${isOpen ? 'is-open' : ''}`.trim()}>
      <button
        type="button"
        ref={triggerRef}
        className={`support-logs-picker-trigger app-control-root ${selectedOption ? 'has-value' : ''}`.trim()}
        onClick={() => {
          if (!disabled && hasOptions) {
            setOpen((current) => !current);
          }
        }}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-label={ariaLabel}
      >
        <span className="support-logs-picker-text app-control-label">
          {selectedOption?.label || placeholder}
        </span>
        <span className="support-logs-picker-icon app-control-chevron" aria-hidden="true">
          <ControlChevron />
        </span>
      </button>

      {isOpen && menuStyle && createPortal(
        <div ref={menuRef} className="support-logs-picker-menu" style={menuStyle}>
          <div className="support-logs-picker-menu-scroll" role="listbox" aria-label={ariaLabel}>
            {hasOptions ? normalizedGroups.map((group) => (
              <div key={group.label} className="support-logs-picker-group">
                <p className="support-logs-picker-group-label">{group.label}</p>
                <div className="support-logs-picker-option-list">
                  {(group.options || []).map((option) => (
                    <button
                      key={option.provider}
                      type="button"
                      className={`support-logs-picker-option ${option.provider === value ? 'is-active' : ''}`.trim()}
                      onClick={() => {
                        onChange(option.provider);
                        setOpen(false);
                      }}
                      role="option"
                      aria-selected={option.provider === value}
                    >
                      <span className="support-logs-picker-option-label">{option.label}</span>
                      {option.meta ? (
                        <span className={`support-logs-picker-option-meta ${option.metaAccent ? 'is-accent' : ''}`.trim()}>
                          {option.meta}
                        </span>
                      ) : null}
                    </button>
                  ))}
                </div>
              </div>
            )) : (
              <div className="support-logs-empty">
                {emptyMessage}
              </div>
            )}
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

function SupportLoggingControl({
  institutions,
  userTimezone = DEFAULT_USER_TIMEZONE,
  userTimeFormat = DEFAULT_USER_TIME_FORMAT,
  onOpenChange,
  sidebarExpanded = false,
}) {
  const containerRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportingAll, setExportingAll] = useState(false);
  const [focusedProvider, setFocusedProvider] = useState('');
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [selectedRunPinned, setSelectedRunPinned] = useState(false);
  const [allRecentCount, setAllRecentCount] = useState(0);
  const [message, setMessage] = useState('');
  const [runsLoadError, setRunsLoadError] = useState('');
  const [diagnosticView, setDiagnosticView] = useState('institutions');
  const [appDiagnosticsLoading, setAppDiagnosticsLoading] = useState(false);
  const [appDiagnosticsError, setAppDiagnosticsError] = useState('');
  const [appIncidents, setAppIncidents] = useState([]);
  const [appDiagnosticsPolicy, setAppDiagnosticsPolicy] = useState(null);
  const [selectedAppIncidentId, setSelectedAppIncidentId] = useState('');
  const [exportingAppIncident, setExportingAppIncident] = useState(false);
  const [devCaptureOn, setDevCaptureOn] = useState(() => {
    if (typeof window === 'undefined') return false;
    return window.localStorage?.getItem(DEV_CAPTURE_STORAGE_KEY) === 'true';
  });
  const isDevBuild = import.meta.env.DEV;

  const setDevCaptureState = useCallback((enabled) => {
    const nextEnabled = Boolean(enabled);
    setDevCaptureOn(nextEnabled);
    if (typeof window !== 'undefined') {
      window.localStorage?.setItem(DEV_CAPTURE_STORAGE_KEY, nextEnabled ? 'true' : 'false');
    }
  }, []);

  const fetchDevCaptureLevel = useCallback(async () => {
    if (!isDevBuild) return;
    try {
      const resp = await fetch(`${API}/settings/dev-diagnostics/capture-level`);
      if (!resp.ok) return;
      const data = await resp.json();
      if (data?.status === 'ok') {
        setDevCaptureState(String(data.capture_level || 'redacted_rich') === 'developer_local');
      }
    } catch (_) {
      /* dev-only diagnostics — silent on transport errors */
    }
  }, [isDevBuild, setDevCaptureState]);

  const toggleDevCapture = useCallback(async (nextOn) => {
    if (!isDevBuild) return;
    setDevCaptureState(nextOn);
    try {
      const resp = await fetch(`${API}/settings/dev-diagnostics/capture-level`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ capture_level: nextOn ? 'developer_local' : 'redacted_rich' }),
      });
      if (!resp.ok) return;
      const data = await resp.json();
      if (data?.status === 'ok') {
        setDevCaptureState(String(data.capture_level || 'redacted_rich') === 'developer_local');
      }
    } catch (_) {
      /* dev-only diagnostics — silent on transport errors */
    }
  }, [isDevBuild, setDevCaptureState]);

  const institutionProviderKeys = useMemo(() => {
    const providerSet = new Set();
    (institutions || []).forEach((institution) => {
      const provider = String(institution?.provider || '').trim();
      if (provider) {
        providerSet.add(provider);
      }
    });
    return providerSet;
  }, [institutions]);
  const hiddenInstitutionProviderKeys = useMemo(() => {
    const providerSet = new Set();
    (institutions || []).forEach((institution) => {
      const provider = String(institution?.provider || '').trim();
      if (provider && institution?.hidden) {
        providerSet.add(provider);
      }
    });
    return providerSet;
  }, [institutions]);

  const supportProviders = useMemo(
    () => SUPPORT_LOG_PROVIDER_OPTIONS.map((option) => ({
      ...option,
      isConfigured: institutionProviderKeys.has(option.provider),
      isHidden: hiddenInstitutionProviderKeys.has(option.provider),
    })),
    [hiddenInstitutionProviderKeys, institutionProviderKeys],
  );
  const supportProviderKeySet = useMemo(
    () => new Set(supportProviders.map(({ provider }) => provider)),
    [supportProviders],
  );
  const activeFocusedProvider = supportProviderKeySet.has(focusedProvider) ? focusedProvider : '';

  const configuredSupportProviders = useMemo(
    () => supportProviders.filter(({ isConfigured }) => isConfigured),
    [supportProviders],
  );

  const availableToAddSupportProviders = useMemo(
    () => supportProviders.filter(({ isConfigured }) => !isConfigured),
    [supportProviders],
  );

  const providerLabelByKey = useMemo(
    () => supportProviders.reduce((acc, { provider, label }) => {
      acc[provider] = label;
      return acc;
    }, {}),
    [supportProviders],
  );

  const focusedProviderLabel = useMemo(
    () => providerLabelByKey[activeFocusedProvider] || '',
    [providerLabelByKey, activeFocusedProvider],
  );

  const focusedProviderConfigured = institutionProviderKeys.has(activeFocusedProvider);

  const supportProviderGroups = useMemo(
    () => [
      {
        label: 'Your institutions',
        options: configuredSupportProviders.map(({ provider, label, isHidden }) => ({
          provider,
          label,
          meta: isHidden ? 'Hidden' : undefined,
        })),
      },
      {
        label: 'Available to add',
        options: availableToAddSupportProviders.map(({ provider, label }) => ({
          provider,
          label,
          meta: 'Add flow',
          metaAccent: true,
        })),
      },
    ],
    [configuredSupportProviders, availableToAddSupportProviders],
  );

  const fetchFailedRunsPayload = useCallback(async (provider) => {
    if (!provider) {
      return { status: 'ok', runs: [] };
    }
    try {
      const params = new URLSearchParams({ provider });
      const resp = await fetch(`${API}/settings/support-logs/runs?${params.toString()}`);
      return await resp.json();
    } catch (_) {
      return { status: 'error', message: 'Failed to load diagnostic snapshots.', runs: [] };
    }
  }, []);

  const fetchAllRecentCount = useCallback(async () => {
    try {
      const resp = await fetch(`${API}/settings/support-logs/runs`);
      const data = await resp.json();
      if (data.status !== 'ok') {
        return 0;
      }
      const cutoff = Date.now() - EXPORT_EVERYTHING_RECENT_MINUTES * 60 * 1000;
      const recent = (data.runs || []).filter((run) => {
        const ts = Date.parse(run?.modified_at || run?.generated_at || '');
        return !Number.isNaN(ts) && ts >= cutoff;
      });
      return recent.length;
    } catch (_) {
      return 0;
    }
  }, []);

  const applyRunsPayload = useCallback((payload, { silent = false } = {}) => {
    if (payload.status !== 'ok') {
      const errorMessage = payload.message || 'Failed to load diagnostic snapshots.';
      setRunsLoadError(errorMessage);
      if (!silent) setMessage(errorMessage);
      return;
    }
    const list = Array.isArray(payload.runs) ? payload.runs : [];
    setRuns(list);
    setRunsLoadError('');
    setSelectedRunId((previousRunId) => {
      const stillExists = previousRunId && list.find((run) => run.run_id === previousRunId);
      if (selectedRunPinned && stillExists) {
        return previousRunId;
      }
      return list[0]?.run_id || '';
    });
    if (!silent) setMessage('');
  }, [selectedRunPinned]);

  const setPanelOpen = useCallback((nextOpen) => {
    setOpen(nextOpen);
    if (onOpenChange) onOpenChange(nextOpen);
    if (nextOpen) {
      void fetchDevCaptureLevel();
      setAppDiagnosticsLoading(true);
      if (activeFocusedProvider) setLoading(true);
    }
  }, [activeFocusedProvider, fetchDevCaptureLevel, onOpenChange]);

  const closePanel = useCallback(() => setPanelOpen(false), [setPanelOpen]);

  const applyAppDiagnosticsPayload = useCallback((payload, { silent = false } = {}) => {
    if (payload?.status !== 'ok') {
      const errorMessage = payload?.message || 'Application diagnostics could not be loaded.';
      setAppDiagnosticsError(errorMessage);
      if (!silent && diagnosticView === 'application') setMessage(errorMessage);
      return;
    }
    const incidents = Array.isArray(payload.incidents) ? payload.incidents : [];
    setAppIncidents(incidents);
    setAppDiagnosticsPolicy(payload.policy || null);
    setAppDiagnosticsError('');
    setSelectedAppIncidentId((current) => (
      current && incidents.some((incident) => incident.incidentId === current)
        ? current
        : incidents[0]?.incidentId || ''
    ));
    if (!silent && diagnosticView === 'application') setMessage('');
  }, [diagnosticView]);

  const refreshApplicationDiagnostics = useCallback(async ({ silent = false } = {}) => {
    const payload = await listDesktopAppDiagnostics();
    applyAppDiagnosticsPayload(payload, { silent });
    setAppDiagnosticsLoading(false);
  }, [applyAppDiagnosticsPayload]);

  const handleDiagnosticViewChange = useCallback((nextView) => {
    setDiagnosticView(nextView);
    setMessage('');
    if (nextView === 'application') {
      setAppDiagnosticsLoading(true);
      void refreshApplicationDiagnostics();
    }
  }, [refreshApplicationDiagnostics]);

  const handleProviderChange = useCallback((nextProvider) => {
    setFocusedProvider(nextProvider);
    setSelectedRunId('');
    setSelectedRunPinned(false);
    setRuns([]);
    setExporting(false);
    setMessage('');
    setRunsLoadError('');
    if (nextProvider) setLoading(true);
  }, []);

  const runGroups = useMemo(() => groupRunsBySyncId(runs), [runs]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    let cancelled = false;
    async function loadSupportDiagnostics() {
      const [runsPayload, recentCount] = await Promise.all([
        fetchFailedRunsPayload(activeFocusedProvider),
        fetchAllRecentCount(),
      ]);
      if (cancelled) return;
      setAllRecentCount(recentCount);
      applyRunsPayload(runsPayload);
      setLoading(false);
    }
    void loadSupportDiagnostics();
    return () => {
      cancelled = true;
    };
  }, [activeFocusedProvider, applyRunsPayload, fetchAllRecentCount, fetchFailedRunsPayload, open]);

  useEffect(() => {
    if (!open) return undefined;
    const initialRefreshId = window.setTimeout(() => {
      void refreshApplicationDiagnostics({ silent: diagnosticView !== 'application' });
    }, 0);
    const intervalId = window.setInterval(() => {
      void refreshApplicationDiagnostics({ silent: true });
    }, 15000);
    return () => {
      window.clearTimeout(initialRefreshId);
      window.clearInterval(intervalId);
    };
  }, [diagnosticView, open, refreshApplicationDiagnostics]);

  useDismissibleLayer({
    open,
    ref: containerRef,
    onDismiss: closePanel,
    pointerEvent: 'mousedown',
    ignoreSelector: '.support-logs-picker-menu',
  });

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const intervalId = window.setInterval(() => {
      const refreshSupportDiagnostics = async () => {
        const [runsPayload, recentCount] = await Promise.all([
          fetchFailedRunsPayload(activeFocusedProvider),
          fetchAllRecentCount(),
        ]);
        setAllRecentCount(recentCount);
        if (activeFocusedProvider) {
          applyRunsPayload(runsPayload, { silent: true });
        }
      };
      void refreshSupportDiagnostics();
    }, SUPPORT_DIAGNOSTICS_REFRESH_INTERVAL_MS);
    return () => window.clearInterval(intervalId);
  }, [activeFocusedProvider, applyRunsPayload, fetchAllRecentCount, fetchFailedRunsPayload, open]);

  const exportRunZip = async () => {
    if (!activeFocusedProvider || !selectedRunId) {
      setMessage('Pick a sync attempt first.');
      return;
    }
    setExporting(true);
    setMessage('');
    let archiveId = '';
    let archiveFilename = '';
    try {
      const resp = await fetch(
        `${API}/settings/support-logs/runs/${encodeURIComponent(activeFocusedProvider)}/${encodeURIComponent(selectedRunId)}/zip`,
        { method: 'POST' },
      );
      const data = await resp.json();
      if (data.status !== 'ok') {
        setMessage(data.message || 'Failed to package this snapshot.');
        return;
      }
      archiveId = data.archive_id || '';
      archiveFilename = data.archive_filename || '';
      setMessage(archiveFilename ? `Saved ${archiveFilename}.` : 'Saved.');
    } catch (_) {
      setMessage('Failed to package this snapshot.');
      return;
    } finally {
      setExporting(false);
    }
    if (archiveId) {
      presentSupportArchive(archiveId, archiveFilename).catch(() => {
        setMessage('The snapshot was saved, but could not be opened or downloaded.');
      });
    }
  };

  const exportEverythingRecent = async () => {
    setExportingAll(true);
    setMessage('');
    let archiveId = '';
    let archiveFilename = '';
    try {
      const resp = await fetch(`${API}/settings/support-logs/runs/export-recent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ minutes: EXPORT_EVERYTHING_RECENT_MINUTES }),
      });
      const data = await resp.json();
      if (data.status === 'empty') {
        setMessage(`No diagnostic snapshots from the last ${EXPORT_EVERYTHING_RECENT_MINUTES} minutes to bundle.`);
        return;
      }
      if (data.status !== 'ok') {
        setMessage(data.message || 'Failed to bundle recent snapshots.');
        return;
      }
      archiveId = data.archive_id || '';
      archiveFilename = data.archive_filename || '';
      const runCount = data.run_count || 0;
      const attemptCount = data.included_attempt_count || 0;
      const omittedAttemptCount = data.omitted_attempt_count || 0;
      const requestedAttemptCount = data.requested_attempt_count || attemptCount + omittedAttemptCount;
      const omittedProviders = Array.isArray(data.providers_with_omitted_attempts)
        ? data.providers_with_omitted_attempts
          .filter(Boolean)
          .map((provider) => providerLabelByKey[provider] || provider)
        : [];
      const omittedProviderNote = omittedProviders.length
        ? ` for ${omittedProviders.join(', ')}`
        : '';
      const omissionNote = data.truncated
        ? ` Exported the newest ${attemptCount} of ${requestedAttemptCount} complete attempts; ${omittedAttemptCount} older attempt(s)${omittedProviderNote} were omitted as whole units by the safety limit and remain available from their provider timestamp pills. See export_manifest.json.`
        : '';
      setMessage(
        archiveFilename
          ? `Bundled ${attemptCount} finalized attempt(s) (${runCount} snapshots) as ${archiveFilename}.${omissionNote}`
          : `Bundled ${attemptCount} finalized attempt(s) (${runCount} snapshots).${omissionNote}`,
      );
    } catch (_) {
      setMessage('Failed to bundle recent snapshots.');
      return;
    } finally {
      setExportingAll(false);
    }
    if (archiveId) {
      presentSupportArchive(archiveId, archiveFilename).catch(() => {
        setMessage('The bundle was saved, but could not be opened or downloaded.');
      });
    }
  };

  const exportApplicationIncident = async ({ current = false } = {}) => {
    setExportingAppIncident(true);
    setMessage('');
    try {
      const result = await exportDesktopAppDiagnostic(current ? '' : selectedAppIncidentId);
      if (result?.status === 'cancelled') return;
      if (result?.status !== 'ok') {
        setMessage(result?.message || 'Application diagnostics could not be exported.');
        return;
      }
      setMessage(result.filename ? `Saved ${result.filename}.` : 'Application diagnostics saved.');
      void refreshApplicationDiagnostics({ silent: true });
    } finally {
      setExportingAppIncident(false);
    }
  };

  const formatRunTime = (run) => {
    const raw = run?.modified_at || run?.generated_at || '';
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return '';
    const use12Hour = normalizeUserTimeFormat(userTimeFormat) === '12h';
    const now = getAppNow();
    const formatterTimeZone = userTimezone || DEFAULT_USER_TIMEZONE;
    const dayFormatterOptions = {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      timeZone: formatterTimeZone,
    };
    try {
      const parsedDay = new Intl.DateTimeFormat('en-CA', dayFormatterOptions).format(parsed);
      const today = new Intl.DateTimeFormat('en-CA', dayFormatterOptions).format(now);
      const formatter = new Intl.DateTimeFormat([], {
        ...(parsedDay === today ? {} : { month: 'short', day: 'numeric', ...(parsed.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }) }),
        hour: use12Hour ? 'numeric' : '2-digit',
        minute: '2-digit',
        ...(parsedDay === today ? { second: '2-digit' } : {}),
        ...(use12Hour ? { hour12: true } : { hourCycle: 'h23' }),
        timeZone: formatterTimeZone,
      });
      return formatter.format(parsed);
    } catch (_) {
      if (parsed.toDateString() !== now.toDateString()) {
        return parsed.toLocaleString([], {
          month: 'short',
          day: 'numeric',
          ...(parsed.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
          hour: use12Hour ? 'numeric' : '2-digit',
          minute: '2-digit',
          hour12: use12Hour,
        });
      }
      return parsed.toLocaleTimeString([], {
        hour: use12Hour ? 'numeric' : '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: use12Hour,
      });
    }
  };

  return (
    <div ref={containerRef} className={`support-logs-control ${open ? 'is-open' : ''}`.trim()}>
      <button
        type="button"
        aria-label="Support Diagnostics"
        data-tooltip={sidebarExpanded ? null : 'Diagnostics'}
        data-tooltip-placement="right"
        className="floating-nav-btn support-logs-trigger"
        onClick={() => setPanelOpen(!open)}
      >
        <span className="floating-nav-btn-icon">
          <DiagnosticsSidebarIcon className="floating-nav-icon-graphic" />
        </span>
        <span className="floating-nav-label">Diagnostics</span>
      </button>

      {open && (
        <div className="support-logs-panel" role="dialog" aria-label="Diagnostics">
          <div className="support-logs-panel-header">
            <div>
              <h3 className="support-logs-title">Diagnostics</h3>
            </div>
            {isDevBuild && diagnosticView === 'institutions' && (
              <div
                className="support-logs-dev-toggle"
                data-tooltip="Developer-only: visible in dev builds and stripped from production packages. When on, diagnostics keep less-redacted provider responses and transaction narratives for local troubleshooting. Keep developer captures on this device."
              >
                <span className="support-logs-dev-toggle-label">Dev capture</span>
                <DevCaptureToggle enabled={devCaptureOn} onChange={toggleDevCapture} />
              </div>
            )}
          </div>

          <div className="support-logs-view-tabs" role="tablist" aria-label="Diagnostic type">
            <button
              type="button"
              className={`btn-secondary app-control-root support-logs-view-tab ${diagnosticView === 'institutions' ? 'is-active' : ''}`.trim()}
              onClick={() => handleDiagnosticViewChange('institutions')}
              role="tab"
              aria-selected={diagnosticView === 'institutions'}
            >
              <span className="app-control-label">Institutions</span>
            </button>
            <button
              type="button"
              className={`btn-secondary app-control-root support-logs-view-tab ${diagnosticView === 'application' ? 'is-active' : ''}`.trim()}
              onClick={() => handleDiagnosticViewChange('application')}
              role="tab"
              aria-selected={diagnosticView === 'application'}
            >
              <span className="app-control-label">Application</span>
            </button>
          </div>

          {diagnosticView === 'institutions' ? (
            <>
              <p className="support-logs-copy">
                <BrandName /> saves a diagnostic record on this device after each institution sync. Nothing is sent to the developer automatically. Choose an institution, then select the timestamp for the sync you want to inspect or share. Each timestamp exports only that complete sync attempt.
              </p>
              <p className="support-logs-copy support-logs-copy-faint">
                {devCaptureOn
                  ? 'Developer capture is on. Credentials, session cookies, and authentication tokens are still redacted, but the bundle may include more detailed provider responses, account references, and transaction descriptions. Keep these exports on this device unless you are sending one privately for support.'
                  : 'Standard diagnostic bundles redact credentials, session cookies, authentication tokens, account names, and raw account identifiers. They may still include troubleshooting details such as stable redacted references, account types, currencies, amounts, dates, statuses, and error messages. Share them privately with support—never post them publicly.'}
              </p>

              <div className="support-logs-picker-row">
                <SupportLogsProviderPicker
                  value={activeFocusedProvider}
                  onChange={handleProviderChange}
                  placeholder="Choose institution…"
                  groups={supportProviderGroups}
                  disabled={loading}
                  emptyMessage="No institutions are available for diagnostics yet."
                  ariaLabel="Choose institution for support diagnostics"
                />
              </div>

              {activeFocusedProvider && !focusedProviderConfigured && (
            <p className="support-logs-note">
              {focusedProviderLabel || activeFocusedProvider} is not added yet. The next time you try to add it, the snapshot will land here.
            </p>
          )}

              {activeFocusedProvider && (
            <div className="support-logs-diagnostics">
              <div className="support-logs-section-header">
                <div>
                  <h4 className="support-logs-section-title">Recent sync attempts</h4>
                </div>
              </div>

              {runGroups.length > 0 && (
                <div
                  className="support-logs-attempt-grid"
                  role="radiogroup"
                  aria-label="Recent diagnostic snapshots"
                >
                  {runGroups.map((group) => {
                    const groupRunIds = group.archives.map((archive) => archive.run_id);
                    const isActive = groupRunIds.includes(selectedRunId);
                    return (
                      <button
                        key={group.key}
                        type="button"
                        className={`support-logs-attempt-pill app-control-root ${isActive ? 'is-active' : ''}`.trim()}
                        onClick={() => {
                          setSelectedRunId(group.primaryRun?.run_id || '');
                          setSelectedRunPinned(true);
                        }}
                        role="radio"
                        aria-checked={isActive}
                      >
                        <span className="app-control-label">{formatRunTime(group.primaryRun)}</span>
                      </button>
                    );
                  })}
                </div>
              )}

              {loading && runGroups.length === 0 && (
                <p className="support-logs-note">
                  Loading recent sync attempts…
                </p>
              )}

              {!loading && runsLoadError && runGroups.length === 0 && (
                <p className="support-logs-note">
                  {runsLoadError}
                </p>
              )}

              {!loading && !runsLoadError && runGroups.length === 0 && (
                <p className="support-logs-note">
                  No recent sync attempts for {focusedProviderLabel || activeFocusedProvider} — try a sync first, then come back.
                </p>
              )}

              <div className="support-logs-actions support-logs-actions-secondary">
                <button
                  type="button"
                  className="btn-primary app-control-root support-logs-export-btn"
                  onClick={exportRunZip}
                  disabled={!selectedRunId || exporting}
                >
                  <span className="app-control-label">{exporting ? 'Packaging…' : 'Export Attempt Bundle'}</span>
                </button>
              </div>
            </div>
          )}

              <div className="support-logs-empty-fill">
                <p className="support-logs-note">
                  Do you have issues with more than one institution? Use <strong>Export all providers’ recent attempts</strong> below for the explicitly multi-provider bundle from the last {EXPORT_EVERYTHING_RECENT_MINUTES} minutes — successes and failures together, with each attempt kept in its own folder.
                </p>
              </div>

              <div className="support-logs-actions support-logs-actions-tertiary">
                <button
                  type="button"
                  className="btn-primary app-control-root support-logs-export-btn"
                  onClick={exportEverythingRecent}
                  disabled={exportingAll || allRecentCount === 0}
                >
                  <span className="app-control-label">{exportingAll ? 'Bundling…' : `Export all providers’ recent attempts (${EXPORT_EVERYTHING_RECENT_MINUTES} min)`}</span>
                </button>
              </div>
            </>
          ) : (
            <div className="support-logs-application-view" role="tabpanel">
              <p className="support-logs-copy">
                <BrandName /> automatically saves an application incident locally on this device when the desktop window or its local backend stops responding. Nothing is uploaded to the developer. Select an incident to export the technical logs and recovery details saved around that problem. If no incident was created for what you saw, <strong className="support-logs-app-export-label">Export Current App Logs</strong> packages the most recent entries already in the app’s technical logs, ending when you click Export.
              </p>
              <p className="support-logs-copy support-logs-copy-faint">
                Incidents stay on this device for {appDiagnosticsPolicy?.retentionDays || Math.round((appDiagnosticsPolicy?.retentionHours || 168) / 24)} days, with at most {appDiagnosticsPolicy?.maxIncidents || 8} incidents and {Math.round((appDiagnosticsPolicy?.maxTotalBytes || (8 * 1024 * 1024)) / (1024 * 1024))} MB total. That retention period does not guarantee the stored log lines span the entire period: each capture keeps up to {Math.round((appDiagnosticsPolicy?.maxLogTailBytes || (384 * 1024)) / 1024)} KB from each current source log, so the time covered varies with activity. Bundles contain sanitized technical logs and recovery timing—not the database, provider diagnostics, credentials, account records, balances, or transactions. Review them before sharing and send them privately.
              </p>

              {appIncidents.length > 0 ? (
                <div
                  className="support-logs-app-incident-list"
                  role="radiogroup"
                  aria-label="Recent application diagnostic incidents"
                >
                  {appIncidents.map((incident) => {
                    const selected = incident.incidentId === selectedAppIncidentId;
                    const outcome = incident.outcome === 'recovered'
                      ? 'Recovered'
                      : incident.outcome === 'recovery_failed'
                        ? 'Recovery failed'
                        : incident.outcome === 'recovery_rate_limited'
                          ? 'Recovery paused'
                          : 'Captured';
                    return (
                      <button
                        key={incident.incidentId}
                        type="button"
                        className={`support-logs-app-incident app-control-root ${selected ? 'is-active' : ''}`.trim()}
                        onClick={() => setSelectedAppIncidentId(incident.incidentId)}
                        role="radio"
                        aria-checked={selected}
                      >
                        <span className="support-logs-app-incident-copy app-control-label">
                          {formatRunTime({ generated_at: incident.createdAt })} · {outcome}
                        </span>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <p className="support-logs-note">
                  {appDiagnosticsLoading
                    ? 'Loading recent application incidents…'
                    : appDiagnosticsError || 'No application incidents were captured within the retention window.'}
                </p>
              )}

              <div className="support-logs-actions support-logs-actions-tertiary">
                <button
                  type="button"
                  className="btn-primary app-control-root support-logs-export-btn"
                  onClick={() => exportApplicationIncident()}
                  disabled={!selectedAppIncidentId || exportingAppIncident}
                >
                  <span className="app-control-label">{exportingAppIncident ? 'Exporting…' : 'Export Selected Incident'}</span>
                </button>
                <button
                  type="button"
                  className="btn-secondary app-control-root support-logs-export-btn"
                  onClick={() => exportApplicationIncident({ current: true })}
                  disabled={exportingAppIncident}
                >
                  <span className="app-control-label">{exportingAppIncident ? 'Exporting…' : 'Export Current App Logs'}</span>
                </button>
              </div>
            </div>
          )}

          {message && (
            <div className="support-logs-message">
              {message}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function pageTitleForPathname(pathname) {
  if (pathname === '/') return 'Dashboard';
  if (pathname === '/accounts') return 'Accounts';
  if (pathname === '/cash-flow') return 'Cash Flow';
  if (pathname === '/holdings') return 'Investments';
  if (pathname === '/transactions') return 'Transactions';
  if (pathname.startsWith('/settings')) return 'Settings';
  if (pathname === '/faq' || pathname === '/licenses') return 'Help';
  return '';
}

const SETTINGS_SUBNAV_ITEMS = [
  { to: '/settings', end: true, label: 'General' },
  { to: '/settings/categories', label: 'Transaction Categories' },
];

function SettingsSubnav() {
  return (
    <div className="settings-subnav app-section-nav" role="navigation" aria-label="Settings sections">
      {SETTINGS_SUBNAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) => `settings-subnav-tab app-section-nav-tab ${isActive ? 'is-active' : ''}`.trim()}
        >
          <span className="settings-subnav-tab-label">{item.label}</span>
        </NavLink>
      ))}
    </div>
  );
}

const HELP_SUBNAV_ITEMS = [
  { to: '/faq', end: true, label: 'Frequently Asked Questions' },
  { to: '/licenses', label: 'Legal & Licences' },
];

function HelpSubnav() {
  return (
    <div className="settings-subnav app-section-nav" role="navigation" aria-label="Help sections">
      {HELP_SUBNAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) => `settings-subnav-tab app-section-nav-tab ${isActive ? 'is-active' : ''}`.trim()}
        >
          <span className="settings-subnav-tab-label">{item.label}</span>
        </NavLink>
      ))}
    </div>
  );
}

// Right-side content of the global toolbar row. Owns the per-route
// filter slots (currency, scope, timeline, etc.) that the page-level
// components portal into.
function GlobalToolbarActions({
  pathname,
  currencyBusy,
  currencyOptions,
  dashboardTimeframe,
  onDashboardTimeframeChange,
  dashboardCustomDateRange,
  onDashboardCustomDateRangeChange,
  primaryCurrency,
  setPrimaryCurrency,
}) {
  const isDashboard = pathname === '/';
  const isAccounts = pathname === '/accounts';
  const isCashFlow = pathname === '/cash-flow';
  const isHoldings = pathname === '/holdings';
  const isTransactions = pathname === '/transactions';
  const routeKey = pathname === '/' ? 'dashboard' : pathname.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '') || 'page';
  const currencyRow = {
    key: 'currency',
    label: 'Currency',
    control: (
      <CurrencyViewPicker
        value={primaryCurrency}
        options={currencyOptions}
        onChange={setPrimaryCurrency}
        busy={currencyBusy}
        panelId={`${routeKey}-currency-panel`}
      />
    ),
  };

  if (isTransactions) {
    return (
      <AppViewFiltersMenu
        id="transactions-view-filters"
        ariaLabel="Transactions view and filters"
        rows={[
          {
            key: 'accounts',
            label: 'Accounts',
            control: (
              <div
                id="transactions-toolbar-scope-slot"
                className="toolbar-filter-slot transactions-toolbar-scope-slot"
                aria-label="Transaction account filters"
              />
            ),
          },
          {
            key: 'period',
            label: 'Period',
            control: (
              <div
                id="transactions-toolbar-timeline-slot"
                className="toolbar-filter-slot transactions-toolbar-timeline-slot"
                aria-label="Transaction period filters"
              />
            ),
          },
          {
            key: 'category',
            label: 'Category',
            control: (
              <div
                id="transactions-toolbar-category-slot"
                className="toolbar-filter-slot transactions-toolbar-category-slot"
                aria-label="Transaction category filters"
              />
            ),
          },
        ]}
      />
    );
  }

  if (isAccounts) {
    return (
      <AppViewFiltersMenu
        id="accounts-view-filters"
        ariaLabel="Accounts view and filters"
        rows={[
          {
            key: 'accounts',
            label: 'Accounts',
            control: (
              <div
                id="accounts-toolbar-scope-slot"
                className="toolbar-filter-slot accounts-toolbar-scope-slot"
                aria-label="Accounts scope filters"
              />
            ),
          },
          {
            key: 'period',
            label: 'Period',
            control: (
              <DashboardTimelineControl
                timeframe={dashboardTimeframe}
                customDateRange={dashboardCustomDateRange}
                onTimeframeChange={onDashboardTimeframeChange}
                onCustomDateRangeChange={onDashboardCustomDateRangeChange}
              />
            ),
          },
          currencyRow,
          {
            key: 'group',
            label: 'Group',
            control: (
              <div
                id="accounts-toolbar-group-slot"
                className="toolbar-filter-slot accounts-toolbar-group-slot"
                aria-label="Accounts grouping"
              />
            ),
          },
        ]}
      />
    );
  }

  if (isCashFlow) {
    return (
      <AppViewFiltersMenu
        id="cash-flow-view-filters"
        ariaLabel="Cash Flow view and filters"
        rows={[
          {
            key: 'accounts',
            label: 'Accounts',
            control: (
              <div
                id="cash-flow-toolbar-scope-slot"
                className="toolbar-filter-slot cash-flow-toolbar-scope-slot"
                aria-label="Cash Flow account filters"
              />
            ),
          },
          currencyRow,
        ]}
      />
    );
  }

  if (isHoldings) {
    return null;
  }

  if (isDashboard) {
    return (
      <AppViewFiltersMenu
        id="dashboard-view-filters"
        ariaLabel="Dashboard view and filters"
        rows={[
          {
            key: 'accounts',
            label: 'Accounts',
            control: (
              <div
                id="dashboard-toolbar-scope-slot"
                className="toolbar-filter-slot dashboard-toolbar-scope-slot"
                aria-label="Dashboard source filters"
              />
            ),
          },
          {
            key: 'period',
            label: 'Period',
            control: (
              <DashboardTimelineControl
                timeframe={dashboardTimeframe}
                customDateRange={dashboardCustomDateRange}
                onTimeframeChange={onDashboardTimeframeChange}
                onCustomDateRangeChange={onDashboardCustomDateRangeChange}
              />
            ),
          },
          currencyRow,
          {
            key: 'layout',
            label: 'Layout',
            control: (
              <div
                id="dashboard-toolbar-customize-slot"
                className="toolbar-filter-slot dashboard-toolbar-customize-slot"
                aria-label="Dashboard customization"
              />
            ),
          },
        ]}
      />
    );
  }

  return null;
}

function ShellContent({
  data,
  dataRefreshId,
  fetchData,
  allScopeInstitutions,
  fetchAllScopeInstitutions,
  userTimezone,
  userTimezoneConfigured,
  setUserTimezone,
  setUserTimezoneConfigured,
  userTimeFormat,
  setUserTimeFormat,
  autoSyncStates,
  setAutoSyncStates,
  autoSyncInProgress,
  activeSyncBatches,
  syncNetworkNotice,
  setSyncNetworkNotice,
  syncActivity,
  dataLoadError,
  scopeLoadError,
  onRetryData,
  onRetryScope,
  showAddModal,
  setShowAddModal,
  showManualWizard,
  setShowManualWizard,
  assetGroupCategory,
  setAssetGroupCategory,
  showCashModal,
  setShowCashModal,
  addAuthModal,
  setAddAuthModal,
  handleAddAuthNeeded,
  handleAddAuthSuccess,
  syncAllBlockingRef,
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const [themeMode, setThemeModeRaw] = useState(INITIAL_BREAKTWENTY_THEME_MODE);
  const [_, setThemeRevision] = useState(0);
  const existingSyncProviders = useMemo(
    () => data.allInstitutions.map((institution) => institution.provider),
    [data.allInstitutions],
  );
  const [promoDemoActive, setPromoDemoActiveState] = useState(
    () => PROMO_DEMO_ENABLED && isPromoDemoActive(),
  );
  const [investmentsSelectedAccountIds, setInvestmentsSelectedAccountIds] = useState(null);
  const [dashboardTimeframe, setDashboardTimeframe] = useState(() => {
    try {
      const saved = window.localStorage.getItem(GLOBAL_TIMEFRAME_STORAGE_KEY);
      if (saved === PORTFOLIO_CUSTOM_TIMEFRAME_KEY && !isCompleteCustomDateRange(readStoredDashboardCustomDateRange())) {
        return DEFAULT_PORTFOLIO_TIMEFRAME;
      }
      return saved || DEFAULT_PORTFOLIO_TIMEFRAME;
    } catch (_) {
      return DEFAULT_PORTFOLIO_TIMEFRAME;
    }
  });
  const [dashboardCustomDateRange, setDashboardCustomDateRange] = useState(readStoredDashboardCustomDateRange);
  useEffect(() => {
    try {
      window.localStorage.setItem(GLOBAL_TIMEFRAME_STORAGE_KEY, String(dashboardTimeframe || ''));
    } catch (_) { /* ignore */ }
  }, [dashboardTimeframe]);
  useEffect(() => {
    try {
      window.localStorage.setItem(
        GLOBAL_TIMEFRAME_CUSTOM_RANGE_STORAGE_KEY,
        JSON.stringify(dashboardCustomDateRange || { start: '', end: '' }),
      );
    } catch (_) { /* ignore */ }
  }, [dashboardCustomDateRange]);
  const [fxRates, setFxRates] = useState({});
  const [primaryCurrency, setPrimaryCurrencyState] = useState('CAD');
  const [currencyBusy, setCurrencyBusy] = useState(false);
  const refreshRates = useCallback(() => (
    fetch(`${API}/fx-rates`)
      .then((r) => r.json())
      .then((d) => {
        if (d.base) setPrimaryCurrencyState(String(d.base).trim().toUpperCase());
        if (d.rates) setFxRates(d.rates);
      })
      .catch(() => {})
  ), []);
  useEffect(() => { refreshRates(); }, [refreshRates]);
  // The toolbar picker sets the primary currency directly: persist to Settings,
  // then re-fetch rates on the new base (which flips primaryCurrency + its rates
  // together, so the whole app switches in one render). `currencyBusy` drives the
  // picker's pending spinner across that round-trip.
  const setPrimaryCurrency = useCallback(async (next) => {
    const normalized = String(next || '').trim().toUpperCase();
    if (!normalized || normalized === primaryCurrency) return;
    setCurrencyBusy(true);
    try {
      await fetch(`${API}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ primary_currency: normalized }),
      });
    } catch (_) { /* ignore */ }
    await refreshRates();
    setCurrencyBusy(false);
  }, [primaryCurrency, refreshRates]);
  const currencyConvert = useMemo(
    () => makeCurrencyConverter(primaryCurrency, fxRates),
    [primaryCurrency, fxRates],
  );
  const currencyContextValue = useMemo(() => ({
    primaryCurrency,
    fxRates,
    setPrimaryCurrency,
    convert: currencyConvert,
    refreshRates,
  }), [primaryCurrency, fxRates, setPrimaryCurrency, currencyConvert, refreshRates]);
  const activeTheme = getAppliedBreakTwentyColorTheme(themeMode);
  const themeContextValue = useMemo(() => ({
    mode: themeMode,
    colors: activeTheme.colors,
    chartColors: activeTheme.chartColors,
  }), [activeTheme, themeMode]);
  const [liveBrandAssetVersions, setLiveBrandAssetVersions] = useState(() => normalizeBrandAssetVersions(brandAssetVersions));
  useEffect(() => subscribeBrandAssetVersionUpdates((nextVersions) => {
    setLiveBrandAssetVersions((current) => (
      brandAssetVersionsEqual(current, nextVersions) ? current : nextVersions
    ));
  }), []);
  useEffect(() => {
    if (!import.meta.env.DEV || typeof window === 'undefined') return undefined;
    let cancelled = false;
    const refreshBrandAssetVersions = () => {
      fetch(`${BRAND_ASSET_VERSION_MANIFEST_SRC}?t=${Date.now()}`, { cache: 'no-store' })
        .then((response) => (response.ok ? response.json() : null))
        .then((nextVersions) => {
          if (cancelled || !nextVersions) return;
          const normalized = normalizeBrandAssetVersions(nextVersions);
          setLiveBrandAssetVersions((current) => (
            brandAssetVersionsEqual(current, normalized) ? current : normalized
          ));
        })
        .catch(() => {});
    };
    refreshBrandAssetVersions();
    const timer = window.setInterval(refreshBrandAssetVersions, 750);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);
  const sidebarBrandAssets = getBrandImageAssets(themeMode, liveBrandAssetVersions);
  // Always offer the full selectable set (not just held currencies) so the user
  // can switch to any of them — e.g. CHF even when they hold no CHF account.
  const currencyOptions = SELECTABLE_CURRENCIES;
  const [showWelcome, setShowWelcome] = useState(false);
  const setThemeMode = useCallback((nextMode) => {
    setThemeModeRaw(normalizeBreakTwentyThemeMode(nextMode));
  }, []);
  const togglePromoDemo = useCallback(async () => {
    if (!PROMO_DEMO_ENABLED) return;
    const next = !promoDemoActive;
    setPromoDemoActive(next);
    setPromoDemoActiveState(next);
    setShowWelcome(false);
    navigate('/');
    await fetchData({ forceDataRefreshId: true });
  }, [fetchData, navigate, promoDemoActive, setShowWelcome]);
  const openWelcomeFromSettings = useCallback(async () => {
    if (PROMO_DEMO_ENABLED && promoDemoActive) {
      setPromoDemoActive(false);
      setPromoDemoActiveState(false);
      await fetchData({ forceDataRefreshId: true });
    }
    setShowWelcome(true);
  }, [fetchData, promoDemoActive, setShowWelcome]);
  useEffect(() => {
    const appliedMode = applyBreakTwentyThemeMode(themeMode);
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.setItem(BREAKTWENTY_THEME_STORAGE_KEY, appliedMode);
    } catch {
      // Theme mode still applies for the session when storage is unavailable.
    }
  }, [themeMode]);
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const handleThemeHmr = () => {
      setThemeRevision((revision) => revision + 1);
    };
    window.addEventListener(BREAKTWENTY_THEME_HMR_EVENT, handleThemeHmr);
    return () => window.removeEventListener(BREAKTWENTY_THEME_HMR_EVENT, handleThemeHmr);
  }, []);
  const [supportLogsOpen, setSupportLogsOpen] = useState(false);
  const [optimisticAccountSyncActivities, setOptimisticAccountSyncActivities] = useState([]);
  const [showAddTransactionModal, setShowAddTransactionModal] = useState(false);
  const [desktopUpdateStatus, setDesktopUpdateStatus] = useState(null);
  const showUpdateNotification = hasSidebarUpdateNotification(desktopUpdateStatus);
  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/onboarding/status`)
      .then((r) => r.json())
      .then((status) => {
        if (cancelled || !status || status.completed) return;
        setShowWelcome(true);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);
  const saveWelcomeDefaults = useCallback(async ({
    timezone,
    timeFormat,
    primaryCurrency: nextPrimaryCurrency,
  } = {}) => {
    const normalizedTimezone = String(timezone || '').trim() || DEFAULT_USER_TIMEZONE;
    const normalizedTimeFormat = normalizeUserTimeFormat(timeFormat || userTimeFormat);
    const normalizedCurrency = String(nextPrimaryCurrency || '').trim().toUpperCase();

    if (
      normalizedTimezone
      && (
        normalizedTimezone !== userTimezone
        || normalizedTimeFormat !== userTimeFormat
        || !userTimezoneConfigured
      )
    ) {
      const resp = await fetch(`${API}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_timezone: normalizedTimezone,
          user_time_format: normalizedTimeFormat,
        }),
      });
      if (!resp.ok) {
        const payload = await resp.json().catch(() => ({}));
        throw new Error(payload?.message || 'Failed to save time preferences.');
      }
      setUserTimezone(normalizedTimezone);
      setUserTimezoneConfigured(true);
      setUserTimeFormat(normalizedTimeFormat);
    }

    if (normalizedCurrency && normalizedCurrency !== primaryCurrency) {
      await setPrimaryCurrency(normalizedCurrency);
    }
  }, [
    primaryCurrency,
    setPrimaryCurrency,
    setUserTimeFormat,
    setUserTimezone,
    setUserTimezoneConfigured,
    userTimeFormat,
    userTimezone,
    userTimezoneConfigured,
  ]);
  const [sidebarExpanded, setSidebarExpanded] = useState(() => {
    try {
      const stored = window.localStorage.getItem(SIDEBAR_EXPANDED_STORAGE_KEY);
      // First startup on this device (no stored preference yet) → expanded.
      // After the user toggles it, the persisted value wins on every later launch.
      return stored === null ? true : stored === 'true';
    } catch (_) {
      return true;
    }
  });
  const hasModalOverlayOpen = Boolean(
    showAddModal
    || showManualWizard
    || assetGroupCategory
    || showCashModal
    || showAddTransactionModal
    || showWelcome
    || addAuthModal
  );
  const effectiveSidebarExpanded = showWelcome || sidebarExpanded;
  const handleSidebarToggle = useCallback(() => {
    if (hasModalOverlayOpen) return;
    setSidebarExpanded((current) => !current);
  }, [hasModalOverlayOpen]);
  // openRightTrays is an ordered list of currently-mounted right-edge trays;
  // the most recently registered key is the "active" one whose width the shell
  // reserves. Mutual-exclusion in product UX means there's normally only one.
  const [openRightTrays, setOpenRightTrays] = useState([]);
  const registerRightTray = useCallback((key) => {
    setOpenRightTrays((prev) => [...prev.filter((existing) => existing !== key), key]);
    return () => setOpenRightTrays((prev) => prev.filter((existing) => existing !== key));
  }, []);
  const activeRightTrayKey = openRightTrays.length > 0
    ? openRightTrays[openRightTrays.length - 1]
    : null;
  const activeRightTrayReservationPolicy = getRightTrayReservationPolicy(activeRightTrayKey);
  const rightTrayContextValue = useMemo(
    () => ({ register: registerRightTray, activeKey: activeRightTrayKey }),
    [registerRightTray, activeRightTrayKey],
  );
  const addAuthModalKind = addAuthModal ? getProviderAddAuthModal(addAuthModal.provider) : null;
  const isDashboardRoute = location.pathname === '/';
  const isCashFlowRoute = location.pathname === '/cash-flow';
  const isAccountsRoute = location.pathname === '/accounts';
  const isSettingsRoute = location.pathname.startsWith('/settings');
  const isHelpRoute = location.pathname === '/faq' || location.pathname === '/licenses';
  const showsGlobalBalanceToggle = ['/', '/accounts', '/cash-flow'].includes(location.pathname);
  const activeProviderActivities = useMemo(
    () => mergeOptimisticSyncActivities(
      getActiveProviderActivityItems(syncActivity),
      autoSyncStates,
      optimisticAccountSyncActivities,
    ),
    [autoSyncStates, optimisticAccountSyncActivities, syncActivity]
  );
  const handleSyncAllBlockedChange = useCallback((blocked) => {
    syncAllBlockingRef.current = blocked;
  }, [syncAllBlockingRef]);
  const routeTimeframe = showWelcome ? DEFAULT_PORTFOLIO_TIMEFRAME : dashboardTimeframe;
  const routeCustomDateRange = useMemo(
    () => (showWelcome ? { start: '', end: '' } : dashboardCustomDateRange),
    [dashboardCustomDateRange, showWelcome],
  );
  const routeContent = useMemo(() => (
    <BreakTwentyErrorBoundary key={location.pathname} resetKey={dataRefreshId}>
      <React.Suspense fallback={<RouteLoadingState />}>
        <AppRoutes>
          <Route
            path="/"
            element={
              <Dashboard
                data={data}
                allScopeInstitutions={allScopeInstitutions}
                fetchAllScopeInstitutions={fetchAllScopeInstitutions}
                onDataChange={fetchData}
                dataRefreshId={dataRefreshId}
                timeframe={routeTimeframe}
                customDateRange={routeCustomDateRange}
                syncActivity={syncActivity}
                activeSyncBatches={activeSyncBatches}
                autoSyncStates={autoSyncStates}
                autoSyncInProgress={autoSyncInProgress}
                tourDemoActive={showWelcome}
              />
            }
          />
          <Route
            path="/accounts"
            element={
              <Accounts
                data={data}
                allScopeInstitutions={allScopeInstitutions}
                fetchAllScopeInstitutions={fetchAllScopeInstitutions}
                userTimezone={userTimezone}
                userTimezoneConfigured={userTimezoneConfigured}
                onDataChange={fetchData}
                autoSyncStates={autoSyncStates}
                setAutoSyncStates={setAutoSyncStates}
                autoSyncInProgress={autoSyncInProgress}
                activeSyncBatches={activeSyncBatches}
                syncActivity={syncActivity}
                onOptimisticSyncActivitiesChange={setOptimisticAccountSyncActivities}
                setSyncAllBlocked={handleSyncAllBlockedChange}
                syncNetworkNotice={syncNetworkNotice}
                onSyncNetworkNoticeChange={setSyncNetworkNotice}
                timeframe={routeTimeframe}
                customDateRange={routeCustomDateRange}
              />
            }
          />
          <Route
            path="/holdings"
            element={
              <Holdings
                data={data}
                allScopeInstitutions={allScopeInstitutions}
                fetchAllScopeInstitutions={fetchAllScopeInstitutions}
                onDataChange={fetchData}
                selectedAccountIds={investmentsSelectedAccountIds}
                setSelectedAccountIds={setInvestmentsSelectedAccountIds}
                timeframe={routeTimeframe}
                setTimeframe={setDashboardTimeframe}
                customDateRange={routeCustomDateRange}
                setCustomDateRange={setDashboardCustomDateRange}
              />
            }
          />
          <Route path="/transactions" element={<Transactions data={data} dataRefreshId={dataRefreshId} allScopeInstitutions={allScopeInstitutions} fetchAllScopeInstitutions={fetchAllScopeInstitutions} onDataChange={fetchData} timeframe={routeTimeframe} setTimeframe={setDashboardTimeframe} customDateRange={routeCustomDateRange} setCustomDateRange={setDashboardCustomDateRange} />} />
          <Route
            path="/cash-flow"
            element={
              <CashFlow
                allScopeInstitutions={allScopeInstitutions}
                fetchAllScopeInstitutions={fetchAllScopeInstitutions}
                onDataChange={fetchData}
              />
            }
          />
          <Route
            path="/settings"
            element={
              <Settings
                showPageTitle={false}
                onTimezoneChange={(timezone) => {
                  setUserTimezone(timezone || DEFAULT_USER_TIMEZONE);
                  setUserTimezoneConfigured(true);
                }}
                onTimeFormatChange={(timeFormat) => setUserTimeFormat(normalizeUserTimeFormat(timeFormat))}
                devToolsEnabled={PROMO_DEMO_ENABLED}
                promoDemoActive={PROMO_DEMO_ENABLED && promoDemoActive}
                onTogglePromoDemo={PROMO_DEMO_ENABLED ? togglePromoDemo : null}
                onOpenWelcome={openWelcomeFromSettings}
              />
            }
          />
          <Route path="/settings/categories" element={<CategoriesSettings />} />
          <Route path="/faq" element={<Faq />} />
          <Route path="/licenses" element={<Licenses />} />
        </AppRoutes>
      </React.Suspense>
    </BreakTwentyErrorBoundary>
  ), [
    activeSyncBatches,
    allScopeInstitutions,
    autoSyncInProgress,
    autoSyncStates,
    data,
    dataRefreshId,
    fetchAllScopeInstitutions,
    fetchData,
    handleSyncAllBlockedChange,
    investmentsSelectedAccountIds,
    location.pathname,
    openWelcomeFromSettings,
    promoDemoActive,
    routeCustomDateRange,
    routeTimeframe,
    setAutoSyncStates,
    setSyncNetworkNotice,
    setUserTimeFormat,
    setUserTimezone,
    setUserTimezoneConfigured,
    showWelcome,
    syncActivity,
    syncNetworkNotice,
    togglePromoDemo,
    userTimezone,
    userTimezoneConfigured,
  ]);
  const appShellRef = useRef(null);
  const appMainRef = useRef(null);
  const toolbarShellRef = useRef(null);
  const toolbarRowRef = useRef(null);
  const toolbarLeftRef = useRef(null);
  const toolbarCenterRef = useRef(null);
  const toolbarRightRef = useRef(null);
  const [toolbarWrapped, setToolbarWrapped] = useState(false);
  const handleSupportLogsOpenChange = useCallback((nextOpen) => {
    setSupportLogsOpen(nextOpen);
  }, []);

  useLayoutEffect(() => {
    const shell = appShellRef.current;
    const main = appMainRef.current;
    if (!shell || !main) return undefined;

    const measuredTrayVars = [
      '--app-tray-shell-width',
      '--app-tray-content-reservation',
      '--app-tray-content-left-reservation',
      '--app-tray-column-width',
      '--app-tray-resolved-edge-gap',
      '--app-tray-panel-gap',
      '--app-tray-edge-offset',
    ];
    const clearMeasuredTrayVars = () => {
      measuredTrayVars.forEach((name) => {
        shell.style.removeProperty(name);
        main.style.removeProperty(name);
      });
      main.querySelectorAll('.app-edge-tray-shell').forEach((trayShell) => {
        measuredTrayVars.forEach((name) => {
          trayShell.style.removeProperty(name);
        });
        trayShell.style.removeProperty('right');
        trayShell.style.removeProperty('width');
      });
    };

    if (!activeRightTrayKey) {
      clearMeasuredTrayVars();
      return undefined;
    }

    let frameId = null;
    const parsePx = (value) => {
      const parsed = parseFloat(value);
      return Number.isFinite(parsed) ? parsed : 0;
    };
    const getTrayShells = () => Array.from(main.querySelectorAll('.app-edge-tray-shell'));
    const setPxVar = (name, value) => {
      const next = `${Math.max(0, value).toFixed(3)}px`;
      if (shell.style.getPropertyValue(name) !== next) {
        shell.style.setProperty(name, next);
      }
      if (main.style.getPropertyValue(name) !== next) {
        main.style.setProperty(name, next);
      }
      getTrayShells().forEach((trayShell) => {
        if (trayShell.style.getPropertyValue(name) !== next) {
          trayShell.style.setProperty(name, next);
        }
      });
    };
    const setTrayShellProperty = (property, value) => {
      const next = `${Math.max(0, value).toFixed(3)}px`;
      getTrayShells().forEach((trayShell) => {
        if (trayShell.style.getPropertyValue(property) !== next) {
          trayShell.style.setProperty(property, next);
        }
      });
    };
    const clearTrayShellProperties = (...properties) => {
      getTrayShells().forEach((trayShell) => {
        properties.forEach((property) => {
          trayShell.style.removeProperty(property);
        });
      });
    };
    const measureCssWidth = (width) => {
      const probe = document.createElement('div');
      probe.setAttribute('aria-hidden', 'true');
      probe.style.position = 'absolute';
      probe.style.visibility = 'hidden';
      probe.style.pointerEvents = 'none';
      probe.style.height = '0';
      probe.style.overflow = 'hidden';
      probe.style.width = width;
      shell.appendChild(probe);
      const measuredWidth = probe.getBoundingClientRect().width;
      probe.remove();
      return Number.isFinite(measuredWidth) ? measuredWidth : 0;
    };
    const getVisibleContentRight = (fallbackRight) => {
      const content = main.querySelector('.app-main-column .app-content');
      if (!content) return fallbackRight;

      const surfaceSelector = [
        '.panel-shell',
        '.accounts-table-shell',
      ].join(',');
      const rightEdges = Array.from(content.querySelectorAll(surfaceSelector))
        .flatMap((element) => {
          const styles = window.getComputedStyle(element);
          if (styles.display === 'none' || styles.visibility === 'hidden') return [];
          const rect = element.getBoundingClientRect();
          if (rect.width <= 1 || rect.height <= 1) return [];
          return [rect.right];
        });

      return rightEdges.length > 0 ? Math.max(...rightEdges) : fallbackRight;
    };
    const getAccountsTableScrollableWidth = () => {
      const table = main.querySelector('.accounts-table-shell.accounts-institution-panels-shell');
      if (!table) return 0;

      return Math.max(
        0,
        table.scrollWidth,
        ...[
          '.accounts-table-scroll-sizer',
          '.accounts-table-rows',
          '.accounts-panel-header-inner',
        ].map((selector) => {
          const element = table.querySelector(selector);
          const width = element?.getBoundingClientRect?.().width;
          return Number.isFinite(width) ? width : 0;
        }),
      );
    };
    const measureTrayReservation = () => {
      frameId = null;
      const mainStyles = window.getComputedStyle(main);
      const mainRect = main.getBoundingClientRect();
      const leftPadding = parsePx(mainStyles.paddingLeft);
      const rightPadding = parsePx(mainStyles.paddingRight);
      const innerWidth = Math.max(
        0,
        main.clientWidth - leftPadding - rightPadding,
      );
      const workAreaMax = measureCssWidth('var(--app-work-area-max)') || innerWidth;
      const columnWidth = Math.min(innerWidth, workAreaMax);
      const centeredSlack = Math.max(0, (innerWidth - columnWidth) / 2);
      const edgeGap = measureCssWidth('var(--app-tray-edge-gap)');
      const configuredTrayWidth = measureCssWidth('var(--app-right-tray-width)');
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth || main.clientWidth;
      const trayWidth = Math.min(
        configuredTrayWidth,
        Math.max(0, viewportWidth - (edgeGap * 2)),
      );
      const reservationPolicy = getRightTrayReservationPolicy(activeRightTrayKey);
      const trayLaneWidth = trayWidth + (edgeGap * 2);
      const naturalColumnLeft = mainRect.left + leftPadding + centeredSlack;
      const naturalColumnRight = naturalColumnLeft + columnWidth;
      const column = main.querySelector('.app-main-column');
      const currentColumnRight = column?.getBoundingClientRect().right || naturalColumnRight;
      const currentContentRight = getVisibleContentRight(currentColumnRight);
      // Translate the currently reserved layout back into its natural geometry
      // without temporarily writing unreserved CSS variables. A transient
      // reservation collapse lets native scroll anchoring move the page.
      const naturalContentRight = naturalColumnRight + (currentContentRight - currentColumnRight);
      let reservation;
      let resolvedGap;
      let resolvedColumnWidth = columnWidth;
      let leftReservation = centeredSlack;

      if (reservationPolicy === 'force') {
        const availableContentLane = Math.max(0, viewportWidth - naturalContentRight);
        if (availableContentLane >= trayLaneWidth) {
          reservation = centeredSlack;
          resolvedGap = Math.max(edgeGap, (viewportWidth - naturalContentRight - trayWidth) / 2);
        } else {
          const targetColumnRight = Math.max(
            naturalColumnLeft,
            viewportWidth - trayWidth - (edgeGap * 2),
          );
          resolvedColumnWidth = Math.min(
            columnWidth,
            Math.max(0, targetColumnRight - naturalColumnLeft),
          );
          reservation = Math.max(
            centeredSlack,
            innerWidth - centeredSlack - resolvedColumnWidth,
          );
          resolvedGap = Math.max(
            edgeGap,
            (viewportWidth - (naturalColumnLeft + resolvedColumnWidth) - trayWidth) / 2,
          );
        }
      } else {
        const availableContentLane = Math.max(0, viewportWidth - naturalContentRight);
        const missingLane = Math.max(0, trayLaneWidth - availableContentLane);
        reservation = centeredSlack + missingLane;
        resolvedGap = missingLane > 0
          ? edgeGap
          : Math.max(edgeGap, (availableContentLane - trayWidth) / 2);
      }

      if (reservationPolicy === 'force' && activeRightTrayKey === 'accounts-detail') {
        const accountsTableScrollableWidth = getAccountsTableScrollableWidth();
        const neededReclaim = Math.max(0, accountsTableScrollableWidth - resolvedColumnWidth + 2);
        const leftReclaim = Math.min(leftReservation, neededReclaim);
        if (leftReclaim > 0) {
          leftReservation -= leftReclaim;
          resolvedColumnWidth += leftReclaim;
        }
      }

      setPxVar('--app-tray-shell-width', trayWidth);
      setPxVar('--app-tray-content-reservation', reservation);
      setPxVar('--app-tray-content-left-reservation', leftReservation);
      setPxVar('--app-tray-column-width', resolvedColumnWidth);
      setPxVar('--app-tray-resolved-edge-gap', resolvedGap);
      setPxVar('--app-tray-panel-gap', resolvedGap);
      setPxVar('--app-tray-edge-offset', resolvedGap);
      if (configuredTrayWidth > 0) {
        setTrayShellProperty('right', resolvedGap);
        setTrayShellProperty('width', trayWidth);
      } else {
        clearTrayShellProperties('right', 'width');
      }
    };
    const scheduleMeasure = () => {
      if (frameId !== null) return;
      frameId = window.requestAnimationFrame(measureTrayReservation);
    };

    measureTrayReservation();
    const observer = new ResizeObserver(scheduleMeasure);
    observer.observe(shell);
    observer.observe(main);
    const column = main.querySelector('.app-main-column');
    if (column) observer.observe(column);
    const mutationObserver = new MutationObserver((mutations) => {
      if (activeRightTrayKey === 'cashflow-detail') {
        const trayStructureChanged = mutations.some((mutation) => (
          [...mutation.addedNodes, ...mutation.removedNodes].some((node) => (
            node.nodeType === Node.ELEMENT_NODE
            && (
              node.matches?.('.app-edge-tray, .app-edge-tray-shell')
              || node.querySelector?.('.app-edge-tray-shell')
            )
          ))
        ));
        if (!trayStructureChanged) return;
      }
      scheduleMeasure();
    });
    mutationObserver.observe(main, { childList: true, subtree: true });
    window.addEventListener('resize', scheduleMeasure);
    window.visualViewport?.addEventListener('resize', scheduleMeasure);

    return () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      observer.disconnect();
      mutationObserver.disconnect();
      window.removeEventListener('resize', scheduleMeasure);
      window.visualViewport?.removeEventListener('resize', scheduleMeasure);
      clearMeasuredTrayVars();
    };
  }, [activeRightTrayKey]);

  useLayoutEffect(() => {
    const shell = toolbarShellRef.current;
    const row = toolbarRowRef.current;
    const left = toolbarLeftRef.current;
    const center = toolbarCenterRef.current;
    const right = toolbarRightRef.current;
    if (!shell || !row || !left || !center || !right) return undefined;

    let frameId = null;
    const WRAP_SAFETY_PX = 8;
    const UNWRAP_HYSTERESIS_PX = 16;
    const getFlexGap = (element) => {
      const styles = window.getComputedStyle(element);
      return parseFloat(styles.columnGap || styles.gap || '0') || 0;
    };
    const getVisibleFlexItems = (element) => {
      return Array.from(element.children).flatMap((child) => {
        const styles = window.getComputedStyle(child);
        if (styles.display === 'none' || styles.visibility === 'hidden') return [];
        if (styles.display === 'contents') return getVisibleFlexItems(child);
        if (child.id?.endsWith('-slot') && child.children.length > 0) return getVisibleFlexItems(child);
        return [child];
      });
    };
    const getClusterNaturalWidth = (element) => {
      const gap = getFlexGap(element);
      const widths = getVisibleFlexItems(element)
        .map((child) => {
          const rectWidth = child.getBoundingClientRect().width;
          return Math.max(rectWidth, child.scrollWidth || 0);
        })
        .filter((width) => width > 0.5);

      return widths.reduce((sum, width) => sum + width, 0) + (Math.max(0, widths.length - 1) * gap);
    };
    const setCenterLeft = (leftPx) => {
      center.style.setProperty('--app-toolbar-center-left', `${Math.round(leftPx)}px`);
    };
    const getToolbarLayout = (gap) => {
      const leftWidth = getClusterNaturalWidth(left);
      const centerWidth = getClusterNaturalWidth(center);
      const rightWidth = getClusterNaturalWidth(right);
      if (centerWidth <= 0.5) {
        return {
          shouldWrap: leftWidth + rightWidth + (leftWidth > 0.5 && rightWidth > 0.5 ? gap : 0) > row.clientWidth,
          centerLeft: 0,
        };
      }
      const available = row.clientWidth;
      const minCenterLeft = leftWidth > 0.5 ? leftWidth + gap : 0;
      const maxCenterLeft = available - centerWidth - (rightWidth > 0.5 ? rightWidth + gap : 0);
      const idealCenterLeft = (available - centerWidth) / 2;
      if (maxCenterLeft < minCenterLeft) {
        return { shouldWrap: true, centerLeft: 0 };
      }
      const centerLeft = Math.min(Math.max(idealCenterLeft, minCenterLeft), maxCenterLeft);
      return {
        shouldWrap: false,
        centerLeft,
      };
    };
    const measure = () => {
      frameId = null;
      const wasWrapped = shell.classList.contains('is-toolbar-wrapped');
      if (wasWrapped) {
        shell.classList.remove('is-toolbar-wrapped');
      }
      setCenterLeft(0);
      let shouldWrap = false;
      try {
        const gap = getFlexGap(row);
        const measurementGap = gap + (wasWrapped ? UNWRAP_HYSTERESIS_PX : WRAP_SAFETY_PX);
        const layout = getToolbarLayout(measurementGap);
        shouldWrap = layout.shouldWrap;
        setCenterLeft(shouldWrap ? 0 : layout.centerLeft);
        setToolbarWrapped((currentlyWrapped) => {
          return shouldWrap === currentlyWrapped ? currentlyWrapped : shouldWrap;
        });
      } finally {
        if (wasWrapped && shouldWrap) {
          shell.classList.add('is-toolbar-wrapped');
        }
      }
    };
    const scheduleMeasure = () => {
      if (frameId !== null) return;
      frameId = window.requestAnimationFrame(measure);
    };

    measure();
    const observer = new ResizeObserver(scheduleMeasure);
    observer.observe(shell);
    observer.observe(row);
    observer.observe(left);
    observer.observe(center);
    observer.observe(right);
    window.addEventListener('resize', scheduleMeasure);
    window.visualViewport?.addEventListener('resize', scheduleMeasure);
    return () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      observer.disconnect();
      center.style.removeProperty('--app-toolbar-center-left');
      window.removeEventListener('resize', scheduleMeasure);
      window.visualViewport?.removeEventListener('resize', scheduleMeasure);
    };
  }, [location.pathname, activeRightTrayKey]);

  useEffect(() => {
    try {
      window.localStorage.setItem(SIDEBAR_EXPANDED_STORAGE_KEY, sidebarExpanded ? 'true' : 'false');
    } catch (_) {
      return;
    }
  }, [sidebarExpanded]);

  useEffect(() => {
    let cancelled = false;

    getDesktopUpdateStatus()
      .then((status) => {
        if (!cancelled) setDesktopUpdateStatus(status);
      })
      .catch(() => {
        if (!cancelled) setDesktopUpdateStatus(null);
      });

    const unsubscribe = subscribeDesktopUpdateStatus((status) => {
      if (!cancelled) setDesktopUpdateStatus(status);
    });

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [promoDemoActive]);

  const handleUpdateNotificationClick = useCallback(() => {
    navigate('/settings');
  }, [navigate]);

  return (
    <CurrencyContext.Provider value={currencyContextValue}>
    <ThemeContext.Provider value={themeContextValue}>
    <RightTrayContext.Provider value={rightTrayContextValue}>
    <div
      ref={appShellRef}
      className={[
        'app-shell',
        isDashboardRoute && 'is-dashboard-route',
        isCashFlowRoute && 'is-cashflow-page',
        effectiveSidebarExpanded && 'is-sidebar-expanded',
        hasModalOverlayOpen && 'has-modal-overlay-open',
        activeRightTrayKey && 'has-right-tray-open',
        activeRightTrayKey && `right-tray-${activeRightTrayKey}`,
        activeRightTrayKey && `right-tray-reservation-${activeRightTrayReservationPolicy}`,
      ].filter(Boolean).join(' ')}
    >
      <aside
        className={[
          'floating-nav-rail',
          effectiveSidebarExpanded ? 'is-expanded' : 'is-compact',
          supportLogsOpen && 'has-open-support-window',
        ].filter(Boolean).join(' ')}
        aria-label="Primary navigation"
      >
        <a
          className="floating-nav-brand"
          href={APP_WEBSITE_URL}
          target="_blank"
          rel="noreferrer"
          aria-label="Visit breaktwenty.com"
          data-tooltip="Visit breaktwenty.com"
          data-tooltip-placement="right"
        >
          <img
            key={`sidebar-logo-${themeMode}-${sidebarBrandAssets.mark.version}`}
            className="floating-nav-brand-logo"
            src={sidebarBrandAssets.mark.src}
            alt=""
          />
          <img
            key={`sidebar-wordmark-${themeMode}-${sidebarBrandAssets.wordmark.version}`}
            className="floating-nav-brand-wordmark"
            src={sidebarBrandAssets.wordmark.src}
            srcSet={sidebarBrandAssets.wordmark.srcSet}
            alt=""
          />
        </a>
        <div className="floating-nav-top-stack">
          <TransactionImportRailNotifier activeImports={activeProviderActivities} />
          <FloatingNavAction
            icon={AddNetWorthSidebarIcon}
            label="Add to Net Worth"
            className="floating-nav-add-btn"
            onClick={() => setShowAddModal(true)}
            sidebarExpanded={effectiveSidebarExpanded}
          />
          <FloatingNavAction
            icon={AddTransactionSidebarIcon}
            label="Add Transactions"
            className="floating-nav-add-btn"
            onClick={() => setShowAddTransactionModal(true)}
            sidebarExpanded={effectiveSidebarExpanded}
          />
        </div>
        <div className="floating-nav-group floating-nav-group-primary">
          {PRIMARY_NAV_ITEMS.map((item) => (
            <FloatingNavItem key={item.label} {...item} sidebarExpanded={effectiveSidebarExpanded} />
          ))}
        </div>
        <FloatingNavSupportLinks sidebarExpanded={effectiveSidebarExpanded} />
        <div className="floating-nav-group floating-nav-group-bottom">
          {showUpdateNotification && (
            <FloatingNavAction
              icon={MdStar}
              label="Update Available"
              className="floating-nav-update-btn"
              onClick={handleUpdateNotificationClick}
              sidebarExpanded={effectiveSidebarExpanded}
            />
          )}
          <SupportLoggingControl
            institutions={data.allInstitutions}
            userTimezone={userTimezone}
            userTimeFormat={userTimeFormat}
            onOpenChange={handleSupportLogsOpenChange}
            sidebarExpanded={effectiveSidebarExpanded}
          />
          {UTILITY_NAV_ITEMS.map((item) => (
            <FloatingNavItem key={item.label} {...item} sidebarExpanded={effectiveSidebarExpanded} />
          ))}
        </div>
      </aside>
      <button
        type="button"
          className="floating-nav-toggle"
        aria-label={effectiveSidebarExpanded ? 'Collapse sidebar' : 'Expand sidebar'}
        aria-expanded={effectiveSidebarExpanded}
        aria-disabled={hasModalOverlayOpen ? 'true' : undefined}
        disabled={hasModalOverlayOpen}
        onClick={handleSidebarToggle}
      >
        <SidebarToggleIcon expanded={effectiveSidebarExpanded} />
      </button>

      <main className="app-main" ref={appMainRef}>
      {/* Toolbar lives inside .app-main so it scrolls out of view with the
          page, but OUTSIDE .app-main-column — the
          column's container-type:inline-size would otherwise scope the
          toolbar's position:fixed popovers/tooltips to the column instead
          of the viewport. CSS gives the toolbar a stable symmetric work-area
          width so right-tray reservation does not move the top row. */}
      <div ref={toolbarShellRef} className={`page-shell-toolbar app-surface-button-scope ${toolbarWrapped ? 'is-toolbar-wrapped' : ''}`.trim()}>
        {/* Row 1 — identity strip. Page identity/tabs sit left, route-fast
            controls can use the measured center slot, and chrome filters sit right. */}
        <div className="app-toolbar-identity-row" ref={toolbarRowRef}>
          <div className="app-toolbar-identity-left" ref={toolbarLeftRef}>
            <h1 className="app-toolbar-identity-title">
              {pageTitleForPathname(location.pathname)}
            </h1>
            {location.pathname === '/holdings' && (
              <div
                id="investments-toolbar-nav-slot"
                className="investments-toolbar-nav-slot"
                aria-label="Investments sections"
              />
            )}
            {isSettingsRoute && <SettingsSubnav />}
            {isHelpRoute && <HelpSubnav />}
          </div>
          <div
            className="app-toolbar-identity-center"
            ref={toolbarCenterRef}
          >
            {location.pathname === '/' && (
              <DashboardClock
                userTimezone={userTimezone}
                userTimeFormat={userTimeFormat}
                frozenNow={showWelcome ? TOUR_DEMO_NOW_ISO : promoDemoActive ? PROMO_DEMO_NOW_ISO : null}
              />
            )}
            {isCashFlowRoute && (
              <div
                id="cash-flow-toolbar-timeline-slot"
                className="cash-flow-toolbar-timeline-slot"
                aria-label="Cash Flow period"
              />
            )}
            {isAccountsRoute && (
              <div
                id="accounts-toolbar-actions-slot"
                className="accounts-toolbar-actions-slot"
                aria-label="Accounts actions"
              />
            )}
          </div>
          <div
            className="app-toolbar-identity-right"
            aria-label="App-wide controls"
            ref={toolbarRightRef}
          >
            {autoSyncInProgress && <ToolbarChip label="Auto-sync live" accent />}
            <AppThemeToggle themeMode={themeMode} onThemeModeChange={setThemeMode} />
            {location.pathname === '/holdings' && (
              <div
                id="investments-toolbar-filters-slot"
                className="toolbar-filter-group toolbar-filter-slot investments-toolbar-filters-slot"
                aria-label="Investments filters"
              />
            )}
            {location.pathname !== '/holdings' && (
              <div className="app-page-controls" aria-label="Page view controls">
                {showsGlobalBalanceToggle && (
                  <div className="app-toolbar-visibility">
                    <BalancesToggleButton className="page-top-balance-toggle" />
                  </div>
                )}
                <div className="app-toolbar-export">
                  <div
                    id="app-toolbar-export-slot"
                    className="app-toolbar-export-slot"
                    aria-label="Export"
                  />
                </div>
                <GlobalToolbarActions
                  pathname={location.pathname}
                  currencyBusy={currencyBusy}
                  currencyOptions={currencyOptions}
                  dashboardTimeframe={dashboardTimeframe}
                  onDashboardTimeframeChange={setDashboardTimeframe}
                  dashboardCustomDateRange={dashboardCustomDateRange}
                  onDashboardCustomDateRangeChange={setDashboardCustomDateRange}
                  primaryCurrency={primaryCurrency}
                  setPrimaryCurrency={setPrimaryCurrency}
                />
              </div>
            )}
          </div>
        </div>
      </div>

        <div className="app-main-column">
          <div className="app-content">
            <AppStatusNotice
              title="App data could not be refreshed"
              message={dataLoadError}
              actionLabel="Retry"
              onAction={onRetryData}
            />
            <AppStatusNotice
              title="Account scope could not be refreshed"
              message={scopeLoadError}
              actionLabel="Retry"
              onAction={onRetryScope}
            />
            {routeContent}
          </div>
        </div>
      </main>

      {showAddModal && (
        <AddToNetWorthModal
          existingProviders={existingSyncProviders}
          onClose={() => setShowAddModal(false)}
          onAuthNeeded={handleAddAuthNeeded}
          onAddManual={() => setShowManualWizard(true)}
          onAddAssetGroup={(category) => setAssetGroupCategory(category)}
          onAddCash={() => setShowCashModal(true)}
        />
      )}

      {showManualWizard && (
        <ManualInstitutionWizard
          onClose={() => setShowManualWizard(false)}
          onBack={() => { setShowManualWizard(false); setShowAddModal(true); }}
          onComplete={() => fetchData()}
        />
      )}

      {assetGroupCategory && (
        <TangibleAssetModal
          category={assetGroupCategory}
          onClose={() => setAssetGroupCategory(null)}
          onBack={() => { setAssetGroupCategory(null); setShowAddModal(true); }}
          onComplete={() => fetchData()}
        />
      )}

      {showCashModal && (
        <CashOpeningModal
          accounts={data.accounts}
          onClose={() => setShowCashModal(false)}
          onBack={() => { setShowCashModal(false); setShowAddModal(true); }}
          onComplete={() => fetchData()}
        />
      )}

      {showAddTransactionModal && (
        <AddTransactionModal
          accounts={data.accounts}
          onClose={() => setShowAddTransactionModal(false)}
          onCreated={() => fetchData()}
        />
      )}

      {showWelcome && (
        <WelcomeModal
          primaryCurrency={primaryCurrency}
          currencyOptions={currencyOptions}
          userTimezone={userTimezone}
          userTimeFormat={userTimeFormat}
          onSaveDefaults={saveWelcomeDefaults}
          onComplete={async () => { setShowWelcome(false); await fetchData({ forceDataRefreshId: true }); }}
          onAddAccount={() => setShowAddModal(true)}
          onRefreshData={fetchData}
        />
      )}

      {addAuthModal && addAuthModalKind === 'scraper' && (
        <ScraperAuthModal
          key={`scraper-auth:${addAuthModal.provider}:${addAuthModal.id || 'new'}:${addAuthModal.isNew ? 'add' : 'existing'}`}
          institution={addAuthModal}
          userTimezone={userTimezone}
          userTimezoneConfigured={userTimezoneConfigured}
          onClose={() => setAddAuthModal(null)}
          onSuccess={handleAddAuthSuccess}
        />
      )}

      {addAuthModal && addAuthModalKind === 'api' && (
        <ApiAuthModal
          institution={addAuthModal}
          onClose={() => setAddAuthModal(null)}
          onSuccess={handleAddAuthSuccess}
        />
      )}
    </div>
    </RightTrayContext.Provider>
    </ThemeContext.Provider>
    </CurrencyContext.Provider>
  );
}

function App() {
  const [promoBoundaryActive, setPromoBoundaryActive] = useState(
    () => PROMO_DEMO_ENABLED && isPromoDemoActive(),
  );
  const [data, setData] = useState({
    networth: null,
    accounts: [],
    institutions: [],
    allInstitutions: [],
    transactionImportStatus: { institutions: [] },
  });
  const [allScopeInstitutions, setAllScopeInstitutions] = useState([]);
  const [dataRefreshId, setDataRefreshId] = useState(0);
  const [userTimezone, setUserTimezone] = useState(DEFAULT_USER_TIMEZONE);
  const [userTimezoneConfigured, setUserTimezoneConfigured] = useState(false);
  const [userTimeFormat, setUserTimeFormat] = useState(DEFAULT_USER_TIME_FORMAT);
  const [loading, setLoading] = useState(true);
  const [showAddModal, setShowAddModal] = useState(false);
  const [showManualWizard, setShowManualWizard] = useState(false);
  const [assetGroupCategory, setAssetGroupCategory] = useState(null);
  const [showCashModal, setShowCashModal] = useState(false);
  const [autoSyncStates, setAutoSyncStates] = useState(null);
  const [addAuthModal, setAddAuthModal] = useState(null);
  const [autoSyncInProgress, setAutoSyncInProgress] = useState(false);
  const [activeSyncBatches, setActiveSyncBatches] = useState([]);
  const [syncNetworkNotice, setSyncNetworkNotice] = useState(null);
  const [syncActivity, setSyncActivity] = useState({ active: [] });
  const [dataLoadError, setDataLoadError] = useState('');
  const [scopeLoadError, setScopeLoadError] = useState('');
  const syncAllBlockingRef = useRef(false);
  const dataRequestCoordinatorRef = useRef(null);
  const scopeRequestCoordinatorRef = useRef(null);
  if (dataRequestCoordinatorRef.current === null) {
    dataRequestCoordinatorRef.current = createLatestRequestCoordinator();
  }
  if (scopeRequestCoordinatorRef.current === null) {
    scopeRequestCoordinatorRef.current = createLatestRequestCoordinator();
  }
  const dataSignatureRef = useRef('');
  const syncActivitySignatureRef = useRef('');
  const transactionImportStatusSignatureRef = useRef('');
  const activeTransactionImportKeysRef = useRef(new Set());
  const activeSyncActivityKeysRef = useRef(new Set());
  const activeSyncBatchesRef = useRef([]);
  const monitoredSyncBatchIdsRef = useRef(new Set(loadMonitoredSyncBatchIds()));
  const awaitingStatusFinalizationRef = useRef(false);
  const initialDataRetryAttemptsRef = useRef(0);
  const handledBackendRecoveriesRef = useRef(new Set());
  const backendRecoveryReconciliationRef = useRef(false);

  useEffect(() => {
    if (!PROMO_DEMO_ENABLED) return undefined;
    const handlePromoDemoChange = (event) => {
      setPromoBoundaryActive(Boolean(event?.detail?.active));
    };
    window.addEventListener(PROMO_DEMO_CHANGE_EVENT, handlePromoDemoChange);
    return () => window.removeEventListener(PROMO_DEMO_CHANGE_EVENT, handlePromoDemoChange);
  }, []);

  useEffect(() => {
    activeSyncBatchesRef.current = activeSyncBatches;
  }, [activeSyncBatches]);

  useEffect(() => {
    let cancelled = false;
    const handleControlFontsLoaded = () => {
      scheduleControlSlotAlignmentAudit();
    };
    document.fonts?.addEventListener('loadingdone', handleControlFontsLoaded);
    void document.fonts?.ready.then(() => {
      if (!cancelled) {
        scheduleControlSlotAlignmentAudit();
      }
    });
    scheduleControlSlotAlignmentAudit();
    void getMainWindowZoomStatus().then((zoomStatus) => {
      if (!cancelled) {
        applyDesktopZoomRimScale(zoomStatus);
      }
    });
    const unsubscribe = subscribeMainWindowZoomStatus((zoomStatus) => {
      applyDesktopZoomRimScale(zoomStatus);
    });
    const handleRendererScaleChange = () => {
      applyRendererDeviceScale(null);
    };
    window.addEventListener('resize', handleRendererScaleChange);
    window.visualViewport?.addEventListener('resize', handleRendererScaleChange);
    return () => {
      cancelled = true;
      document.fonts?.removeEventListener('loadingdone', handleControlFontsLoaded);
      unsubscribe();
      window.removeEventListener('resize', handleRendererScaleChange);
      window.visualViewport?.removeEventListener('resize', handleRendererScaleChange);
    };
  }, []);

  const setSyncActivityIfChanged = useCallback((nextSyncActivity) => {
    const nextSignature = getSyncActivitySignature(nextSyncActivity);
    if (syncActivitySignatureRef.current === nextSignature) {
      return;
    }
    syncActivitySignatureRef.current = nextSignature;
    setSyncActivity({ active: Array.isArray(nextSyncActivity?.active) ? nextSyncActivity.active : [] });
  }, []);

  const getActiveTransactionImportKeys = useCallback((payload) => {
    return getActiveTransactionImportKeysFromPayload(payload);
  }, []);

  const fetchData = useCallback(async ({
    refreshTransactionsOnDataChange = true,
    refreshTransactionsOnSettledImport = false,
    forceDataRefreshId = false,
  } = {}) => {
    const request = dataRequestCoordinatorRef.current.begin();
    setDataLoadError('');
    try {
      const snapshot = await fetchAppSnapshot({ apiBase: API, signal: request.signal });
      if (!request.isCurrent()) return null;
      initialDataRetryAttemptsRef.current = 0;
      const {
        networth,
        accounts,
        institutions,
        allInstitutions,
        transactionImportStatus,
        syncActivity: nextSyncActivity,
        activeSyncBatches: nextActiveSyncBatches,
      } = snapshot;
      setSyncActivityIfChanged(nextSyncActivity);
      setActiveSyncBatches(nextActiveSyncBatches.batches);
      const nextData = {
        networth,
        accounts,
        institutions,
        // Enabled institutions including hidden ones — autosync targets this set so
        // visibility (hidden) never silently stops an institution from syncing.
        allInstitutions,
        transactionImportStatus,
      };
      const previousActiveImportKeys = activeTransactionImportKeysRef.current;
      const nextActiveImportKeys = getActiveTransactionImportKeys(transactionImportStatus);
      const settledImportKeys = [...previousActiveImportKeys].filter(
        (key) => !nextActiveImportKeys.has(key)
      );
      activeTransactionImportKeysRef.current = nextActiveImportKeys;
      transactionImportStatusSignatureRef.current = getTransactionImportStatusSignature(transactionImportStatus);
      const nextDataSignature = getDataSignature(nextData);
      let dataRefreshIdBumped = false;
      if (dataSignatureRef.current !== nextDataSignature) {
        dataSignatureRef.current = nextDataSignature;
        setData(nextData);
        const shouldRefreshTransactions = (
          (refreshTransactionsOnDataChange && nextActiveImportKeys.size === 0)
          || (refreshTransactionsOnSettledImport && settledImportKeys.length > 0)
        );
        if (shouldRefreshTransactions) {
          setDataRefreshId((current) => current + 1);
          dataRefreshIdBumped = true;
        }
      }
      if (forceDataRefreshId && !dataRefreshIdBumped) {
        setDataRefreshId((current) => current + 1);
      }
      return nextData;
    } catch (err) {
      if (!request.isCurrent() || err?.name === 'AbortError') return null;
      console.error('Failed to fetch data:', err);
      setDataLoadError(err?.message || 'App data could not be loaded.');
      return null;
    } finally {
      if (request.finish()) setLoading(false);
    }
  }, [getActiveTransactionImportKeys, setSyncActivityIfChanged]);

  const fetchAllScopeInstitutions = useCallback(async () => {
    const request = scopeRequestCoordinatorRef.current.begin();
    setScopeLoadError('');
    try {
      const institutionsWithAccounts = await fetchScopeInstitutions({
        apiBase: API,
        signal: request.signal,
      });
      if (request.isCurrent()) {
        setAllScopeInstitutions(institutionsWithAccounts);
        return institutionsWithAccounts;
      }
    } catch (err) {
      if (!request.isCurrent() || err?.name === 'AbortError') return null;
      console.error('Failed to fetch all scope institutions:', err);
      setScopeLoadError(err?.message || 'Account scope could not be loaded.');
      return null;
    } finally {
      request.finish();
    }
    return null;
  }, []);

  useEffect(() => {
    if (data.networth === null) return;
    void fetchAllScopeInstitutions();
  }, [data.institutions, data.networth, fetchAllScopeInstitutions]);

  useEffect(() => {
    if (!dataLoadError || data.networth !== null || loading) return undefined;
    const attempt = initialDataRetryAttemptsRef.current + 1;
    initialDataRetryAttemptsRef.current = attempt;
    const retryDelay = Math.min(
      INITIAL_DATA_RETRY_BASE_MS * (2 ** Math.max(0, attempt - 1)),
      INITIAL_DATA_RETRY_MAX_MS,
    );
    const retryTimer = window.setTimeout(() => {
      void fetchData({ refreshTransactionsOnDataChange: false });
    }, retryDelay);
    return () => window.clearTimeout(retryTimer);
  }, [data.networth, dataLoadError, fetchData, loading]);

  useEffect(() => () => {
    dataRequestCoordinatorRef.current.cancel();
    scopeRequestCoordinatorRef.current.cancel();
  }, []);

  useEffect(() => {
    if (PROMO_DEMO_ENABLED && promoBoundaryActive) return undefined;
    const handleSyncActivityEvent = ({ payload }) => {
      const nextSyncActivity = payload || { active: [] };
      const previousActiveActivityKeys = activeSyncActivityKeysRef.current;
      const nextActiveActivityKeys = getActiveSyncActivityKeys(nextSyncActivity);
      const activitySettled = hasSettledSyncActivity(
        previousActiveActivityKeys,
        nextActiveActivityKeys,
      );
      activeSyncActivityKeysRef.current = nextActiveActivityKeys;
      setSyncActivityIfChanged(nextSyncActivity);
      if (activitySettled && !backendRecoveryReconciliationRef.current) {
        awaitingStatusFinalizationRef.current = true;
        void fetchData({
          refreshTransactionsOnDataChange: false,
          refreshTransactionsOnSettledImport: true,
        });
      } else if (
        nextActiveActivityKeys.size === 0
        && awaitingStatusFinalizationRef.current
        && !backendRecoveryReconciliationRef.current
      ) {
        // The post-settle drain committed the final sync_status; refetch once
        // so a chip stuck on auth_required flips to its real state.
        awaitingStatusFinalizationRef.current = false;
        void fetchData({ refreshTransactionsOnDataChange: false });
      }
    };

    const handleTransactionImportStatusEvent = ({ payload }) => {
      const transactionImportStatus = payload || { institutions: [] };
      const previousActiveImportKeys = activeTransactionImportKeysRef.current;
      const nextActiveImportKeys = getActiveTransactionImportKeys(transactionImportStatus);
      const settledImportKeys = [...previousActiveImportKeys].filter(
        (key) => !nextActiveImportKeys.has(key)
      );
      activeTransactionImportKeysRef.current = nextActiveImportKeys;
      const nextSignature = getTransactionImportStatusSignature(transactionImportStatus);
      const signatureChanged = transactionImportStatusSignatureRef.current !== nextSignature;
      const shouldRefreshTransactions = shouldRefreshSettledTransactionImports({
        previousActiveKeys: previousActiveImportKeys,
        nextActiveKeys: nextActiveImportKeys,
        previousSignature: transactionImportStatusSignatureRef.current,
        nextSignature,
      });
      transactionImportStatusSignatureRef.current = nextSignature;
      if (signatureChanged) {
        setData((previousData) => ({
          ...previousData,
          transactionImportStatus,
        }));
      }
      if (shouldRefreshTransactions && settledImportKeys.length > 0) {
        setDataRefreshId((current) => current + 1);
      }
    };

    const handleSyncBatchEvent = ({ payload }) => {
      const batchId = String(payload?.batch_id || '').trim();
      const networkBlocker = getSyncNetworkBlocker(payload);
      if (networkBlocker) {
        setSyncNetworkNotice(networkBlocker);
      }
      if (payload?.results) {
        setData((previousData) => applySyncBatchResultsToAppData(previousData, payload));
      }
      setActiveSyncBatches((current) => reconcileActiveSyncBatches(current, payload));
      if (batchId) {
        if (isTerminalSyncBatch(payload)) {
          monitoredSyncBatchIdsRef.current.delete(batchId);
          forgetMonitoredSyncBatchId(batchId);
          if (!backendRecoveryReconciliationRef.current) {
            void fetchData({ forceDataRefreshId: true });
          }
        } else {
          monitoredSyncBatchIdsRef.current.add(batchId);
          rememberMonitoredSyncBatchId(batchId);
        }
      }
    };

    const unsubscribers = [
      subscribeServerEvent(SERVER_EVENT_TYPES.SYNC_ACTIVITY, handleSyncActivityEvent),
      subscribeServerEvent(SERVER_EVENT_TYPES.TRANSACTION_IMPORT_STATUS, handleTransactionImportStatusEvent),
      subscribeServerEvent(SERVER_EVENT_TYPES.SYNC_BATCH, handleSyncBatchEvent),
    ];
    return () => {
      unsubscribers.forEach((unsubscribe) => unsubscribe());
    };
  }, [fetchData, getActiveTransactionImportKeys, promoBoundaryActive, setSyncActivityIfChanged]);

  useEffect(() => subscribeBackendRecovery((recovery) => {
    backendRecoveryReconciliationRef.current = true;
    void reconcileRendererAfterBackendRecovery({
      recovery,
      handledRecoveryIds: handledBackendRecoveriesRef.current,
      reconnectServerEvents,
      fetchData,
      fetchAllScopeInstitutions,
      acknowledge: acknowledgeBackendRecovery,
    }).finally(() => {
      backendRecoveryReconciliationRef.current = false;
      awaitingStatusFinalizationRef.current = false;
    });
  }), [fetchAllScopeInstitutions, fetchData]);

  useEffect(() => {
    let cancelled = false;
    let controller = null;
    let timeoutId = null;
    const reconcile = async () => {
      controller = new AbortController();
      let nextDelayMs = 5000;
      try {
        const batches = await fetchActiveSyncBatches({ signal: controller.signal });
        if (!cancelled) {
          const activeIds = new Set(
            batches.map((batch) => String(batch?.batch_id || '').trim()).filter(Boolean),
          );
          activeIds.forEach((batchId) => {
            monitoredSyncBatchIdsRef.current.add(batchId);
            rememberMonitoredSyncBatchId(batchId);
          });
          const missingBatchIds = [...monitoredSyncBatchIdsRef.current]
            .filter((batchId) => !activeIds.has(batchId));
          setActiveSyncBatches(batches);
          nextDelayMs = batches.length > 0 ? 1500 : 5000;
          if (missingBatchIds.length > 0) {
            let shouldRefresh = false;
            let unresolvedBatch = false;
            for (const batchId of missingBatchIds) {
              try {
                const batch = await fetchSyncBatch(batchId, { signal: controller.signal });
                if (cancelled) return;
                const networkBlocker = getSyncNetworkBlocker(batch);
                if (networkBlocker) setSyncNetworkNotice(networkBlocker);
                if (batch?.results) {
                  setData((previousData) => applySyncBatchResultsToAppData(previousData, batch));
                }
                if (isTerminalSyncBatch(batch)) {
                  monitoredSyncBatchIdsRef.current.delete(batchId);
                  forgetMonitoredSyncBatchId(batchId);
                  shouldRefresh = true;
                } else {
                  unresolvedBatch = true;
                }
              } catch (error) {
                if (error?.name === 'AbortError') throw error;
                unresolvedBatch = true;
              }
            }
            if (unresolvedBatch) nextDelayMs = 1500;
            if (
              shouldRefresh
              && !cancelled
              && !backendRecoveryReconciliationRef.current
            ) {
              await fetchData({ forceDataRefreshId: true });
            }
          }
        }
      } catch (error) {
        if (!cancelled && error?.name !== 'AbortError') {
          console.warn('Active sync batch reconciliation failed', error);
        }
      } finally {
        if (!cancelled) {
          timeoutId = window.setTimeout(() => { void reconcile(); }, nextDelayMs);
        }
      }
    };
    void reconcile();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timeoutId) window.clearTimeout(timeoutId);
    };
  }, [fetchData]);

  const loadUserSettings = async () => {
    try {
      const resp = await fetch(`${API}/settings`);
      if (!resp.ok) {
        throw new Error('Settings request failed');
      }
      const settings = await resp.json();
      const savedTimezone = String(settings.user_timezone || DEFAULT_USER_TIMEZONE).trim() || DEFAULT_USER_TIMEZONE;
      const nextTimeFormat = normalizeUserTimeFormat(settings.user_time_format);
      const timezoneConfigured = settings.user_timezone_configured !== false;
      const nextTimezone = timezoneConfigured ? savedTimezone : getBrowserTimezone();
      const nextTimezoneConfigured = timezoneConfigured
        || (await persistInitialUserTimezone(nextTimezone, nextTimeFormat));
      setUserTimezone(nextTimezone);
      setUserTimezoneConfigured(nextTimezoneConfigured);
      setUserTimeFormat(nextTimeFormat);
      return {
        userTimezone: nextTimezone,
        userTimezoneConfigured: nextTimezoneConfigured,
        userTimeFormat: nextTimeFormat,
      };
    } catch (_) {
      setUserTimezone(DEFAULT_USER_TIMEZONE);
      setUserTimezoneConfigured(false);
      setUserTimeFormat(DEFAULT_USER_TIME_FORMAT);
      return {
        userTimezone: DEFAULT_USER_TIMEZONE,
        userTimezoneConfigured: false,
        userTimeFormat: DEFAULT_USER_TIME_FORMAT,
      };
    }
  };

  const autoSyncTimer = useRef(null);
  const autoSyncRunning = useRef(false);
  const autoSyncRunIdRef = useRef(0);
  const dataInstitutionsRef = useRef(data.allInstitutions);

  useEffect(() => {
    // Autosync targets every ENABLED institution, including hidden ones — hidden is a
    // display concern, not a sync switch — so source the full enabled list rather than
    // the visibility-filtered /institutions list.
    dataInstitutionsRef.current = data.allInstitutions;
  }, [data.allInstitutions]);

  const getAutoSyncResolvedState = (result, institutionId) => {
    const status = result?.status;
    if (status === 'ok') {
      return {
        status: 'ok',
        justSynced: true,
        institutionId,
        optimisticLastSyncedAt: getAppNow().toISOString(),
      };
    }
    if (status === 'skipped') {
      return {
        status: 'ok',
        justSynced: false,
        institutionId,
      };
    }
    if (isProviderAuthStatus(status)) {
      return { status: 'auth_required', institutionId };
    }
    if (status === 'network_error') {
      return {
        status: 'network_error',
        message: formatSyncErrorMessage(result?.provider || null, result?.message, APP_SYNC_FAILURE_OPTIONS),
        institutionId,
      };
    }
    if (status === 'already_syncing') {
      return { status: 'pending_backend', institutionId };
    }
    return {
      status: 'error',
      message: formatSyncErrorMessage(result?.provider || null, result?.message, APP_SYNC_FAILURE_OPTIONS),
      institutionId,
    };
  };

  useEffect(() => {
    let cancelled = false;

    const runAutoSync = async (institutionsSource = dataInstitutionsRef.current) => {
      if (
        autoSyncRunning.current
        || syncAllBlockingRef.current
        || hasActiveManualSyncBatch(activeSyncBatchesRef.current)
      ) return;
      const insts = (institutionsSource || [])
        .filter((inst) => Boolean(getBackgroundSyncEndpoint(inst.provider)));
      if (insts.length === 0) return;
      if (PROMO_DEMO_ENABLED && isPromoDemoActive()) {
        try {
          recordAutoSyncTrigger(localStorage);
        } catch (err) {
          console.error('Auto-sync could not record its cooldown:', err);
        }
        return;
      }
      let autoSyncAdmission = null;
      try {
        autoSyncAdmission = admitAutoSyncRun({
          acquireLease: acquireAutoSyncLease,
          releaseLease: releaseAutoSyncLease,
          storage: localStorage,
        });
      } catch (err) {
        console.error('Auto-sync could not record its cooldown:', err);
        return;
      }
      if (!autoSyncAdmission) return;
      const { lease: autoSyncLease, runId } = autoSyncAdmission;
      autoSyncRunning.current = true;
      setAutoSyncInProgress(true);
      let needsFinalRefresh = false;
      let moomooSync = null;
      try {
        autoSyncRunIdRef.current = runId;
        const syncing = {};
        insts.forEach((inst) => {
          syncing[String(inst.id)] = {
            status: 'syncing',
            runId,
            provider: inst.provider,
            institutionId: inst.id,
            label: inst.name,
          };
        });
        setAutoSyncStates({ ...syncing });

        const batchInsts = insts.filter((inst) => inst.provider !== 'moomoo');
        const moomooInsts = insts.filter((inst) => inst.provider === 'moomoo');
        const institutionById = new Map(insts.map((inst) => [Number(inst.id), inst]));
        const processedConnections = new Set();
        const applyBatchResult = (connectionKey, result) => {
          const institutionId = Number(result?.institution_id || 0);
          const inst = institutionById.get(institutionId);
          const provider = String(result?.provider || inst?.provider || '').trim();
          const stateKey = String(institutionId || connectionKey);
          if (processedConnections.has(stateKey)) return;
          const status = result?.status;
          if (!status || status === 'pending' || status === 'syncing') return;
          processedConnections.add(stateKey);

          const networkBlocker = getSyncNetworkBlocker(result);
          if (networkBlocker) {
            setSyncNetworkNotice(networkBlocker);
            setAutoSyncStates((prev) => {
              if (!prev || autoSyncRunIdRef.current !== runId) return prev;
              const next = { ...prev };
              delete next[stateKey];
              return next;
            });
            return;
          }
          setSyncNetworkNotice(null);
          const resolvedState = getAutoSyncResolvedState({ ...result, provider }, institutionId);
          const txImportStatus = String(result?.transaction_import_job?.status || '').trim().toLowerCase();
          const awaitingTransactionImport = (
            resolvedState.status === 'ok'
            && ACTIVE_SYNC_STATUSES.has(txImportStatus)
          );
          const stagedState = awaitingTransactionImport
            ? {
                status: 'syncing',
                awaitingTransactionImport: true,
                institutionId,
                optimisticLastSyncedAt: resolvedState.optimisticLastSyncedAt || getAppNow().toISOString(),
              }
            : resolvedState;
          setAutoSyncStates((prev) => {
            if (!prev || autoSyncRunIdRef.current !== runId) return prev;
            return {
              ...prev,
              [stateKey]: {
                ...stagedState,
                runId,
                provider,
                institutionId,
                label: inst?.name,
              },
            };
          });
          if (resolvedState.status === 'ok' && institutionId) {
            needsFinalRefresh = true;
            if (Array.isArray(result.accounts)) {
              setData((prev) => ({
                ...prev,
                accounts: mergeInstitutionAccounts(prev.accounts, institutionId, result.accounts),
              }));
            }
            if (!awaitingTransactionImport) {
              setTimeout(() => {
                setAutoSyncStates((prev) => (
                  prev && prev[stateKey]?.runId === runId
                    ? {
                        ...prev,
                        [stateKey]: {
                          ...prev[stateKey],
                          justSynced: false,
                          optimisticLastSyncedAt:
                            prev[stateKey]?.optimisticLastSyncedAt
                              && (getAppNow().getTime() - new Date(prev[stateKey].optimisticLastSyncedAt).getTime()) <= AUTO_SYNC_OPTIMISTIC_TTL_MS
                              ? prev[stateKey].optimisticLastSyncedAt
                              : null,
                        },
                      }
                    : prev
                ));
              }, 5000);
            }
          }
        };

        const { moomooPromise, batchPromise } = startIndependentSyncLanes({
          startMoomoo: moomooInsts.length > 0
            ? () => Promise.all(moomooInsts.map(async (moomooInst) => {
              try {
                const syncEndpoint = getBackgroundSyncEndpoint(moomooInst.provider);
                const resp = await fetch(`${API}${syncEndpoint}`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({
                    sync_source: 'autosync',
                    institution_id: moomooInst.id,
                  }),
                });
                const result = await resp.json();
                applyBatchResult(`connection:${moomooInst.id}`, result);
              } catch (err) {
                applyBatchResult(`connection:${moomooInst.id}`, {
                  status: 'error',
                  provider: moomooInst.provider,
                  institution_id: moomooInst.id,
                  message: err?.message,
                });
              }
            }))
            : null,
          startBatch: batchInsts.length > 0
            ? () => runSyncBatchUntilDone({
              connections: batchInsts.map((inst) => ({
                provider: inst.provider,
                institution_id: inst.id,
              })),
              mode: 'auto',
              onConnectionResult: (connectionKey, result) => applyBatchResult(connectionKey, result),
            })
            : null,
        });
        moomooSync = moomooPromise;

        if (batchPromise) {
          const batch = await batchPromise;
          const finalStatus = String(batch?.status || '').toLowerCase();
          if (finalStatus === 'blocked') {
            const networkBlocker = getSyncNetworkBlocker(batch);
            if (networkBlocker) {
              setSyncNetworkNotice(networkBlocker);
            }
            setAutoSyncStates((prev) => {
              if (!prev || autoSyncRunIdRef.current !== runId) return prev;
              const next = { ...prev };
              batchInsts.forEach((inst) => {
                if (next[String(inst.id)]?.runId === runId) {
                  delete next[String(inst.id)];
                }
              });
              return next;
            });
            if (moomooSync) {
              await moomooSync;
            }
            if (networkBlocker) {
              setSyncNetworkNotice(networkBlocker);
            }
            if (moomooInsts.length > 0) {
              try {
                await fetchData();
              } catch (refreshErr) {
                console.error('Auto-sync Moomoo refresh failed:', refreshErr);
              }
            }
            return;
          }
          if (!batch || finalStatus === 'not_found' || finalStatus === 'error') {
            throw new Error(batch?.message || 'Auto-sync batch failed');
          }
          setSyncNetworkNotice(null);
        }

        if (moomooSync) {
          await moomooSync;
        }

        await fetchData();
      } catch (err) {
        console.error('Auto-sync failed:', err);
        if (moomooSync) {
          await moomooSync.catch(() => null);
        }
        if (insts.length > 0 && runId) {
          setAutoSyncStates((prev) => {
            if (!prev || autoSyncRunIdRef.current !== runId) return prev;
            const next = { ...prev };
            insts.forEach((inst) => {
              const stateKey = String(inst.id);
              if (next[stateKey]?.status === 'syncing') {
                const failureState = getClientSyncFailureState(inst.provider, err, APP_SYNC_FAILURE_OPTIONS);
                next[stateKey] = {
                  status: failureState.status,
                  message: failureState.message,
                  runId,
                  provider: inst.provider,
                  institutionId: inst.id,
                  label: inst.name,
                };
              }
            });
            return next;
          });
        }
        if (needsFinalRefresh) {
          try {
            await fetchData();
          } catch (refreshErr) {
            console.error('Auto-sync refresh failed:', refreshErr);
          }
        }
      } finally {
        autoSyncRunning.current = false;
        releaseAutoSyncLease(autoSyncLease);
        setAutoSyncInProgress(false);
        // Final sweep: a provider still flagged 'syncing' for this finished run (and NOT
        // legitimately awaiting a background transaction import) had its terminal batch result
        // dropped or returned non-terminal. Clear the stale flag so the optimistic "Syncing"
        // badge can't linger past the run and re-surface on a later re-render — e.g. an
        // institution-scope toggle calling fetchData re-evaluates the merge and would otherwise
        // paint a phantom sync (Coinbase) that never actually ran.
        if (runId) {
          setAutoSyncStates((prev) => {
            if (!prev || autoSyncRunIdRef.current !== runId) return prev;
            let changed = false;
            const next = { ...prev };
            Object.keys(next).forEach((connectionKey) => {
              const state = next[connectionKey];
              if (state?.status === 'syncing' && !state?.awaitingTransactionImport) {
                delete next[connectionKey];
                changed = true;
              }
            });
            return changed ? next : prev;
          });
        }
      }
    };

    const scheduleNext = () => {
      if (autoSyncTimer.current) clearTimeout(autoSyncTimer.current);
      const last = Number(localStorage.getItem(AUTO_SYNC_LAST_TRIGGER_STORAGE_KEY) || '0');
      const delay = Math.max(AUTO_SYNC_COOLDOWN_MS - (Date.now() - last), 60000);
      autoSyncTimer.current = setTimeout(() => {
        runAutoSync().then(() => {
          if (!cancelled) {
            scheduleNext();
          }
        });
      }, delay);
    };

    const initializeAutoSync = async () => {
      const pendingProviders = getPendingInstitutionAddProviders();
      if (pendingProviders.length > 0) {
        await Promise.all(
          pendingProviders.map((provider) => cleanupPendingInstitutionAddOnStartup(provider)),
        );
      }
      if (cancelled) return;
      const [latestData] = await Promise.all([
        fetchData(),
        loadUserSettings(),
      ]);
      if (cancelled) return;
      const loadedInstitutions = latestData?.allInstitutions ?? dataInstitutionsRef.current;
      const lastAutoSync = localStorage.getItem(AUTO_SYNC_LAST_TRIGGER_STORAGE_KEY);
      if (shouldRunStartupAutoSync(lastAutoSync)) {
        runAutoSync(loadedInstitutions).then(() => {
          if (!cancelled) {
            scheduleNext();
          }
        });
      } else {
        scheduleNext();
      }
    };

    initializeAutoSync();

    return () => {
      cancelled = true;
      if (autoSyncTimer.current) clearTimeout(autoSyncTimer.current);
    };
    // Initialize autosync ONCE on mount; the self-scheduling timer (scheduleNext) handles
    // later cycles and reads the live institution list via dataInstitutionsRef, so a data
    // mutation (scope-apply hide, add, delete) no longer re-fires runAutoSync as a side
    // effect (previously a hide/scope change could trigger an unrelated provider sync).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const institutionsList = Array.isArray(data?.transactionImportStatus?.institutions)
      ? data.transactionImportStatus.institutions
      : [];
    const activeTxConnections = new Set();
    institutionsList.forEach((inst) => {
      const institutionId = Number(inst?.institution_id || 0);
      if (!institutionId) return;
      const currentFetchStatus = String(inst?.current_fetch_status || '').trim().toLowerCase();
      const jobStatus = String(inst?.transaction_import_job_status || '').trim().toLowerCase();
      if (ACTIVE_SYNC_STATUSES.has(currentFetchStatus) || ACTIVE_SYNC_STATUSES.has(jobStatus)) {
        activeTxConnections.add(String(institutionId));
      }
    });
    setAutoSyncStates((prev) => {
      if (!prev) return prev;
      let changed = false;
      const next = { ...prev };
      Object.entries(prev).forEach(([connectionKey, state]) => {
        if (!state?.awaitingTransactionImport || activeTxConnections.has(connectionKey)) return;
        const institution = data.allInstitutions.find(
          (item) => String(item.id) === String(connectionKey),
        );
        if (!institution) return;
        const settlement = getTransactionImportSettlementStatus(
          data.transactionImportStatus,
          institution,
        );
        if (settlement === 'active' || settlement === 'unknown') return;
        if (settlement === 'complete' || settlement === 'not_applicable') {
          next[connectionKey] = {
            ...state,
            status: 'ok',
            awaitingTransactionImport: false,
            justSynced: true,
            optimisticLastSyncedAt: getAppNow().toISOString(),
          };
        } else {
          next[connectionKey] = {
            ...state,
            status: settlement === 'auth_required' ? 'auth_required' : TRANSACTION_IMPORT_RETRY_STATUS,
            message: getTransactionImportIssueMessage(data.transactionImportStatus, institution),
            awaitingTransactionImport: false,
            justSynced: false,
            optimisticLastSyncedAt: null,
          };
        }
        changed = true;
        // Must schedule inside the updater — under React automatic batching, an outer array pushed to here is empty by the time the effect's synchronous forEach runs.
        setTimeout(() => {
          setAutoSyncStates((p) => {
            if (!p || !p[connectionKey]?.justSynced) return p;
            return {
              ...p,
              [connectionKey]: { ...p[connectionKey], justSynced: false },
            };
          });
        }, 5000);
      });
      return changed ? next : prev;
    });
  }, [data.allInstitutions, data.transactionImportStatus]);

  const handleAddAuthNeeded = async (inst) => {
    if (!getProviderAddAuthModal(inst.provider)) {
      return;
    }
    clearInterruptedInstitutionAdd(inst.provider);
    setAddAuthModal({ ...inst, isNew: true });
  };

  const handleAddAuthSuccess = useCallback(() => {
    setAddAuthModal(null);
    void fetchData();
  }, [fetchData]);

  if (loading) {
    return <AppBrandLoadingState />;
  }
  if (dataLoadError && data.networth === null) {
    return (
      <div className="app app-error-state" role="alert">
        <AppStatusNotice
          title="App data could not be loaded"
          message={dataLoadError}
          actionLabel="Retry"
          onAction={() => {
            setLoading(true);
            void fetchData();
          }}
        />
      </div>
    );
  }

  return (
    <Router>
      <ShellContent
        data={data}
        dataRefreshId={dataRefreshId}
        fetchData={fetchData}
        allScopeInstitutions={allScopeInstitutions}
        fetchAllScopeInstitutions={fetchAllScopeInstitutions}
        userTimezone={userTimezone}
        userTimezoneConfigured={userTimezoneConfigured}
        setUserTimezone={setUserTimezone}
        setUserTimezoneConfigured={setUserTimezoneConfigured}
        userTimeFormat={userTimeFormat}
        setUserTimeFormat={setUserTimeFormat}
        autoSyncStates={autoSyncStates}
        setAutoSyncStates={setAutoSyncStates}
        autoSyncInProgress={autoSyncInProgress}
        activeSyncBatches={activeSyncBatches}
        syncNetworkNotice={syncNetworkNotice}
        setSyncNetworkNotice={setSyncNetworkNotice}
        syncActivity={syncActivity}
        dataLoadError={dataLoadError}
        scopeLoadError={scopeLoadError}
        onRetryData={() => { void fetchData(); }}
        onRetryScope={() => { void fetchAllScopeInstitutions(); }}
        showAddModal={showAddModal}
        setShowAddModal={setShowAddModal}
        showManualWizard={showManualWizard}
        setShowManualWizard={setShowManualWizard}
        assetGroupCategory={assetGroupCategory}
        setAssetGroupCategory={setAssetGroupCategory}
        showCashModal={showCashModal}
        setShowCashModal={setShowCashModal}
        addAuthModal={addAuthModal}
        setAddAuthModal={setAddAuthModal}
        handleAddAuthNeeded={handleAddAuthNeeded}
        handleAddAuthSuccess={handleAddAuthSuccess}
        syncAllBlockingRef={syncAllBlockingRef}
      />
      <GlobalTooltip />
    </Router>
  );
}

export { BreakTwentyErrorBoundary };
export default App;
