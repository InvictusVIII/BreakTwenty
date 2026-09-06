import React, { useState, useMemo, useEffect, useLayoutEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import CsvExportButton from '../components/CsvExportButton';
import AppStatusNotice from '../components/AppStatusNotice';
import { getTourHint, isTourDemoActive } from '../components/tourDemoData';
import EditableAccountName from '../components/EditableAccountName';
import InstitutionAccountSelector, { ScopeSelectorTrigger, formatScopeSelectionSummary } from '../components/InstitutionAccountSelector';
import CategoryPill from '../components/CategoryPill';
import ScraperAuthModal from '../components/ScraperAuthModal';
import InstitutionSettingsModal from '../components/InstitutionSettingsModal';
import ApiAuthModal from '../components/ApiAuthModal';
import SortableTableHeader, { getNextSortConfig, usePersistentSortConfig } from '../components/SortableTableHeader';
import { MdArrowBack, MdCheck, MdErrorOutline, MdHourglassEmpty, MdInfoOutline, MdSync, MdSettings, MdUnfoldLess, MdUnfoldMore, MdLink } from 'react-icons/md';
import InstitutionLogo from '../components/InstitutionLogo';
import ProviderSyncStatus from '../components/ProviderSyncStatus';
import HorizontalScrollProxy from '../components/HorizontalScrollProxy';
import ControlChevron from '../components/ControlChevron';
import TriangleIcon from '../components/TriangleIcon';
import AccountTypeBadge from '../components/AccountTypeBadge';
import { useCurrency, useRightTrayOpenState, useTheme } from '../appState';
import AccountWebsiteIcon from '../assets/icons/account-website-icon.svg?react';
import useDismissibleLayer, { APP_NON_DISMISS_INTERACTION_SELECTOR } from '../hooks/useDismissibleLayer';
import useBalancesHidden from '../hooks/useBalancesHidden';
import FitMoney from '../components/FitMoney';
import EChart from '../components/charts/EChart';
import { SideDetailDrawerPanel } from '../components/SideDetailDrawer';
import {
  buildFxHistoryIndex,
  makeHistoricalCurrencyConverter,
  NATIVE_VIEW_CURRENCY,
  makeCurrencyConverter,
  resolveGroupDisplayCurrency,
} from '../utils/currencyView';
import { getTransactionPrimaryDescription } from '../utils/transactionDescription';
import { getAppNow, getAppNowMs } from '../utils/appClock';
import {
  DEFAULT_SYNC_REQUEST_TIMEOUT_MS,
  USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS,
  fetchWithTimeout,
} from '../utils/syncRequests';
import { formatSyncErrorMessage, getClientSyncFailureState } from '../utils/clientSyncFailure';
import { escapeHtml as escapeTooltipHtml } from '../utils/html';
import {
  getActiveSyncBatchConnectionStates,
  getActiveSyncBatchInstitutionIds,
  getSyncNetworkBlocker,
  hasActiveManualSyncBatch,
  runSyncBatchUntilDone,
} from '../utils/syncBatch';
import {
  findTransactionImportInstitution,
  getTransactionImportIssueMessage,
  getTransactionImportSettlementStatus,
  isProviderAuthStatus,
  resolveProviderSyncDisplay,
  TRANSACTION_IMPORT_RETRY_STATUS,
} from '../utils/syncDisplayState';
import { startIndependentSyncLanes } from '../utils/syncOrchestration';
import {
  buildAccountsDetailRecentTransactionsUrl,
  buildInstitutionSyncRequestBody,
  getImportStatusDetail,
  shouldShowAccountsDetailSync,
} from '../utils/accountsViewUtils';
import { API } from '../config';
import {
  getProviderAuthAction,
  getBackgroundSyncEndpoint,
  getUserInitiatedSyncEndpoint,
} from '../constants/providers';
import { breaktwentyChartText } from '../theme/typography';
import {
  cloneScopeInstitutions,
  PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
  PORTFOLIO_TIMEFRAMES,
  DEFAULT_PORTFOLIO_TIMEFRAME,
  formatMoneyParts,
  getAccountChange,
  getAccountDisplayBalance,
  getAccountGroupChangeSummary,
  parseSyncedAt,
  timeAgo,
} from '../utils/portfolioViewUtils';
import { getAccountTypeBadgeClass } from '../utils/accountType';
import { readTransactionCollectionResponse } from '../utils/apiResponse';
import { formatLongDateValue } from '../utils/date';
import { persistVisibilityScope, reconcileVisibilityScope } from '../utils/visibilityScope';
import './Accounts.css';

const ACCOUNTS_DETAIL_TRAY_TRANSITION_MS = 360;
const ACCOUNTS_GROUP_COLLAPSE_MS = 240;

function cssLengthToPixels(value, element) {
  const trimmedValue = value.trim();
  const numericValue = Number.parseFloat(trimmedValue);
  if (!Number.isFinite(numericValue)) return 0;
  if (trimmedValue.endsWith('rem')) {
    const rootFontSize = Number.parseFloat(getComputedStyle(document.documentElement).fontSize);
    return Number.isFinite(rootFontSize) ? numericValue * rootFontSize : 0;
  }
  if (trimmedValue.endsWith('em')) {
    const fontSize = Number.parseFloat(getComputedStyle(element).fontSize);
    return Number.isFinite(fontSize) ? numericValue * fontSize : 0;
  }
  return numericValue;
}

const ACCOUNTS_HORIZONTAL_SCROLL_PROXY_OPTIONS = {
  className: 'accounts-table-horizontal-scrollbar',
  innerClassName: 'accounts-table-horizontal-scrollbar-inner',
  contentWidthProperty: '--accounts-horizontal-scroll-measured-content-width',
  controllerMeasurementProperty: '--accounts-horizontal-scroll-measured-content-width',
  targetViewportProperty: '--horizontal-scroll-viewport-width',
  targetScrollProperty: '--horizontal-scroll-left',
  mapping: 'proportional',
  resetOnTargetWidthChange: true,
  clampTolerance: 0,
  contentWidthTolerance: 0,
  resolveTarget: (controller) => controller.closest('[data-accounts-horizontal-scroll-sync]'),
  getContentWidth: ({ target }) => target.scrollWidth,
  getVisibilityBuffer: ({ target }) => cssLengthToPixels(
    getComputedStyle(target).getPropertyValue('--accounts-scroll-shadow-gutter'),
    target,
  ),
  getMinimumControllerRange: ({ controller }) => cssLengthToPixels(
    getComputedStyle(controller).getPropertyValue('--accounts-horizontal-scroll-min-range'),
    controller,
  ),
  getObservedElements: ({ controller, target }) => [
    target,
    ...Array.from(target.children).filter((child) => child !== controller),
  ],
};

function AccountsHorizontalScrollProxy({ className = '' }) {
  return <HorizontalScrollProxy options={ACCOUNTS_HORIZONTAL_SCROLL_PROXY_OPTIONS} className={className} />;
}

function getInstitutionLogoUrl(inst) {
  if (!inst?.has_logo) return undefined;
  const version = inst.logo_version;
  return `${API}/institutions/${inst.id}/logo${version ? `?v=${version}` : ''}`;
}

// Splits a balance into a symbol "zone" + the number. The number block has a fixed
// min-width and right-aligns, so it expands leftward and the symbol — right-aligned
// just left of it — sits at a consistent position across rows. Glyph symbols
// ($, £, ₿) glue to the number; letter symbols (CHF, Kč) take a trailing space,
// matching formatMoney's spacing rule.
function balanceFragment(value, currency) {
  const abs = Math.abs(Number(value) || 0);
  const parts = formatMoneyParts(abs, currency);
  const compactParts = formatMoneyParts(abs, currency, { compact: true });
  const symbol = parts ? parts.currencySymbol : '$';
  const spaced = /\p{L}$/u.test(symbol);
  const prefix = `${Number(value) < 0 ? '-' : ''}${spaced ? `${symbol} ` : symbol}`;
  // Symbol + amount are ONE FitMoney unit: the symbol hugs the number, the pair
  // centers in its bounded slot, and it compacts to "Kč 20.4T" (full on hover)
  // only when it would overflow — so no balance clips, however large.
  return (
    <FitMoney
      className="balance-amount"
      full={`${prefix}${parts ? parts.amountText : '—'}`}
      compact={`${prefix}${compactParts ? compactParts.amountText : '—'}`}
    />
  );
}

function formatAccountsDateLabel(value) {
  if (!value) return '';
  const [year, month, day] = String(value).slice(0, 10).split('-').map(Number);
  const parsed = year && month && day ? new Date(year, month - 1, day) : null;
  if (!parsed || Number.isNaN(parsed.getTime())) return String(value);

  return parsed.toLocaleDateString('en-CA', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function getCustomRangeEndDate(customDateRange) {
  return customDateRange?.end || customDateRange?.start || '';
}

function getCustomRangeStartDate(customDateRange) {
  return customDateRange?.start || '';
}

function formatAccountsDateKey(value) {
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) return '';
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
  }
  return value ? String(value).slice(0, 10) : '';
}

function getRollingRangeStartDate(timeframe) {
  const config = PORTFOLIO_TIMEFRAMES.find((item) => item.label === timeframe);
  if (!config) return '';
  if (config.days === 'ytd') {
    return `${getAppNow().getFullYear()}-01-01`;
  }
  const dayCount = Number(config.days);
  if (!Number.isFinite(dayCount)) return '';
  const startDate = getAppNow();
  startDate.setDate(startDate.getDate() - dayCount);
  return formatAccountsDateKey(startDate);
}

function getAccountsTimelineRange(timeframe, customDateRange) {
  if (timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY) {
    return {
      start: getCustomRangeStartDate(customDateRange),
      end: getCustomRangeEndDate(customDateRange),
    };
  }
  if (timeframe === 'All') {
    return { start: '', end: '' };
  }
  return {
    start: getRollingRangeStartDate(timeframe),
    end: formatAccountsDateKey(getAppNow()),
  };
}

function getAccountsTimelineDisplayLabel(timeframe, customDateRange) {
  if (timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY) {
    const startLabel = formatAccountsDateLabel(getCustomRangeStartDate(customDateRange));
    const endLabel = formatAccountsDateLabel(getCustomRangeEndDate(customDateRange));
    return startLabel && endLabel ? `${startLabel} - ${endLabel}` : 'Custom Range';
  }
  return PORTFOLIO_TIMEFRAMES.find((item) => item.label === timeframe)?.triggerLabel || timeframe;
}

function moneyText(value, currency, { compact = false, alwaysSign = false } = {}) {
  const num = Number(value) || 0;
  const abs = Math.abs(num);
  const parts = formatMoneyParts(abs, currency, { compact });
  const symbol = parts ? parts.currencySymbol : '$';
  const spaced = /\p{L}$/u.test(symbol);
  const prefix = `${alwaysSign && num > 0 ? '+' : num < 0 ? '-' : ''}${spaced ? `${symbol} ` : symbol}`;
  return `${prefix}${parts ? parts.amountText : '—'}`;
}

function changeMoneyText(value, currency, { compact = false } = {}) {
  return moneyText(value, currency, { compact, alwaysSign: true });
}

function ChangeValue({ diff, currency, startValue = null, startDate = null, showStart = false }) {
  const amountFull = changeMoneyText(diff, currency);
  const startLabel = startDate ? `Start ${formatAccountsDateLabel(startDate)}` : 'Start';
  return (
    <span className={`accounts-change-stack ${showStart ? 'has-start' : ''}`.trim()}>
      <FitMoney
        className="accounts-change-amount"
        full={amountFull}
        compact={changeMoneyText(diff, currency, { compact: true })}
      />
      {showStart ? (
        <span className="accounts-change-start">
          <span className="accounts-change-start-label">{startLabel}</span>
          <FitMoney
            className="accounts-change-start-money"
            full={moneyText(startValue, currency)}
            compact={moneyText(startValue, currency, { compact: true })}
          />
        </span>
      ) : null}
    </span>
  );
}

function TimelineValueCell({ label, date, value, currency, hidden, className = '', showDate = true, showLabel = true }) {
  const numericValue = Number(value);
  const hasValue = value !== null && value !== undefined && value !== '' && Number.isFinite(numericValue);
  const tone = !hasValue ? 'empty' : hidden ? 'accounts-value-hidden' : numericValue > 0 ? 'positive' : numericValue < 0 ? 'negative' : 'zero';
  const moneyFull = hasValue ? `${moneyText(numericValue, currency)} ${currency}` : '';
  const moneyCompact = hasValue ? `${moneyText(numericValue, currency, { compact: true })} ${currency}` : '';
  return (
    <span className={`accounts-timeline-value ${tone} ${className}`.trim()}>
      {showLabel ? (
        <span className="accounts-timeline-label">
          <span>{label}</span>
          {showDate && date ? <span className="accounts-timeline-date">{formatAccountsDateLabel(date)}</span> : null}
        </span>
      ) : null}
      <span className="accounts-timeline-money">
        {hasValue ? (
          hidden ? (
            <span className="balance-amount">****** {currency}</span>
          ) : (
            <FitMoney className="balance-amount" full={moneyFull} compact={moneyCompact} />
          )
        ) : (
          <span className="accounts-timeline-empty">—</span>
        )}
      </span>
    </span>
  );
}

function AccountBalanceCell({ value, currency, isLiability, hidden }) {
  // Liability balance is net owed: negate it — you-owe → negative/red, a credit balance
  // (overpaid) → positive/green, since the lender then owes you.
  const signed = isLiability ? -value : value;
  // Colour by the SIGN of the displayed value, not just the asset/liability flag, so a
  // negative asset (e.g. an overdrawn cash holder) reads red like the negative it is,
  // instead of asset-green that looks positive.
  const tone = hidden ? 'accounts-value-hidden' : signed === 0 ? 'zero' : signed < 0 ? 'liability' : 'asset';
  return (
    <span className={`account-balance ${tone}`}>
      {hidden ? <span className="balance-amount">******</span> : balanceFragment(signed, currency)}
      <span className="account-currency">{currency}</span>
    </span>
  );
}

const AUTO_SYNC_OPTIMISTIC_TTL_MS = 2 * 60 * 1000;
const CLIENT_SYNC_FAILURE_TTL_MS = 15 * 60 * 1000;
const PROVIDER_SYNC_SUCCESS_DISPLAY_MS = 5000;
const ACCOUNT_GROUP_BY_INSTITUTION = 'institution';
const ACCOUNT_GROUP_BY_CATEGORY = 'category';
const ACCOUNT_GROUP_BY_KIND = 'kind';
const ACCOUNT_GROUP_MODES = [
  { key: ACCOUNT_GROUP_BY_INSTITUTION, label: 'Institution' },
  { key: ACCOUNT_GROUP_BY_CATEGORY, label: 'Account Type' },
  { key: ACCOUNT_GROUP_BY_KIND, label: 'Assets & Liabilities' },
];
// Coarse two-way split for the 'kind' group mode — assets vs liabilities, nothing more.
const ASSET_LIABILITY_GROUPS = [
  { key: 'assets', label: 'Assets' },
  { key: 'liabilities', label: 'Liabilities' },
];
const ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS = {
  institution: 'asc',
  sync: 'desc',
  start: 'desc',
  change: 'desc',
  balance: 'desc',
};
const ACCOUNT_GROUP_BY_STORAGE_KEY = 'breaktwenty_accounts_group_by_v1';
const ACCOUNT_LIST_SORT_STORAGE_KEY = 'breaktwenty_accounts_account_list_sort_v1';

function isSameAccountsDetailSelection(current, next) {
  if (!current || !next || current.kind !== next.kind) return false;
  if (next.kind === 'institution') {
    return String(current.institutionId) === String(next.institutionId);
  }
  if (next.kind === 'account') {
    return String(current.accountId) === String(next.accountId);
  }
  return false;
}

const ACCOUNT_CATEGORY_GROUPS = [
  { key: 'cash_banking', label: 'Cash & Banking' },
  { key: 'investments', label: 'Investments' },
  { key: 'credit_cards', label: 'Credit Cards' },
  { key: 'loans_debt', label: 'Loans & Debt' },
  { key: 'real_estate', label: 'Real Estate' },
  { key: 'vehicles', label: 'Vehicles' },
  { key: 'valuables', label: 'Valuables' },
  { key: 'private_investments', label: 'Private Investments' },
  { key: 'other_assets', label: 'Other Assets' },
  { key: 'other', label: 'Other' },
];
const ACCOUNT_CATEGORY_BY_TYPE = {
  banking: 'cash_banking',
  cash: 'cash_banking',
  chequing: 'cash_banking',
  checking: 'cash_banking',
  savings: 'cash_banking',
  brokerage: 'investments',
  brokerage_cash: 'investments',
  crypto: 'investments',
  fhsa: 'investments',
  investment: 'investments',
  lira: 'investments',
  margin: 'investments',
  resp: 'investments',
  rrif: 'investments',
  rrsp: 'investments',
  taxable: 'investments',
  tfsa: 'investments',
  rdsp: 'investments',
  lrsp: 'investments',
  lif: 'investments',
  lrif: 'investments',
  prif: 'investments',
  rpp: 'investments',
  dpsp: 'investments',
  spp: 'investments',
  nreg: 'investments',
  '401k': 'investments',
  ira: 'investments',
  roth_ira: 'investments',
  credit_card: 'credit_cards',
  auto_loan: 'loans_debt',
  line_of_credit: 'loans_debt',
  loan: 'loans_debt',
  loc: 'loans_debt',
  heloc: 'loans_debt',
  mortgage: 'loans_debt',
  student_loan: 'loans_debt',
  personal_loan: 'loans_debt',
  other_debt: 'loans_debt',
  real_estate: 'real_estate',
  primary_residence: 'real_estate',
  secondary_residence: 'real_estate',
  rental_property: 'real_estate',
  investment_property: 'real_estate',
  land: 'real_estate',
  other_real_estate: 'real_estate',
  car: 'vehicles',
  boat: 'vehicles',
  motorcycle: 'vehicles',
  snowmobile: 'vehicles',
  bicycle: 'vehicles',
  other_vehicle: 'vehicles',
  art: 'valuables',
  jewelry: 'valuables',
  collectibles: 'valuables',
  furniture: 'valuables',
  other_valuable: 'valuables',
  private_equity: 'private_investments',
  business: 'private_investments',
  private_equity_fund: 'private_investments',
  venture_capital_fund: 'private_investments',
  private_loan: 'private_investments',
  insurance: 'private_investments',
  other_investment: 'private_investments',
  other_asset: 'other_assets',
};

function loadAccountGroupByPreference() {
  const savedGroupBy = localStorage.getItem(ACCOUNT_GROUP_BY_STORAGE_KEY);
  return ACCOUNT_GROUP_MODES.some((mode) => mode.key === savedGroupBy)
    ? savedGroupBy
    : ACCOUNT_GROUP_BY_INSTITUTION;
}

function getInstitutionGroupKey(institutionId) {
  return `institution:${institutionId}`;
}

function getCategoryGroupKey(categoryKey) {
  return `category:${categoryKey}`;
}

function getAccountCategoryKey(account) {
  const accountType = getAccountTypeBadgeClass(account?.account_type);
  if (ACCOUNT_CATEGORY_BY_TYPE[accountType]) {
    return ACCOUNT_CATEGORY_BY_TYPE[accountType];
  }
  return account?.is_liability ? 'loans_debt' : 'other';
}

function getAccountKindKey(account) {
  return account?.is_liability ? 'liabilities' : 'assets';
}

// Resolves the group set + per-account key fn for a grouped (non-institution) mode, so the
// category and assets/liabilities views share one rendering path.
function getGroupingForMode(groupBy) {
  if (groupBy === ACCOUNT_GROUP_BY_KIND) {
    return { groups: ASSET_LIABILITY_GROUPS, getKey: getAccountKindKey };
  }
  return { groups: ACCOUNT_CATEGORY_GROUPS, getKey: getAccountCategoryKey };
}

function sortAccountsForAccountsPanel(a, b, getBalance = (account) => Number(account.balance) || 0) {
  if (a.is_liability !== b.is_liability) return a.is_liability ? 1 : -1;
  const balanceDiff = getBalance(b) - getBalance(a);
  if (balanceDiff !== 0) return balanceDiff;
  return String(a.name || '').localeCompare(String(b.name || ''));
}

function compareNullableNumbers(leftValue, rightValue, direction = 'asc') {
  const leftNumber = Number(leftValue);
  const rightNumber = Number(rightValue);
  const leftMissing = leftValue === null || leftValue === undefined || leftValue === '' || !Number.isFinite(leftNumber);
  const rightMissing = rightValue === null || rightValue === undefined || rightValue === '' || !Number.isFinite(rightNumber);
  if (leftMissing && rightMissing) return 0;
  if (leftMissing) return 1;
  if (rightMissing) return -1;
  const result = leftNumber - rightNumber;
  return direction === 'desc' ? -result : result;
}

function getAccountGroupNetTotal(groupAccounts, convert, getBalance = (account) => Number(account.balance) || 0) {
  const totalAssets = groupAccounts
    .filter((account) => !account.is_liability)
    .reduce((total, account) => total + convert(getBalance(account), account.currency), 0);
  const totalLiabilities = groupAccounts
    .filter((account) => account.is_liability)
    .reduce((total, account) => total + convert(getBalance(account), account.currency), 0);
  return totalAssets - totalLiabilities;
}

function compareAccountListSortValues(left, right, sortConfig) {
  if (!sortConfig) return 0;
  let result = 0;
  if (sortConfig.key === 'institution') {
    result = String(left.label || '').localeCompare(String(right.label || ''));
    return sortConfig.direction === 'desc' ? -result : result;
  }
  if (sortConfig.key === 'change') {
    result = compareNullableNumbers(
      left.changeSummary?.hasData && !left.changeSummary?.hasFallbackOnlyData ? left.changeSummary.diff : null,
      right.changeSummary?.hasData && !right.changeSummary?.hasFallbackOnlyData ? right.changeSummary.diff : null,
      sortConfig.direction,
    );
    return result;
  }
  if (sortConfig.key === 'start') {
    result = compareNullableNumbers(left.startTotal, right.startTotal, sortConfig.direction);
    return result;
  }
  if (sortConfig.key === 'balance') {
    result = compareNullableNumbers(left.netTotal, right.netTotal, sortConfig.direction);
    return result;
  }
  if (sortConfig.key === 'sync') {
    result = compareNullableNumbers(left.syncTimestamp, right.syncTimestamp, sortConfig.direction);
    return result;
  }
  return 0;
}

function AccountGroupControl({ value, onChange }) {
  const [isOpen, setIsOpen] = useState(false);
  const popoverRef = useRef(null);
  const selectedMode = ACCOUNT_GROUP_MODES.find((mode) => mode.key === value) || ACCOUNT_GROUP_MODES[0];
  const closePopover = useCallback(() => setIsOpen(false), []);

  useDismissibleLayer({
    open: isOpen,
    ref: popoverRef,
    onDismiss: closePopover,
  });

  const handleSelect = (modeKey) => {
    onChange(modeKey);
    setIsOpen(false);
  };

  return (
    <div className={`accounts-group-popover ${isOpen ? 'is-open' : ''}`.trim()} ref={popoverRef}>
      <button
        type="button"
        className={`investments-filter-trigger app-view-filter-trigger app-control-root accounts-group-trigger ${isOpen ? 'is-open' : ''}`.trim()}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        onClick={() => setIsOpen((previous) => !previous)}
      >
        <span className="investments-filter-trigger-icon app-view-filter-trigger-icon app-control-icon accounts-group-trigger-icon" aria-hidden="true">
          <AccountWebsiteIcon focusable="false" />
        </span>
        <span className="investments-view-filter-trigger-label app-view-filter-trigger-label app-control-label accounts-group-summary">{selectedMode.label}</span>
        <span className={`investments-filter-trigger-chevron app-control-chevron accounts-group-chevron ${isOpen ? 'is-open' : ''}`.trim()} aria-hidden="true">
          <ControlChevron />
        </span>
      </button>
      <div
        className={`accounts-group-panel transactions-toolbar-menu-list ${isOpen ? 'is-open' : ''}`.trim()}
        role="menu"
        aria-label="Group accounts by"
        aria-hidden={!isOpen}
      >
        {ACCOUNT_GROUP_MODES.map((mode) => {
          const isSelected = value === mode.key;
          return (
            <button
              key={mode.key}
              type="button"
              role="menuitemradio"
              aria-checked={isSelected}
              className={`accounts-group-menu-item transactions-toolbar-menu-item ${isSelected ? 'is-selected' : ''}`.trim()}
              onClick={() => handleSelect(mode.key)}
            >
              <span className="transactions-toolbar-menu-item-copy">
                <span className="accounts-group-menu-item-label transactions-toolbar-menu-item-label">{mode.label}</span>
              </span>
              <span className={`transactions-toolbar-checkbox ${isSelected ? 'is-selected' : ''}`.trim()} aria-hidden="true">
                {isSelected ? <MdCheck size={14} /> : null}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function getMostRecentSyncedAt(primaryDateStr, fallbackDateStr) {
  const primary = parseSyncedAt(primaryDateStr);
  const fallback = parseSyncedAt(fallbackDateStr);

  if (!primary) return fallbackDateStr || null;
  if (!fallback) return primaryDateStr || null;
  return primary >= fallback ? primaryDateStr : fallbackDateStr;
}

function getActiveOptimisticSyncedAt(dateStr, ttlMs = AUTO_SYNC_OPTIMISTIC_TTL_MS) {
  const parsed = parseSyncedAt(dateStr);
  if (!parsed) return null;
  return (getAppNowMs() - parsed.getTime()) <= ttlMs ? dateStr : null;
}

function getInstLastSynced(accounts, instId, optimisticDateStr = null) {
  const instAccounts = accounts.filter((a) => a.institution_id === instId);
  if (instAccounts.length === 0) return optimisticDateStr;
  const synced = instAccounts
    .map((a) => a.last_synced)
    .filter(Boolean)
    .sort((a, b) => new Date(b) - new Date(a));
  return getMostRecentSyncedAt(synced.length > 0 ? synced[0] : null, optimisticDateStr);
}

const MESSAGE_PRESERVING_STATUSES = new Set(['network_error', 'error', 'error_flex', 'error_scraper']);
// Failure statuses that, once a sync result reports them, must not be reconciled
// back to a (possibly stale) healthier backend sync_status while the provider is
// still settling — the pending-status accumulator holds the real write until the
// provider family goes idle, so the DB can briefly still read the previous "ok".
const SETTLING_PRESERVED_FAILURE_STATUSES = new Set([
  'auth_required',
  'different_profile_detected',
  'network_error',
  'error',
  'error_flex',
  'error_scraper',
]);
const ACTIVE_TRANSACTION_IMPORT_JOB_STATUSES = new Set(['queued', 'running']);
const EMPTY_TRANSACTION_IMPORT_STATUS = { institutions: [] };

function connectionStateKey(institutionOrId) {
  const value = typeof institutionOrId === 'object'
    ? institutionOrId.id
    : institutionOrId;
  return String(Number(value || 0));
}

function getTransactionImportSyncStatus(payload, institution) {
  const settlement = getTransactionImportSettlementStatus(payload, institution);
  if (settlement === 'active') return 'syncing';
  if (settlement === 'auth_required') return 'auth_required';
  if (settlement === 'failed') return TRANSACTION_IMPORT_RETRY_STATUS;
  return null;
}

function getTransactionImportFailureMessage(payload, institution) {
  return getTransactionImportIssueMessage(payload, institution, 'Transaction import retry needed');
}

function isRecentClientSyncFailure(localState) {
  if (!localState?.clientOnly || !MESSAGE_PRESERVING_STATUSES.has(localState.status)) {
    return false;
  }
  const parsed = parseSyncedAt(localState.updatedAt);
  return Boolean(parsed && (getAppNowMs() - parsed.getTime()) <= CLIENT_SYNC_FAILURE_TTL_MS);
}
function AccountSummary({ institutions, accounts, transactionImportStatus, optimisticLastSyncedByInstitution, onRename, syncState, autoSyncStates, autoSyncInProgress, activeBatchStates, syncActivity, onSyncClick, onAuthClick, onCredErrorClick, onSettingsClick, expandedState, closingExpandedState = {}, balancesHidden, onToggle, onDetailSelect, selectedDetail, timeframe, customDateRange, balanceHistory, convertAtDate, viewCurrency = NATIVE_VIEW_CURRENCY, primaryCurrency = 'CAD', groupBy = ACCOUNT_GROUP_BY_INSTITUTION, accountListSort = null, timelineStartColumnLabel = 'Initial Balance', timelineEndColumnLabel = 'Current Balance', timelineStartShowDate = false, timelineEndShowDate = false }) {
  const [, setTick] = useState(0);
  const [editingAccountId, setEditingAccountId] = useState(null);
  const isCustomRange = timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY;
  const periodBalanceDate = isCustomRange ? getCustomRangeEndDate(customDateRange) : null;
  const getPeriodBalance = useCallback(
    (account) => getAccountDisplayBalance(account, balanceHistory, timeframe, customDateRange),
    [balanceHistory, customDateRange, timeframe],
  );
  const convertValue = useCallback(
    (amount, fromCurrency, toCurrency, date = null) => convertAtDate(amount, fromCurrency, toCurrency, date),
    [convertAtDate],
  );
  const convertBalanceToPrimary = useCallback(
    (amount, fromCurrency) => convertValue(amount, fromCurrency, primaryCurrency, periodBalanceDate),
    [convertValue, periodBalanceDate, primaryCurrency],
  );
  const convertChangeToPrimary = useCallback(
    (amount, fromCurrency, date) => convertValue(amount, fromCurrency, primaryCurrency, date),
    [convertValue, primaryCurrency],
  );

  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 60000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (editingAccountId === null) return undefined;

    const handlePointerDown = (event) => {
      if (event.target?.closest?.(APP_NON_DISMISS_INTERACTION_SELECTOR)) return;
      const activeEditor = document.querySelector('.accounts-table-shell .accounts-child-identity.is-editing .account-name-row');
      if (activeEditor?.contains(event.target)) return;
      setEditingAccountId(null);
    };

    document.addEventListener('pointerdown', handlePointerDown, true);
    return () => document.removeEventListener('pointerdown', handlePointerDown, true);
  }, [editingAccountId]);

  const toggle = (id) => onToggle(id);
  const institutionsById = useMemo(
    () => new Map(institutions.map((institution) => [institution.id, institution])),
    [institutions]
  );
  const accountsById = useMemo(() => new Map(accounts.map((account) => [account.id, account])), [accounts]);
  // Asset↔liability link chip: a loan shows the asset it secures; an asset shows how many
  // loans it secures (the equity figure lives in the asset's settings). Answers "what loan
  // is for what asset" at a glance.
  const renderLinkChip = (acc) => {
    if (acc.is_liability) {
      const asset = acc.secured_asset_account_id ? accountsById.get(acc.secured_asset_account_id) : null;
      if (!asset) return null;
      return (
        <span className="account-link-chip" data-tooltip={`Secures ${asset.name}`}>
          <MdLink size={12} aria-hidden="true" />
          <span className="account-link-chip-text">{asset.name}</span>
        </span>
      );
    }
    const loanCount = accounts.filter((a) => a.is_liability && a.secured_asset_account_id === acc.id).length;
    if (loanCount === 0) return null;
    return (
      <span className="account-link-chip" data-tooltip="Secured — see equity in account settings">
        <MdLink size={12} aria-hidden="true" />
        <span className="account-link-chip-text">{loanCount} loan{loanCount === 1 ? '' : 's'}</span>
      </span>
    );
  };
  const categoryPanels = useMemo(() => {
    const { groups: groupingGroups, getKey: groupingGetKey } = getGroupingForMode(groupBy);
    const panelByKey = new Map(
      groupingGroups.map((category, index) => [
        category.key,
        { ...category, groupKey: getCategoryGroupKey(category.key), accounts: [], defaultIndex: index },
      ])
    );

    accounts.forEach((account) => {
      const categoryKey = groupingGetKey(account);
      const panel = panelByKey.get(categoryKey) || panelByKey.get('other') || panelByKey.get(groupingGroups[0].key);
      if (panel) panel.accounts.push(account);
    });

    return groupingGroups
      .map((category) => panelByKey.get(category.key))
      .filter((panel) => panel.accounts.length > 0)
      .map((panel) => {
        const changeSummary = getAccountGroupChangeSummary(panel.accounts, balanceHistory, timeframe, customDateRange, convertChangeToPrimary, getPeriodBalance, periodBalanceDate);
        return {
          ...panel,
          label: panel.label,
          netTotal: getAccountGroupNetTotal(panel.accounts, convertBalanceToPrimary, getPeriodBalance),
          startTotal: changeSummary.hasStartData ? changeSummary.start : null,
          changeSummary,
          accounts: [...panel.accounts].sort((a, b) => {
            const balanceSort = sortAccountsForAccountsPanel(a, b, getPeriodBalance);
            if (balanceSort !== 0) return balanceSort;
            const aInstitution = institutionsById.get(a.institution_id)?.name || a.institution || '';
            const bInstitution = institutionsById.get(b.institution_id)?.name || b.institution || '';
            return String(aInstitution).localeCompare(String(bInstitution));
          }),
        };
      })
      .sort((left, right) => {
        const sortResult = compareAccountListSortValues(left, right, accountListSort);
        if (sortResult !== 0) return sortResult;
        return left.defaultIndex - right.defaultIndex;
      });
  }, [accountListSort, accounts, balanceHistory, convertBalanceToPrimary, convertChangeToPrimary, customDateRange, getPeriodBalance, groupBy, institutionsById, periodBalanceDate, timeframe]);

  if (groupBy !== ACCOUNT_GROUP_BY_INSTITUTION) {
    return (
      <div className="institutions accounts-table-rows is-category-grouped">
        {categoryPanels.map((category) => {
          const categoryAccounts = category.accounts;
          const displayCurrency = resolveGroupDisplayCurrency(categoryAccounts, viewCurrency, primaryCurrency);
          const netTotal = getAccountGroupNetTotal(categoryAccounts, (amount, currency) => convertValue(amount, currency, displayCurrency, periodBalanceDate), getPeriodBalance);
          const categoryChange = categoryAccounts.length > 0
            ? getAccountGroupChangeSummary(categoryAccounts, balanceHistory, timeframe, customDateRange, (amount, currency, date) => convertValue(amount, currency, displayCurrency, date), getPeriodBalance, periodBalanceDate)
            : null;
          const categoryHasTimeline = Boolean(categoryChange?.hasData && !categoryChange?.hasFallbackOnlyData);
          const categoryHasStart = Boolean(categoryChange?.hasStartData);
          const categoryHasEnd = Boolean(categoryChange?.hasEndData);
          const isOpen = Boolean(expandedState[category.groupKey]);
          const isVisuallyOpen = isOpen || Boolean(closingExpandedState[category.groupKey]);

          return (
            <div key={category.groupKey} className={`institution-card accounts-table-group accounts-institution-panel accounts-category-panel ${isVisuallyOpen ? 'is-open' : ''}`.trim()}>
              <div className="accounts-table-parent-row" onClick={() => toggle(category.groupKey)}>
                <div className="accounts-table-row-inner accounts-category-section-header">
                  <div className="accounts-group-info accounts-category-info">
                    <span className="accounts-parent-leading-chevron" aria-hidden="true">
                      <TriangleIcon direction={isVisuallyOpen ? 'down' : 'right'} />
                    </span>
                    <div className="accounts-group-title-stack">
                      <span className="accounts-group-title">{category.label}</span>
                      <span className="accounts-group-count">{categoryAccounts.length} {categoryAccounts.length === 1 ? 'Account' : 'Accounts'}</span>
                    </div>
                  </div>
                  <span className="accounts-category-flex-spacer" aria-hidden="true" />
                  <div className="accounts-category-metric accounts-category-start-metric">
                    <TimelineValueCell
                      label={timelineStartColumnLabel}
                      date={categoryHasStart ? categoryChange.startDate : null}
                      value={categoryHasStart ? categoryChange.start : null}
                      currency={displayCurrency}
                      hidden={balancesHidden}
                      showDate={timelineStartShowDate}
                      showLabel={false}
                    />
                  </div>
                  <div className="accounts-category-metric accounts-category-end-metric">
                    <TimelineValueCell
                      label={timelineEndColumnLabel}
                      date={categoryHasEnd ? categoryChange.endDate : periodBalanceDate}
                      value={categoryHasEnd ? categoryChange.end : netTotal}
                      currency={displayCurrency}
                      hidden={balancesHidden}
                      showDate={timelineEndShowDate}
                      showLabel={false}
                    />
                  </div>
                  <div className="accounts-category-metric accounts-category-change-metric">
                    {categoryHasTimeline ? (
                      <span className={`accounts-group-change ${balancesHidden ? 'accounts-value-hidden' : categoryChange.diff === 0 ? 'zero' : categoryChange.diff > 0 ? 'positive' : 'negative'}`}>
                        {balancesHidden ? (
                          <span className="accounts-group-change-main">******</span>
                        ) : (
                          <ChangeValue diff={categoryChange.diff} currency={displayCurrency} />
                        )}
                      </span>
                    ) : <span className="accounts-group-change">—</span>}
                  </div>
                  <div className="accounts-group-actions accounts-category-controls" aria-hidden="true" />
                </div>
              </div>
              {categoryAccounts.length > 0 && (
                <div className={`accounts-table-children ${isOpen ? 'is-open' : ''}`.trim()} aria-hidden={!isOpen} inert={!isOpen}>
                  <div className="accounts-table-children-content">
                    {categoryAccounts.map((acc) => {
                    const accountInstitution = institutionsById.get(acc.institution_id);
                    const institutionName = accountInstitution?.name || acc.institution || 'Unknown';
                    const rowCurrency = viewCurrency === NATIVE_VIEW_CURRENCY
                      ? (String(acc.currency || '').trim().toUpperCase() || primaryCurrency)
                      : viewCurrency;
                    const rowBalance = convertValue(getPeriodBalance(acc), acc.currency, rowCurrency, periodBalanceDate);
                    const rowTimeline = getAccountChange(acc.id, balanceHistory, timeframe, acc.balance, acc.is_liability, customDateRange, acc.is_imported);
                    const rowChangeStart = rowTimeline ? convertValue(acc.is_liability ? -rowTimeline.startBalance : rowTimeline.startBalance, acc.currency, rowCurrency, rowTimeline.startDate) : null;
                    const rowChangeEnd = rowTimeline ? convertValue(acc.is_liability ? -rowTimeline.endBalance : rowTimeline.endBalance, acc.currency, rowCurrency, rowTimeline.endDate) : (acc.is_liability ? -rowBalance : rowBalance);
                    const rowChangeDiff = rowTimeline ? rowChangeEnd - rowChangeStart : null;

                      return (
                        <div
                          key={acc.id}
                          className={`account-row accounts-table-child-row ${acc.is_imported ? 'is-imported' : ''} ${selectedDetail?.kind === 'account' && String(selectedDetail.accountId) === String(acc.id) ? 'is-detail-selected' : ''}`.trim()}
                          onClick={() => onDetailSelect({ kind: 'account', accountId: acc.id })}
                        >
                          <div className="accounts-table-row-inner accounts-category-account-row">
                            <div className={`accounts-child-identity ${editingAccountId === acc.id ? 'is-editing' : ''}`.trim()}>
                              <span className="account-source-pill" title={institutionName}>
                                <InstitutionLogo name={institutionName} provider={accountInstitution?.provider} logoUrl={getInstitutionLogoUrl(accountInstitution)} size="var(--accounts-child-row-institution-logo-size)" boxed />
                              </span>
                              <EditableAccountName
                                accountId={acc.id}
                                name={acc.name}
                                onRename={onRename}
                                isEditing={editingAccountId === acc.id}
                                onEditStart={() => setEditingAccountId(acc.id)}
                                onEditEnd={() => setEditingAccountId((current) => current === acc.id ? null : current)}
                              />
                              <AccountTypeBadge accountType={acc.account_type} />
                              {renderLinkChip(acc)}
                            </div>
                            <span className="account-flex-spacer" aria-hidden="true" />
                            <TimelineValueCell
                              label={timelineStartColumnLabel}
                              date={rowTimeline?.startDate}
                              value={rowTimeline ? rowChangeStart : null}
                              currency={rowCurrency}
                              hidden={balancesHidden}
                              className="account-start"
                              showDate={timelineStartShowDate}
                              showLabel={false}
                            />
                            <TimelineValueCell
                              label={timelineEndColumnLabel}
                              date={rowTimeline?.endDate || periodBalanceDate}
                              value={rowChangeEnd}
                              currency={rowCurrency}
                              hidden={balancesHidden}
                              className="account-end"
                              showDate={timelineEndShowDate}
                              showLabel={false}
                            />
                            {rowTimeline ? (
                              <span className={`accounts-child-change ${balancesHidden ? 'accounts-value-hidden' : rowChangeDiff === 0 ? 'zero' : rowChangeDiff > 0 ? 'positive' : 'negative'}`}>
                                {balancesHidden ? (
                                  <span className="accounts-child-change-main">******</span>
                                ) : (
                                  <ChangeValue diff={rowChangeDiff} currency={rowCurrency} />
                                )}
                              </span>
                            ) : <span className="accounts-child-change">—</span>}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="institutions accounts-table-rows">
      {institutions.filter((inst) => accounts.some((a) => a.institution_id === inst.id)).map((inst) => {
        const instAccounts = accounts.filter((a) => a.institution_id === inst.id);
        const instDisplayCurrency = resolveGroupDisplayCurrency(instAccounts, viewCurrency, primaryCurrency);
        const netTotal = getAccountGroupNetTotal(instAccounts, (amount, currency) => convertValue(amount, currency, instDisplayCurrency, periodBalanceDate), getPeriodBalance);
        const groupKey = getInstitutionGroupKey(inst.id);
        const isOpen = Boolean(expandedState[groupKey]);
        const isVisuallyOpen = isOpen || Boolean(closingExpandedState[groupKey]);
        const state = syncState[connectionStateKey(inst)] || {};
        const autoState = autoSyncStates?.[connectionStateKey(inst)] || null;
        const lastSynced = getInstLastSynced(
          accounts,
          inst.id,
          getMostRecentSyncedAt(
            optimisticLastSyncedByInstitution?.[inst.id] || null,
            autoState?.institutionId === inst.id
              ? getActiveOptimisticSyncedAt(autoState.optimisticLastSyncedAt)
              : null,
          ),
        );
        const ago = timeAgo(lastSynced);
        const syncModel = resolveProviderSyncDisplay({
          institution: inst,
          lastSynced,
          lastSyncText: ago || 'Never',
          manualSyncState: state,
          autoSyncState: autoState,
          autoSyncInProgress,
          batchState: activeBatchStates?.get(Number(inst.id)) || null,
          syncActivity,
          transactionImportStatus,
        });
        const instChange = instAccounts.length > 0
          ? getAccountGroupChangeSummary(instAccounts, balanceHistory, timeframe, customDateRange, (amount, currency, date) => convertValue(amount, currency, instDisplayCurrency, date), getPeriodBalance, periodBalanceDate)
          : null;
        const instHasTimeline = Boolean(instChange?.hasData && !instChange?.hasFallbackOnlyData);
        const instHasStart = Boolean(instChange?.hasStartData);
        const instHasEnd = Boolean(instChange?.hasEndData);
        const handleStatusAction = (event) => {
          event.stopPropagation();
          if (syncModel.actionTarget === 'auth') {
            onAuthClick(inst);
          } else if (syncModel.actionTarget === 'credentials') {
            onCredErrorClick(inst);
          } else {
            onSyncClick(inst);
          }
        };
        const syncStatusCell = syncModel.tone === 'manual'
          ? (
            <span className="inst-sync-manual">
              <span className="inst-sync-manual-dot" aria-hidden="true" />
              <ProviderSyncStatus
                model={syncModel}
                institutionName={inst.name}
                showIcon={false}
                textClassName="inst-sync-manual-text"
              />
            </span>
          )
          : (
            <ProviderSyncStatus
              model={syncModel}
              institutionName={inst.name}
              onAction={syncModel.actionTarget ? handleStatusAction : null}
              iconClassName="inst-sync-btn inst-sync-anchor"
              textClassName="sync-timestamp"
            />
          );

        return (
          <div key={inst.id} className={`institution-card accounts-table-group accounts-institution-panel ${isVisuallyOpen ? 'is-open' : ''} ${selectedDetail?.kind === 'institution' && String(selectedDetail.institutionId) === String(inst.id) ? 'is-detail-selected' : ''}`.trim()}>
            <div className="accounts-table-parent-row" onClick={() => toggle(groupKey)}>
              <div className="accounts-table-row-inner accounts-institution-section-header">
                <div className="accounts-group-info">
                  {instAccounts.length > 0 && (
                    <button
                      type="button"
                      className="accounts-parent-leading-chevron"
                      aria-label={isOpen ? `Collapse ${inst.name}` : `Expand ${inst.name}`}
                      onClick={(event) => { event.stopPropagation(); toggle(groupKey); }}
                    >
                      <TriangleIcon direction={isVisuallyOpen ? 'down' : 'right'} />
                    </button>
                  )}
                  <span className="accounts-group-logo-wrap"><InstitutionLogo name={inst.name} provider={inst.provider} logoUrl={getInstitutionLogoUrl(inst)} size="var(--accounts-parent-row-logo-size)" boxed /></span>
                  <div className="accounts-group-title-stack">
                    <span className="accounts-group-title">{inst.name}</span>
                    {instAccounts.length > 0 && <span className="accounts-group-count">{instAccounts.length} {instAccounts.length === 1 ? 'Account' : 'Accounts'}</span>}
                  </div>
                </div>
                <div className="inst-sync-info">
                  {syncStatusCell}
                </div>
                <span className="accounts-institution-flex-spacer" aria-hidden="true" />
                <div className="accounts-institution-metric accounts-institution-start-metric">
                  <TimelineValueCell
                    label={timelineStartColumnLabel}
                    date={instHasStart ? instChange.startDate : null}
                    value={instHasStart ? instChange.start : null}
                    currency={instDisplayCurrency}
                    hidden={balancesHidden}
                    showDate={timelineStartShowDate}
                    showLabel={false}
                  />
                </div>
                <div className="accounts-institution-metric accounts-institution-end-metric">
                  <TimelineValueCell
                    label={timelineEndColumnLabel}
                    date={instHasEnd ? instChange.endDate : periodBalanceDate}
                    value={instHasEnd ? instChange.end : netTotal}
                    currency={instDisplayCurrency}
                    hidden={balancesHidden}
                    showDate={timelineEndShowDate}
                    showLabel={false}
                  />
                </div>
                <div className="accounts-institution-metric accounts-institution-change-metric">
                  {instHasTimeline ? (
                    <span className={`accounts-group-change ${balancesHidden ? 'accounts-value-hidden' : instChange.diff === 0 ? 'zero' : instChange.diff > 0 ? 'positive' : 'negative'}`}>
                      {balancesHidden ? (
                        <span className="accounts-group-change-main">******</span>
                      ) : (
                        <ChangeValue diff={instChange.diff} currency={instDisplayCurrency} />
                      )}
                    </span>
                  ) : <span className="accounts-group-change">—</span>}
                </div>
                <div className="accounts-group-actions accounts-institution-actions">
                  <MdSettings
                    size={24}
                    className="inst-settings-btn"
                    onClick={(e) => { e.stopPropagation(); onSettingsClick(inst); }}
                    data-tooltip="Institution settings"
                  />
                  <MdInfoOutline
                    size={24}
                    className="inst-detail-btn"
                    onClick={(e) => { e.stopPropagation(); onDetailSelect({ kind: 'institution', institutionId: inst.id }); }}
                    data-tooltip="Institution overview"
                  />
                </div>
              </div>
            </div>
            {instAccounts.length > 0 && (
              <div className={`accounts-table-children ${isOpen ? 'is-open' : ''}`.trim()} aria-hidden={!isOpen} inert={!isOpen}>
                <div className="accounts-table-children-content">
                  {[...instAccounts].sort((a, b) => sortAccountsForAccountsPanel(a, b, getPeriodBalance)).map((acc) => {
                  const rowCurrency = viewCurrency === NATIVE_VIEW_CURRENCY
                    ? (String(acc.currency || '').trim().toUpperCase() || primaryCurrency)
                    : viewCurrency;
                  const rowBalance = convertValue(getPeriodBalance(acc), acc.currency, rowCurrency, periodBalanceDate);
                  const rowTimeline = getAccountChange(acc.id, balanceHistory, timeframe, acc.balance, acc.is_liability, customDateRange, acc.is_imported);
                  const rowChangeStart = rowTimeline ? convertValue(acc.is_liability ? -rowTimeline.startBalance : rowTimeline.startBalance, acc.currency, rowCurrency, rowTimeline.startDate) : null;
                  const rowChangeEnd = rowTimeline ? convertValue(acc.is_liability ? -rowTimeline.endBalance : rowTimeline.endBalance, acc.currency, rowCurrency, rowTimeline.endDate) : (acc.is_liability ? -rowBalance : rowBalance);
                  const rowChangeDiff = rowTimeline ? rowChangeEnd - rowChangeStart : null;
                    return (
                      <div
                        key={acc.id}
                        className={`account-row accounts-table-child-row ${acc.is_imported ? 'is-imported' : ''} ${selectedDetail?.kind === 'account' && String(selectedDetail.accountId) === String(acc.id) ? 'is-detail-selected' : ''}`.trim()}
                        onClick={() => onDetailSelect({ kind: 'account', accountId: acc.id })}
                      >
                        <div className="accounts-table-row-inner accounts-institution-account-row">
                          <div className={`accounts-child-identity ${editingAccountId === acc.id ? 'is-editing' : ''}`.trim()}>
                            <EditableAccountName
                              accountId={acc.id}
                              name={acc.name}
                              onRename={onRename}
                              isEditing={editingAccountId === acc.id}
                              onEditStart={() => setEditingAccountId(acc.id)}
                              onEditEnd={() => setEditingAccountId((current) => current === acc.id ? null : current)}
                            />
                            <AccountTypeBadge accountType={acc.account_type} />
                            {renderLinkChip(acc)}
                          </div>
                          <span className="account-sync-spacer" aria-hidden="true" />
                          <span className="account-flex-spacer" aria-hidden="true" />
                          <TimelineValueCell
                            label={timelineStartColumnLabel}
                            date={rowTimeline?.startDate}
                            value={rowTimeline ? rowChangeStart : null}
                            currency={rowCurrency}
                            hidden={balancesHidden}
                            className="account-start"
                            showDate={timelineStartShowDate}
                            showLabel={false}
                          />
                          <TimelineValueCell
                            label={timelineEndColumnLabel}
                            date={rowTimeline?.endDate || periodBalanceDate}
                            value={rowChangeEnd}
                            currency={rowCurrency}
                            hidden={balancesHidden}
                            className="account-end"
                            showDate={timelineEndShowDate}
                            showLabel={false}
                          />
                          {rowTimeline ? (
                            <span className={`accounts-child-change ${balancesHidden ? 'accounts-value-hidden' : rowChangeDiff === 0 ? 'zero' : rowChangeDiff > 0 ? 'positive' : 'negative'}`}>
                              {balancesHidden ? (
                                <span className="accounts-child-change-main">******</span>
                              ) : (
                                <ChangeValue diff={rowChangeDiff} currency={rowCurrency} />
                              )}
                            </span>
                          ) : <span className="accounts-child-change">—</span>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function formatImportDate(value) {
  const text = String(value || '').trim();
  if (!text) return '—';
  const [year, month, day] = text.slice(0, 10).split('-').map((part) => Number(part));
  if (!year || !month || !day) return text;
  try {
    return new Intl.DateTimeFormat('en-CA', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    }).format(new Date(year, month - 1, day));
  } catch {
    return text;
  }
}

function formatImportRange(start, end) {
  if (!start && !end) return '—';
  if (start && end) return `${formatImportDate(start)} - ${formatImportDate(end)}`;
  return formatImportDate(start || end);
}

function normalizeHistoryStatus(item) {
  return item?.history_status || 'not_started';
}

function getSyncStatusModel(item) {
  const currentFetchStatus = item.current_fetch_status || 'idle';
  const historyStatus = normalizeHistoryStatus(item);

  if (currentFetchStatus === 'running' || currentFetchStatus === 'queued') {
    return {
      icon: currentFetchStatus === 'queued' ? MdHourglassEmpty : MdSync,
      className: currentFetchStatus === 'queued' ? 'is-queued' : 'is-running',
      label: currentFetchStatus === 'queued' ? 'Queued' : 'Fetching transactions',
      spin: currentFetchStatus === 'running',
    };
  }
  if (currentFetchStatus === 'retry_needed' || historyStatus === 'retry_needed') {
    return { icon: MdErrorOutline, className: 'is-partial', label: 'Retry needed' };
  }
  if (historyStatus === 'running' || historyStatus === 'queued') {
    return {
      icon: historyStatus === 'queued' ? MdHourglassEmpty : MdSync,
      className: historyStatus === 'queued' ? 'is-queued' : 'is-running',
      label: historyStatus === 'queued' ? 'Queued' : 'Fetching transactions',
      spin: historyStatus === 'running',
    };
  }
  if (historyStatus === 'complete') {
    return { icon: MdCheck, className: 'is-complete', label: 'Sync complete' };
  }
  return { icon: MdHourglassEmpty, className: 'is-queued', label: 'Not started' };
}

function getImportStatusLabel(item) {
  const status = normalizeHistoryStatus(item);
  if (status === 'complete') return 'Backfill complete';
  if (status === 'running') return 'Importing history';
  if (status === 'retry_needed') return 'Retry needed';
  if (status === 'queued') return 'Queued';
  return 'Not started';
}

function isImportHistoryComplete(item) {
  return normalizeHistoryStatus(item) === 'complete';
}

function formatTransactionHistoryCoverage(item) {
  // Once the backfill window is complete, show the actual transaction date
  // range (data_start_date/data_end_date) so the user sees "May 12, 2025 →
  // today" instead of the full 10-year target window that was attempted but
  // empty. Fall back to the completed-window range, then the target range.
  if (isImportHistoryComplete(item)) {
    if (item.data_start_date || item.data_end_date) {
      return formatImportRange(
        item.data_start_date || item.completed_start_date || item.backfill_start_date,
        item.data_end_date || item.completed_end_date || item.backfill_end_date,
      );
    }
    return formatImportRange(
      item.completed_start_date || item.backfill_start_date,
      item.completed_end_date || item.backfill_end_date,
    );
  }
  return formatImportRange(item.backfill_start_date, item.backfill_end_date);
}

function getActiveTransactionImportConnections(payload) {
  const connectionIds = new Set();
  const institutions = Array.isArray(payload?.institutions) ? payload.institutions : [];
  institutions.forEach((institution) => {
    const institutionId = Number(institution?.institution_id || 0);
    if (!institutionId) return;
    const currentFetchStatus = String(institution?.current_fetch_status || '').trim().toLowerCase();
    const jobStatus = String(institution?.transaction_import_job_status || '').trim().toLowerCase();
    if (
      currentFetchStatus === 'running'
      || currentFetchStatus === 'queued'
      || ACTIVE_TRANSACTION_IMPORT_JOB_STATUSES.has(jobStatus)
    ) {
      connectionIds.add(institutionId);
    }
  });
  return connectionIds;
}

function hasActiveTransactionImportJob(result) {
  const status = String(result?.transaction_import_job?.status || '').trim().toLowerCase();
  return ACTIVE_TRANSACTION_IMPORT_JOB_STATUSES.has(status);
}

function getEarliestImportDate(values) {
  const dates = values.filter(Boolean).map((value) => String(value).slice(0, 10));
  if (!dates.length) return null;
  return dates.sort()[0];
}

function getLatestImportDate(values) {
  const dates = values.filter(Boolean).map((value) => String(value).slice(0, 10));
  if (!dates.length) return null;
  return dates.sort()[dates.length - 1];
}

function sumImportWindows(accounts, field) {
  return accounts.reduce((total, account) => total + Number(account?.[field] || 0), 0);
}

function aggregateHistoryStatus(accounts, institution) {
  if (institution?.history_status) {
    return institution.history_status;
  }
  const statuses = accounts.map(normalizeHistoryStatus);
  if (!statuses.length) return 'not_started';
  if (statuses.some((status) => status === 'retry_needed')) return 'retry_needed';
  if (statuses.some((status) => status === 'running')) return 'running';
  if (statuses.every((status) => status === 'queued')) return 'queued';
  if (statuses.some((status) => status === 'queued' || status === 'not_started')) return 'retry_needed';
  return 'complete';
}

function aggregateCurrentFetchStatus(accounts, institution) {
  if (institution?.current_fetch_status && institution.current_fetch_status !== 'idle') {
    return {
      current_fetch_status: institution.current_fetch_status,
      current_fetch_label: institution.current_fetch_label,
      current_fetch_mode: institution.current_fetch_mode,
    };
  }
  const statuses = accounts.map((account) => account.current_fetch_status || 'idle');
  const modes = accounts.map((account) => account.current_fetch_mode).filter(Boolean);
  const mode = modes.includes('backfill') ? 'backfill' : modes[0] || null;
  const status = statuses.includes('running')
    ? 'running'
    : statuses.includes('queued')
      ? 'queued'
      : statuses.includes('retry_needed')
        ? 'retry_needed'
        : 'idle';
  return {
    current_fetch_status: status,
    current_fetch_mode: status === 'idle' ? null : mode,
    current_fetch_label: null,
  };
}

function getTransactionImportInstitutionSummary(institution) {
  const accounts = Array.isArray(institution?.accounts) ? institution.accounts : [];
  const historyStatus = aggregateHistoryStatus(accounts, institution);
  const currentFetch = aggregateCurrentFetchStatus(accounts, institution);
  const completedAccounts = accounts.reduce(
    (total, account) => (normalizeHistoryStatus(account) === 'complete' ? total + 1 : total),
    0,
  );
  return {
    status: institution?.status || 'not_started',
    status_label: institution?.status_label || 'Not started',
    history_status: historyStatus,
    history_status_label: institution?.history_status_label || getImportStatusLabel({ history_status: historyStatus }),
    ...currentFetch,
    completed_start_date: getEarliestImportDate(accounts.map((account) => account.completed_start_date)),
    completed_end_date: getLatestImportDate(accounts.map((account) => account.completed_end_date)),
    data_start_date: getEarliestImportDate(accounts.map((account) => account.data_start_date)),
    data_end_date: getLatestImportDate(accounts.map((account) => account.data_end_date)),
    backfill_start_date: getEarliestImportDate(accounts.map((account) => account.backfill_start_date)),
    backfill_end_date: getLatestImportDate(accounts.map((account) => account.backfill_end_date)),
    completed_windows: sumImportWindows(accounts, 'completed_windows'),
    total_windows: sumImportWindows(accounts, 'total_windows'),
    pending_windows: sumImportWindows(accounts, 'pending_windows'),
    failed_windows: sumImportWindows(accounts, 'failed_windows'),
    total_accounts: accounts.length,
    completed_accounts: completedAccounts,
    latest_incremental_start_date: getEarliestImportDate(accounts.map((account) => account.latest_incremental_start_date)),
    latest_incremental_end_date: getLatestImportDate(accounts.map((account) => account.latest_incremental_end_date)),
  };
}

function TransactionImportAddedLine({ addedAt }) {
  const addedLabel = formatAccountsDateLabel(addedAt);
  if (!addedLabel) return null;
  return (
    <span className="transaction-import-coverage-line transaction-import-added-line">
      <span className="transaction-import-coverage-label">Added:</span>
      <span className="transaction-import-coverage-value">{addedLabel}</span>
    </span>
  );
}

function TransactionImportProgressCells({ item, addedAt = null }) {
  const addedLine = addedAt ? <TransactionImportAddedLine addedAt={addedAt} /> : null;
  if (!item) {
    return (
      <>
        <span className="transaction-import-status is-unavailable" role="cell">
          <span className="transaction-import-status-main">
            <span>Not applicable</span>
          </span>
        </span>
        <span className="transaction-import-coverage is-unavailable" role="cell">
          {addedLine || <span>Not applicable</span>}
        </span>
      </>
    );
  }

  const syncStatusModel = getSyncStatusModel(item);
  const SyncStatusIcon = syncStatusModel.icon;
  const statusDetail = getImportStatusDetail(item);
  const historyLabel = isImportHistoryComplete(item) ? 'Imported' : 'Target';

  return (
    <>
      <span className={`transaction-import-status ${syncStatusModel.className}`} role="cell">
        <span className="transaction-import-status-main">
          <SyncStatusIcon size={16} className={syncStatusModel.spin ? 'spin-icon' : ''} />
          <span>{syncStatusModel.label}</span>
        </span>
        {statusDetail ? (
          <span className="transaction-import-status-detail">{statusDetail}</span>
        ) : null}
      </span>
      <span className="transaction-import-coverage" role="cell">
        {addedLine}
        <span className="transaction-import-coverage-line">
          <span className="transaction-import-coverage-label">{historyLabel}:</span>
          <span className="transaction-import-coverage-value">{formatTransactionHistoryCoverage(item)}</span>
        </span>
        <span className="transaction-import-coverage-line">
          <span className="transaction-import-coverage-label">Recent import:</span>
          <span className="transaction-import-coverage-value">
            {formatImportRange(item.latest_incremental_start_date, item.latest_incremental_end_date)}
          </span>
        </span>
      </span>
    </>
  );
}

function AccountImportStatusIcon({ item }) {
  if (!item) return null;

  const syncStatusModel = getSyncStatusModel(item);
  if (syncStatusModel.className === 'is-complete') return null;
  const SyncStatusIcon = syncStatusModel.icon;
  return (
    <span
      className={`accounts-detail-account-status-icon transaction-import-status ${syncStatusModel.className}`.trim()}
      aria-label={syncStatusModel.label}
    >
      <SyncStatusIcon size={16} className={syncStatusModel.spin ? 'spin-icon' : ''} />
    </span>
  );
}

function findTransactionImportAccount(importInstitution, account) {
  const accounts = Array.isArray(importInstitution?.accounts) ? importInstitution.accounts : [];
  if (!account) return null;
  return accounts.find((item) => {
    const sameId = item.account_id !== undefined
      && item.account_id !== null
      && String(item.account_id) === String(account.id);
    const sameExternalId = item.account_external_id
      && account.external_id
      && String(item.account_external_id) === String(account.external_id);
    const sameName = item.account_name && account.name && String(item.account_name) === String(account.name);
    return sameId || sameExternalId || sameName;
  }) || null;
}

function TransactionImportSummaryBlock({ item, addedAt = null }) {
  return (
    <div className="accounts-detail-import-row accounts-detail-import-summary-row">
      <TransactionImportProgressCells item={item} addedAt={addedAt} />
    </div>
  );
}

function buildAccountSparklinePoints(account, history, convertAtDate, currency, timelineRange) {
  const entries = Object.entries(history || {})
    .filter(([date, value]) => date && Number.isFinite(Number(value)))
    .filter(([date]) => {
      const dateKey = formatAccountsDateKey(date);
      if (timelineRange.start && dateKey < timelineRange.start) return false;
      if (timelineRange.end && dateKey > timelineRange.end) return false;
      return true;
    })
    .sort(([left], [right]) => String(left).localeCompare(String(right)));

  return entries.map(([date, value]) => {
    const signedValue = account.is_liability ? -Number(value) : Number(value);
    return {
      date,
      value: convertAtDate(signedValue, account.currency, currency, date),
    };
  });
}

function buildInstitutionSparklinePoints(accounts, balanceHistory, convertAtDate, currency, timelineRange) {
  const datedValuesByAccountId = new Map();
  const allDates = new Set();
  (accounts || []).forEach((account) => {
    const entries = Object.entries(balanceHistory?.[account.id] || {})
      .filter(([date, value]) => date && Number.isFinite(Number(value)))
      .sort(([left], [right]) => String(left).localeCompare(String(right)));
    if (entries.length === 0) return;
    const accountValues = new Map();
    entries.forEach(([date, value]) => {
      const dateKey = formatAccountsDateKey(date);
      accountValues.set(dateKey, Number(value));
      allDates.add(dateKey);
    });
    datedValuesByAccountId.set(account.id, accountValues);
  });

  const latestValues = new Map();
  return Array.from(allDates)
    .sort((left, right) => String(left).localeCompare(String(right)))
    .reduce((points, date) => {
      (accounts || []).forEach((account) => {
        const accountValues = datedValuesByAccountId.get(account.id);
        if (accountValues?.has(date)) {
          latestValues.set(account.id, accountValues.get(date));
        }
      });
      if (timelineRange.start && date < timelineRange.start) return points;
      if (timelineRange.end && date > timelineRange.end) return points;
      let total = 0;
      let hasValue = false;
      (accounts || []).forEach((account) => {
        if (!latestValues.has(account.id)) return;
        const rawValue = latestValues.get(account.id);
        const signedValue = account.is_liability ? -rawValue : rawValue;
        total += convertAtDate(signedValue, account.currency, currency, date);
        hasValue = true;
      });
      if (hasValue) points.push({ date, value: total });
      return points;
    }, []);
}

function resolveSignedBalanceHistoryColor(currentValue, colors, chartColors) {
  const value = Number(currentValue);
  if (Number.isFinite(value)) {
    if (value > 0) return colors.positive || chartColors.netWorth;
    if (value < 0) return colors.negative || chartColors.netWorth;
  }
  return chartColors.netWorth;
}

function BalanceHistorySparkline({ points, currency, balancesHidden, currentValue }) {
  const chartRef = useRef(null);
  const { colors, chartColors } = useTheme();
  const color = resolveSignedBalanceHistoryColor(currentValue, colors, chartColors);

  const option = useMemo(() => {
    return {
      grid: { top: 8, right: 8, bottom: 8, left: 8 },
      tooltip: {
        trigger: 'axis',
        confine: false,
        backgroundColor: 'transparent',
        borderWidth: 0,
        padding: 0,
        extraCssText: 'box-shadow:none; z-index:9999;',
        axisPointer: { type: 'line', lineStyle: { color: chartColors.grid, type: 'dashed' } },
        position: (pt, _params, dom, _rect, size) => {
          let x = pt[0];
          let y = pt[1];
          const chart = chartRef.current;
          if (chart) {
            const data = chart.convertFromPixel({ seriesIndex: 0 }, pt);
            if (data && Number.isFinite(data[0])) {
              const di = Math.max(0, Math.min(points.length - 1, Math.round(data[0])));
              if (points[di]) {
                const sx = chart.convertToPixel({ xAxisIndex: 0 }, di);
                const sy = chart.convertToPixel({ yAxisIndex: 0 }, points[di].value);
                if (Number.isFinite(sx)) x = sx;
                if (Number.isFinite(sy)) y = sy;
              }
            }
          }
          const chartDom = chart?.getDom?.();
          const chartBox = chartDom?.getBoundingClientRect?.();
          const contentWidth = Number(size?.contentSize?.[0]) || dom?.offsetWidth || 0;
          let left = contentWidth ? x - (contentWidth / 2) : x;
          if (contentWidth && chartBox) {
            const clampBox = chartDom.closest?.('.app-detail-drawer-body')?.getBoundingClientRect?.();
            const clampLeft = (clampBox?.left ?? 0) + 8 - chartBox.left;
            const clampRight = (clampBox?.right ?? window.innerWidth) - 8 - contentWidth - chartBox.left;
            if (Number.isFinite(clampLeft) && Number.isFinite(clampRight) && clampRight >= clampLeft) {
              left = Math.max(clampLeft, Math.min(left, clampRight));
            }
            const arrowPadding = Math.min(8, contentWidth / 2);
            const arrowLeft = Math.max(arrowPadding, Math.min(contentWidth - arrowPadding, x - left));
            if (dom?.style && Number.isFinite(arrowLeft)) {
              dom.style.setProperty('--accounts-detail-tooltip-arrow-left', `${arrowLeft}px`);
            }
          }
          return [left, y + 24];
        },
        formatter: (params) => {
          const index = Array.isArray(params) ? params[0]?.dataIndex : params?.dataIndex;
          const point = points[index];
          if (!point) return '';
          const value = balancesHidden ? '******' : moneyText(point.value, currency);
          return '<div class="tooltip-card networth-chart-tooltip accounts-detail-chart-tooltip">'
            + `<p class="tooltip-date">${escapeTooltipHtml(formatAccountsDateLabel(point.date))}</p>`
            + '<p class="networth-tooltip-figure">'
            + `<span class="networth-tooltip-dot" style="background-color:${escapeTooltipHtml(color)}"></span>`
            + `<span>${escapeTooltipHtml(value)}</span></p>`
            + '</div>';
        },
      },
      xAxis: {
        type: 'category',
        data: points.map((point) => point.date),
        boundaryGap: false,
        axisLabel: { show: false, ...breaktwentyChartText.denseTick },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        axisLabel: { show: false, ...breaktwentyChartText.denseTick },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      series: [{
        type: 'line',
        data: points.map((point) => point.value),
        smooth: false,
        showSymbol: false,
        symbolSize: 10,
        lineStyle: { color, width: 2.5 },
        itemStyle: { color, borderColor: chartColors.highlightStroke, borderWidth: chartColors.highlightStrokeWidth },
        areaStyle: { color, opacity: 0.12 },
      }],
    };
  }, [balancesHidden, chartColors, color, currency, points]);

  if (points.length < 2) {
    return (
      <div className="accounts-detail-empty">
        Not enough balance history yet.
      </div>
    );
  }

  return (
    <div className="accounts-detail-chart">
      <EChart option={option} height={120} onChartReady={(chart) => { chartRef.current = chart; }} />
    </div>
  );
}

function AccountBalanceSparkline({ account, history, convertAtDate, currency, balancesHidden, timeframe, customDateRange }) {
  const timelineRange = useMemo(
    () => getAccountsTimelineRange(timeframe, customDateRange),
    [customDateRange, timeframe],
  );
  const points = useMemo(
    () => buildAccountSparklinePoints(account, history, convertAtDate, currency, timelineRange),
    [account, convertAtDate, currency, history, timelineRange],
  );
  const currentValue = useMemo(() => {
    const signedValue = account.is_liability ? -(Number(account.balance) || 0) : Number(account.balance) || 0;
    return convertAtDate(signedValue, account.currency, currency);
  }, [account, convertAtDate, currency]);
  return (
    <BalanceHistorySparkline
      points={points}
      currency={currency}
      balancesHidden={balancesHidden}
      currentValue={currentValue}
    />
  );
}

function InstitutionBalanceSparkline({ accounts, balanceHistory, convertAtDate, currency, balancesHidden, timeframe, customDateRange }) {
  const timelineRange = useMemo(
    () => getAccountsTimelineRange(timeframe, customDateRange),
    [customDateRange, timeframe],
  );
  const points = useMemo(
    () => buildInstitutionSparklinePoints(accounts, balanceHistory, convertAtDate, currency, timelineRange),
    [accounts, balanceHistory, convertAtDate, currency, timelineRange],
  );
  const currentValue = useMemo(
    () => getAccountGroupNetTotal(accounts, (amount, fromCurrency) => convertAtDate(amount, fromCurrency, currency)),
    [accounts, convertAtDate, currency],
  );
  return (
    <BalanceHistorySparkline
      points={points}
      currency={currency}
      balancesHidden={balancesHidden}
      currentValue={currentValue}
    />
  );
}

function groupAccountsDetailTransactionsByDate(transactions) {
  const groups = new Map();
  (transactions || []).forEach((transaction) => {
    const key = String(transaction?.date || '').slice(0, 10);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(transaction);
  });
  return Array.from(groups.entries())
    .sort((left, right) => (left[0] < right[0] ? 1 : left[0] > right[0] ? -1 : 0))
    .map(([key, items]) => ({
      key: key || 'undated',
      label: formatLongDateValue(key, '—'),
      items,
    }));
}

function AccountsDetailRecentTransactions({
  transactions,
  loading,
  balancesHidden,
}) {
  if (loading) {
    return <div className="accounts-detail-empty">Loading…</div>;
  }

  if (!transactions || transactions.length === 0) {
    return <div className="accounts-detail-empty">No transactions.</div>;
  }

  return (
    <>
      <div className="accounts-detail-transactions-list">
        {groupAccountsDetailTransactionsByDate(transactions).map((group) => (
          <div className="accounts-detail-transactions-group" key={group.key}>
            <div className="accounts-detail-transactions-group-label">{group.label}</div>
            <div className="accounts-detail-transactions-group-rows">
              {group.items.map((transaction) => {
                const amount = Number(transaction.amount || 0);
                const transactionCurrency = String(transaction.currency || '').trim().toUpperCase() || 'CAD';
                const title = getTransactionPrimaryDescription(transaction);
                const fullAmount = `${moneyText(amount, transactionCurrency, { alwaysSign: true })} ${transactionCurrency}`;
                const compactAmount = `${moneyText(amount, transactionCurrency, { compact: true, alwaysSign: true })} ${transactionCurrency}`;
                return (
                  <div className="accounts-detail-tx" key={transaction.id}>
                    {transaction.category ? <CategoryPill category={transaction.category} size="sm" /> : null}
                    <div className="accounts-detail-tx-main">
                      <span className="accounts-detail-tx-desc" data-tooltip={title}>{title}</span>
                    </div>
                    <span className={`accounts-detail-tx-amount ${amount > 0 ? 'is-income' : amount < 0 ? 'is-spending' : ''}`.trim()}>
                      <FitMoney
                        className="accounts-detail-tx-money"
                        full={balancesHidden ? '******' : fullAmount}
                        compact={balancesHidden ? '******' : compactAmount}
                      />
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

function AccountsDetailTray({
  selection,
  animatedOpen = false,
  accounts,
  institutions,
  transactionImportStatus,
  optimisticLastSyncedByInstitution,
  syncState = {},
  autoSyncStates = {},
  autoSyncInProgress = false,
  activeBatchStates = null,
  syncActivity = null,
  balanceHistory,
  convertAtDate,
  primaryCurrency,
  balancesHidden,
  timeframe,
  customDateRange,
  onClose,
  onDetailSelect,
  dismissLocked = false,
}) {
  const navigate = useNavigate();
  const shellRef = useRef(null);
  const [recentTransactions, setRecentTransactions] = useState([]);
  const [recentTransactionsLoading, setRecentTransactionsLoading] = useState(false);
  const [visibleSelection, setVisibleSelection] = useState(selection || null);
  const [, setClockTick] = useState(0);
  const isOpen = Boolean(selection);
  const handleClose = useCallback((event) => {
    if (dismissLocked) {
      event?.preventDefault?.();
      return;
    }
    onClose();
  }, [dismissLocked, onClose]);

  useEffect(() => {
    let cancelled = false;
    if (selection) {
      Promise.resolve().then(() => {
        if (!cancelled) {
          setVisibleSelection(selection);
        }
      });
      return () => {
        cancelled = true;
      };
    }
    const timeoutId = setTimeout(() => {
      setVisibleSelection(null);
    }, ACCOUNTS_DETAIL_TRAY_TRANSITION_MS);
    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, [selection]);

  useEffect(() => {
    if (!isOpen) return undefined;
    const id = setInterval(() => setClockTick((tick) => tick + 1), 60000);
    return () => clearInterval(id);
  }, [isOpen]);

  const selectedAccount = visibleSelection?.kind === 'account'
    ? accounts.find((account) => String(account.id) === String(visibleSelection.accountId))
    : null;
  const selectedAccountId = selectedAccount?.id;
  const institution = visibleSelection?.kind === 'account'
    ? institutions.find((item) => String(item.id) === String(selectedAccount?.institution_id))
    : visibleSelection?.kind === 'institution'
      ? institutions.find((item) => String(item.id) === String(visibleSelection.institutionId))
      : null;

  useDismissibleLayer({
    open: isOpen,
    ref: shellRef,
    ignoreSelector: '.accounts-institution-section-header, .accounts-institution-account-row, .accounts-category-account-row, .dashboard-timeline-popover, .dashboard-timeline-panel, .timeline-range-picker-popover, .timeline-range-picker, .timeline-selector-trigger',
    ignoreAppChrome: true,
    onDismiss: handleClose,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

  useEffect(() => {
    let cancelled = false;
    if (!selectedAccountId) {
      Promise.resolve().then(() => {
        if (!cancelled) {
          setRecentTransactions([]);
          setRecentTransactionsLoading(false);
        }
      });
      return () => {
        cancelled = true;
      };
    }

    Promise.resolve().then(() => {
      if (cancelled) return;
      setRecentTransactionsLoading(true);
      fetch(buildAccountsDetailRecentTransactionsUrl(selectedAccountId))
        .then((resp) => readTransactionCollectionResponse(resp, {
          label: 'Recent transactions',
        }))
        .then((payload) => {
          if (cancelled) return;
          setRecentTransactions(payload.transactions);
          setRecentTransactionsLoading(false);
        })
        .catch(() => {
          if (cancelled) return;
          setRecentTransactions([]);
          setRecentTransactionsLoading(false);
        });
    });
    return () => { cancelled = true; };
  }, [selectedAccountId]);

  if (!visibleSelection) return null;
  if (!institution || (visibleSelection.kind === 'account' && !selectedAccount)) return null;

  const portalTarget = typeof document !== 'undefined' ? document.querySelector('.app-main') : null;
  if (!portalTarget) return null;

  const institutionAccounts = accounts.filter((account) => String(account.institution_id) === String(institution.id));
  const importInstitution = findTransactionImportInstitution(transactionImportStatus, institution);
  const importSummary = importInstitution ? getTransactionImportInstitutionSummary(importInstitution) : null;
  const importAccount = selectedAccount ? findTransactionImportAccount(importInstitution, selectedAccount) : null;
  const isCustomRange = timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY;
  const periodBalanceDate = isCustomRange ? getCustomRangeEndDate(customDateRange) : null;
  const getPeriodBalance = (account) => getAccountDisplayBalance(account, balanceHistory, timeframe, customDateRange);
  const convertValue = (amount, fromCurrency, toCurrency, date = null) => convertAtDate(amount, fromCurrency, toCurrency, date);

  const titleSyncModel = (() => {
    const key = connectionStateKey(institution);
    const state = syncState?.[key] || {};
    const autoState = autoSyncStates?.[key] || null;
    const optimisticLastSynced = getMostRecentSyncedAt(
      optimisticLastSyncedByInstitution?.[institution.id] || null,
      String(autoState?.institutionId) === String(institution.id)
        ? getActiveOptimisticSyncedAt(autoState.optimisticLastSyncedAt)
        : null,
    );
    const lastSynced = selectedAccount
      ? selectedAccount.last_synced
      : getInstLastSynced(accounts, institution.id, optimisticLastSynced);
    const ago = timeAgo(lastSynced);
    return resolveProviderSyncDisplay({
      institution,
      lastSynced,
      lastSyncText: ago || 'Never',
      manualSyncState: state,
      autoSyncState: autoState,
      autoSyncInProgress,
      batchState: activeBatchStates?.get(Number(institution.id)) || null,
      syncActivity,
      transactionImportStatus,
    });
  })();
  const titleText = selectedAccount ? selectedAccount.name : institution.name;
  const showTitleSyncStatus = shouldShowAccountsDetailSync(institution.provider, selectedAccount);
  const title = (
    <span className="accounts-detail-title-main">
      <span className="accounts-detail-title-logo" aria-hidden="true">
        <InstitutionLogo
          name={institution.name}
          provider={institution.provider}
          logoUrl={getInstitutionLogoUrl(institution)}
          size={30}
        />
      </span>
      <span className="accounts-detail-title-copy">
        <span className="accounts-detail-title-text">{titleText}</span>
        {showTitleSyncStatus ? (
          <ProviderSyncStatus
            model={titleSyncModel}
            institutionName={institution.name}
            showIcon={false}
            showTooltip={false}
            displayLabel={titleSyncModel.tooltipRows[0] || 'Last sync: Never'}
            textClassName="accounts-detail-title-sync-status sync-timestamp"
          />
        ) : null}
      </span>
    </span>
  );
  const backAction = selectedAccount ? (
    <button
      type="button"
      className="add-networth-back accounts-detail-back"
      aria-label={`Back to ${institution.name}`}
      onClick={() => onDetailSelect({ kind: 'institution', institutionId: institution.id })}
    >
      <MdArrowBack size={20} />
    </button>
  ) : null;
  const displayCurrency = selectedAccount
    ? (String(selectedAccount.currency || '').trim().toUpperCase() || primaryCurrency)
    : resolveGroupDisplayCurrency(institutionAccounts, NATIVE_VIEW_CURRENCY, primaryCurrency);
  const timelineDisplayLabel = getAccountsTimelineDisplayLabel(timeframe, customDateRange);
  const handleViewAllTransactions = () => {
    if (!selectedAccount) return;
    navigate('/transactions', {
      state: {
        cashFlowFilter: { accountIds: [selectedAccount.id] },
      },
    });
    handleClose();
  };

  return createPortal((
    <div
      className={`app-edge-tray is-pinned accounts-detail-tray ${selectedAccount ? 'is-account-detail' : 'is-institution-detail'} ${animatedOpen ? 'is-open' : ''}`.trim()}
      role="dialog"
      aria-label="Account details"
      aria-hidden={!animatedOpen}
    >
      <div className="app-edge-tray-backdrop" aria-hidden="true" />
      <div className="app-edge-tray-shell" ref={shellRef}>
        <SideDetailDrawerPanel
          title={title}
          leadingAction={backAction}
          onClose={handleClose}
          className={`panel-shell accounts-detail-panel ${selectedAccount ? 'is-account-detail' : 'is-institution-detail'}`}
        >
          <div className="accounts-detail-content">
            <section className="accounts-detail-section">
              <div className="accounts-detail-section-heading">
                <h4 className="accounts-detail-section-title">Balance History</h4>
                <span className="accounts-detail-section-meta">{timelineDisplayLabel}</span>
              </div>
              {selectedAccount ? (
                <AccountBalanceSparkline
                  account={selectedAccount}
                  history={balanceHistory[selectedAccount.id]}
                  convertAtDate={convertAtDate}
                  currency={displayCurrency}
                  balancesHidden={balancesHidden}
                  timeframe={timeframe}
                  customDateRange={customDateRange}
                />
              ) : (
                <InstitutionBalanceSparkline
                  accounts={institutionAccounts}
                  balanceHistory={balanceHistory}
                  convertAtDate={convertAtDate}
                  currency={displayCurrency}
                  balancesHidden={balancesHidden}
                  timeframe={timeframe}
                  customDateRange={customDateRange}
                />
              )}
            </section>

            <section className="accounts-detail-section">
              <h4 className="accounts-detail-section-title">Transaction Status</h4>
              {selectedAccount ? (
                <TransactionImportSummaryBlock item={importAccount} addedAt={selectedAccount.added_at} />
              ) : (
                <TransactionImportSummaryBlock item={importSummary} addedAt={institution.added_at} />
              )}
            </section>

            {!selectedAccount ? (
              <section className="accounts-detail-section">
                <h4 className="accounts-detail-section-title">Accounts</h4>
                <div className="accounts-detail-account-list">
                  {[...institutionAccounts].sort((a, b) => sortAccountsForAccountsPanel(a, b, getPeriodBalance)).map((account) => {
                    const accountCurrency = String(account.currency || '').trim().toUpperCase() || primaryCurrency;
                    const accountBalance = convertValue(getPeriodBalance(account), account.currency, accountCurrency, periodBalanceDate);
                    const accountImportStatus = findTransactionImportAccount(importInstitution, account);
                    const isAccountSyncManaged = shouldShowAccountsDetailSync(institution.provider, account);
                    return (
                      <div key={account.id} className="accounts-detail-account-item">
                        <button
                          type="button"
                          className="accounts-detail-account-row"
                          onClick={() => onDetailSelect({ kind: 'account', accountId: account.id })}
                        >
                          <span className="accounts-detail-account-copy">
                            {isAccountSyncManaged ? <AccountImportStatusIcon item={accountImportStatus} /> : null}
                            <span className="accounts-detail-account-name">{account.name}</span>
                            <AccountTypeBadge accountType={account.account_type} />
                          </span>
                          <AccountBalanceCell value={accountBalance} currency={accountCurrency} isLiability={account.is_liability} hidden={balancesHidden} />
                        </button>
                      </div>
                    );
                  })}
                </div>
              </section>
            ) : null}

            {selectedAccount ? (
              <section className="accounts-detail-section">
                <div className="accounts-detail-section-heading">
                  <h4 className="accounts-detail-section-title">Last 5 Transactions</h4>
                  <button
                    type="button"
                    className="accounts-detail-transactions-viewall button-shell-opt-out"
                    onClick={handleViewAllTransactions}
                  >
                    View all →
                  </button>
                </div>
                <AccountsDetailRecentTransactions
                  transactions={recentTransactions}
                  loading={recentTransactionsLoading}
                  balancesHidden={balancesHidden}
                />
              </section>
            ) : null}
          </div>
        </SideDetailDrawerPanel>
      </div>
    </div>
  ), portalTarget);
}

function Accounts({
  data,
  allScopeInstitutions = [],
  fetchAllScopeInstitutions,
  userTimezone = 'UTC',
  userTimezoneConfigured = true,
  onDataChange,
  autoSyncStates,
  setAutoSyncStates,
  autoSyncInProgress = false,
  activeSyncBatches = [],
  syncActivity = { active: [] },
  onOptimisticSyncActivitiesChange,
  setSyncAllBlocked,
  syncNetworkNotice = null,
  onSyncNetworkNoticeChange,
  timeframe = DEFAULT_PORTFOLIO_TIMEFRAME,
  customDateRange = { start: '', end: '' },
}) {
  const OPTIMISTIC_SYNC_TTL_MS = 2 * 60 * 1000;
  const OPTIMISTIC_SYNC_RECONCILIATION_WINDOW_MS = 5 * 60 * 1000;
  const [balancesHidden] = useBalancesHidden();
  const [detailSelection, setDetailSelection] = useState(null);
  const accountsDetailTray = useRightTrayOpenState('accounts-detail', Boolean(detailSelection));
  const [settingsModal, setSettingsModal] = useState(null);
  // Core data is owned by App-level state; this page renders straight from the
  // `data` prop (single source of truth) and refreshes only via onDataChange.
  const { accounts = [], institutions: dataInstitutions = [] } = data;
  const [institutionLogoOverrides, setInstitutionLogoOverrides] = useState({});
  const institutions = useMemo(() => dataInstitutions.map((inst) => {
    const override = institutionLogoOverrides[String(inst.id)];
    return override ? { ...inst, has_logo: override.hasLogo, logo_version: override.version } : inst;
  }), [dataInstitutions, institutionLogoOverrides]);
  const handleInstitutionLogoChange = useCallback((institutionId, hasLogo, version = Date.now()) => {
    setInstitutionLogoOverrides((previous) => ({
      ...previous,
      [String(institutionId)]: { hasLogo, version },
    }));
    setSettingsModal((previous) => (
      previous && String(previous.id) === String(institutionId)
        ? { ...previous, has_logo: hasLogo, logo_version: version }
        : previous
    ));
  }, []);
  const handleDataChange = useCallback(async () => {
    await Promise.all([
      onDataChange ? onDataChange() : Promise.resolve(),
      fetchAllScopeInstitutions ? fetchAllScopeInstitutions() : Promise.resolve(),
    ]);
  }, [fetchAllScopeInstitutions, onDataChange]);
  const transactionImportStatus = data.transactionImportStatus || EMPTY_TRANSACTION_IMPORT_STATUS;
  const [balanceHistory, setBalanceHistory] = useState({});
  const [fxHistory, setFxHistory] = useState({});
  const { primaryCurrency, fxRates } = useCurrency();
  const convert = useMemo(() => makeCurrencyConverter(primaryCurrency, fxRates), [primaryCurrency, fxRates]);
  const fxHistoryIndex = useMemo(() => buildFxHistoryIndex(fxHistory), [fxHistory]);
  const convertAtDate = useMemo(
    () => makeHistoricalCurrencyConverter(primaryCurrency, fxRates, fxHistoryIndex),
    [fxHistoryIndex, fxRates, primaryCurrency],
  );
  // Option B: account rows + uniform-institution subtotals stay native; mixed
  // groups and global totals fall back to the primary (the picked currency).
  const effectiveViewCurrency = NATIVE_VIEW_CURRENCY;
  const [optimisticLastSyncedByInstitution, setOptimisticLastSyncedByInstitution] = useState({});
  const optimisticSyncTimeoutsRef = useRef({});
  const providerSyncSuccessTimeoutsRef = useRef({});
  const [syncState, setSyncStateRaw] = useState({});
  const [authModal, setAuthModal] = useState(null);
  const [authSkipAutoLogin, setAuthSkipAutoLogin] = useState(false);
  const [apiCredModal, setApiCredModal] = useState(null);
  const [showVisDropdown, setShowVisDropdown] = useState(false);
  const [scopeSaveError, setScopeSaveError] = useState('');
  const [scopeSaving, setScopeSaving] = useState(false);
  const [expandedState, setExpandedState] = useState({});
  const [closingExpandedState, setClosingExpandedState] = useState({});
  const closingExpandedTimeoutsRef = useRef({});
  const [accountGroupBy, setAccountGroupBy] = useState(loadAccountGroupByPreference);
  const [accountListSort, setAccountListSort] = usePersistentSortConfig(
    ACCOUNT_LIST_SORT_STORAGE_KEY,
    null,
    ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS
  );
  // Transient draft for the scope popover's optimistic toggles; null when the
  // popover is closed (then the central allScopeInstitutions prop is read).
  const [scopeDraft, setScopeDraft] = useState(null);
  // Account-specific actions share the top toolbar with the page-wide filters.
  const [accountsToolbarGroupSlot, setAccountsToolbarGroupSlot] = useState(null);
  const [accountsToolbarScopeSlot, setAccountsToolbarScopeSlot] = useState(null);
  const [accountsToolbarActionsSlot, setAccountsToolbarActionsSlot] = useState(null);
  const accountsScopePopoverRef = useRef(null);
  const syncInFlightConnectionsRef = useRef(new Set());
  const refreshQueueRef = useRef(Promise.resolve());
  const [syncAllRunning, setSyncAllRunning] = useState(false);
  const activeSyncBatchStates = useMemo(
    () => getActiveSyncBatchConnectionStates(activeSyncBatches),
    [activeSyncBatches],
  );
  const activeSyncConnectionIds = useMemo(() => {
    const ids = getActiveSyncBatchInstitutionIds(activeSyncBatches);
    (Array.isArray(syncActivity?.active) ? syncActivity.active : []).forEach((activity) => {
      const status = String(activity?.status || '').trim().toLowerCase();
      const institutionId = Number(activity?.institution_id || 0);
      if (institutionId > 0 && (status === 'queued' || status === 'running')) {
        ids.add(institutionId);
      }
    });
    return ids;
  }, [activeSyncBatches, syncActivity]);
  const durableManualSyncRunning = useMemo(
    () => hasActiveManualSyncBatch(activeSyncBatches),
    [activeSyncBatches],
  );

  useEffect(() => {
    localStorage.setItem(ACCOUNT_GROUP_BY_STORAGE_KEY, accountGroupBy);
  }, [accountGroupBy]);

  useEffect(() => () => {
    Object.values(closingExpandedTimeoutsRef.current).forEach(window.clearTimeout);
  }, []);

  const getAccountVisibleGroupKey = useCallback((account) => {
    if (!account) return null;
    if (accountGroupBy === ACCOUNT_GROUP_BY_INSTITUTION) {
      return getInstitutionGroupKey(account.institution_id);
    }
    const grouping = getGroupingForMode(accountGroupBy);
    return getCategoryGroupKey(grouping.getKey(account));
  }, [accountGroupBy]);

  const selectionBelongsToGroup = useCallback((selection, groupKey) => {
    if (selection?.kind !== 'account') return false;
    const selectedAccount = accounts.find((account) => String(account.id) === String(selection.accountId));
    return getAccountVisibleGroupKey(selectedAccount) === groupKey;
  }, [accounts, getAccountVisibleGroupKey]);

  const toggleAccountGroup = useCallback((groupKey) => {
    const isCollapsing = Boolean(expandedState[groupKey]);

    if (closingExpandedTimeoutsRef.current[groupKey]) {
      window.clearTimeout(closingExpandedTimeoutsRef.current[groupKey]);
      delete closingExpandedTimeoutsRef.current[groupKey];
    }

    setExpandedState((currentState) => {
      const isOpen = Boolean(currentState[groupKey]);
      if (isOpen) {
        setClosingExpandedState((currentClosingState) => ({
          ...currentClosingState,
          [groupKey]: true,
        }));
        closingExpandedTimeoutsRef.current[groupKey] = window.setTimeout(() => {
          setClosingExpandedState((currentClosingState) => {
            const nextClosingState = { ...currentClosingState };
            delete nextClosingState[groupKey];
            return nextClosingState;
          });
          delete closingExpandedTimeoutsRef.current[groupKey];
        }, ACCOUNTS_GROUP_COLLAPSE_MS);
      } else {
        setClosingExpandedState((currentClosingState) => {
          if (!currentClosingState[groupKey]) return currentClosingState;
          const nextClosingState = { ...currentClosingState };
          delete nextClosingState[groupKey];
          return nextClosingState;
        });
      }

      return { ...currentState, [groupKey]: !isOpen };
    });

    setDetailSelection((currentSelection) => (
      currentSelection?.kind === 'institution' || (isCollapsing && selectionBelongsToGroup(currentSelection, groupKey))
        ? null
        : currentSelection
    ));
  }, [expandedState, selectionBelongsToGroup]);

  useEffect(() => {
    fetch(`${API}/accounts/balance-history`)
      .then((r) => r.json())
      .then((d) => setBalanceHistory(d))
      .catch(() => {});
    // Re-key on the account set so a just-added or revalued account (including
    // adds via the App-level Add-to-Net-Worth modal) gets its balance series —
    // otherwise its change column is stuck on a stale "—".
  }, [data.accounts]);

  useEffect(() => {
    fetch(`${API}/fx-rates/history`)
      .then((r) => r.json())
      .then((d) => { if (d.rates) setFxHistory(d.rates); })
      .catch(() => {});
  }, [primaryCurrency]);

  const setSyncState = useCallback((updater) => {
    setSyncStateRaw((prev) => (typeof updater === 'function' ? updater(prev) : updater));
  }, []);

  const optimisticSyncActivities = useMemo(() => institutions
    .filter((institution) => syncState?.[connectionStateKey(institution)]?.status === 'syncing')
    .map((institution) => {
      const label = String(institution?.institution || institution?.name || institution?.provider || 'Institution').trim();
      return {
        key: `accounts:${institution.id}`,
        provider: String(institution.provider || '').trim(),
        institutionId: Number(institution.id),
        label,
        tooltip: `Syncing ${label}`,
      };
    }), [institutions, syncState]);

  useEffect(() => {
    onOptimisticSyncActivitiesChange?.(optimisticSyncActivities);
  }, [onOptimisticSyncActivitiesChange, optimisticSyncActivities]);

  useEffect(() => () => {
    onOptimisticSyncActivitiesChange?.([]);
  }, [onOptimisticSyncActivitiesChange]);

  const clearLocalConnectionSyncState = useCallback((institutionId) => {
    const key = connectionStateKey(institutionId);
    setSyncState((prev) => {
      if (!prev?.[key]) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  }, [setSyncState]);

  const scheduleConnectionJustSyncedClear = useCallback((institutionId) => {
    const key = connectionStateKey(institutionId);
    if (!key || key === '0') return;
    const existingTimeoutId = providerSyncSuccessTimeoutsRef.current[key];
    if (existingTimeoutId) {
      clearTimeout(existingTimeoutId);
    }
    providerSyncSuccessTimeoutsRef.current[key] = setTimeout(() => {
      delete providerSyncSuccessTimeoutsRef.current[key];
      setSyncState((prev) => {
        const current = prev?.[key];
        if (current?.status !== 'ok' || !current.justSynced) return prev;
        return {
          ...prev,
          [key]: {
            ...current,
            justSynced: false,
          },
        };
      });
    }, PROVIDER_SYNC_SUCCESS_DISPLAY_MS);
  }, [setSyncState]);

  useEffect(() => {
    if (!institutions?.length) return;
    const activeTransactionConnections = getActiveTransactionImportConnections(transactionImportStatus);
    setSyncState((prev) => {
      let changed = false;
      const next = { ...prev };
      institutions.forEach((inst) => {
        const key = connectionStateKey(inst);
        const local = next[key];
        const transactionImportSyncStatus = getTransactionImportSyncStatus(transactionImportStatus, inst);
        if (activeSyncConnectionIds.has(Number(inst.id))) {
          if (local?.status !== 'syncing' || local?.provider !== inst.provider) {
            next[key] = { status: 'syncing', provider: inst.provider };
            changed = true;
          }
          return;
        }
        if (transactionImportSyncStatus && transactionImportSyncStatus !== 'syncing') {
          const message = transactionImportSyncStatus === TRANSACTION_IMPORT_RETRY_STATUS
            ? getTransactionImportFailureMessage(transactionImportStatus, inst)
            : null;
          if (local?.status !== transactionImportSyncStatus || local?.message !== message || local?.justSynced) {
            next[key] = { status: transactionImportSyncStatus, provider: inst.provider, message };
            changed = true;
          }
          const timeoutId = optimisticSyncTimeoutsRef.current[inst.id];
          if (timeoutId) {
            clearTimeout(timeoutId);
            delete optimisticSyncTimeoutsRef.current[inst.id];
          }
          return;
        }
        if (!local) return;
        if (local.status === 'syncing' || SETTLING_PRESERVED_FAILURE_STATUSES.has(local.status)) {
          const autoState = autoSyncStates?.[key];
          const providerStillActive = syncInFlightConnectionsRef.current.has(key)
            || activeSyncConnectionIds.has(Number(inst.id))
            || activeTransactionConnections.has(Number(inst.id))
            || autoState?.status === 'syncing';
          // While still settling, keep the in-flight "syncing" state OR a just-
          // observed failure (e.g. IBKR auth_required) instead of adopting the
          // lagging backend sync_status, which can still read the previous "ok"
          // until the pending-status write drains. Once settled, reconcile below.
          if (providerStillActive) return;
        }
        if (local.status === 'ok' && local.justSynced) return;
        if (isRecentClientSyncFailure(local)) return;
        const backendStatus = inst.sync_status || 'ok';
        const preserveMessage = (
          local.message
          && MESSAGE_PRESERVING_STATUSES.has(local.status)
          && local.status === backendStatus
        );
        if (local.status !== backendStatus || (local.message && !preserveMessage) || local.justSynced) {
          next[key] = preserveMessage
            ? { status: backendStatus, message: local.message }
            : { status: backendStatus };
          changed = true;
        }
      });
      return changed ? next : prev;
    });
  }, [activeSyncConnectionIds, autoSyncStates, institutions, setSyncState, transactionImportStatus]);

  const clearOptimisticLastSynced = (institutionId) => {
    const timeoutId = optimisticSyncTimeoutsRef.current[institutionId];
    if (timeoutId) {
      clearTimeout(timeoutId);
      delete optimisticSyncTimeoutsRef.current[institutionId];
    }
    setOptimisticLastSyncedByInstitution((prev) => {
      if (!(institutionId in prev)) return prev;
      const next = { ...prev };
      delete next[institutionId];
      return next;
    });
  };

  const markInstitutionJustSynced = (institutionId) => {
    const syncedAt = getAppNow().toISOString();
    const existingTimeoutId = optimisticSyncTimeoutsRef.current[institutionId];
    if (existingTimeoutId) {
      clearTimeout(existingTimeoutId);
    }
    optimisticSyncTimeoutsRef.current[institutionId] = setTimeout(() => {
      clearOptimisticLastSynced(institutionId);
    }, OPTIMISTIC_SYNC_TTL_MS);
    setOptimisticLastSyncedByInstitution((prev) => ({
      ...prev,
      [institutionId]: syncedAt,
    }));
  };

  useEffect(() => {
    setOptimisticLastSyncedByInstitution((prev) => {
      let changed = false;
      const next = { ...prev };

      Object.entries(prev).forEach(([institutionId, optimisticSyncedAt]) => {
        const backendSyncedAt = getInstLastSynced(accounts, Number(institutionId));
        const optimisticDate = parseSyncedAt(optimisticSyncedAt);
        const backendDate = parseSyncedAt(backendSyncedAt);

        if (!optimisticDate || !backendDate) return;

        if (Math.abs(optimisticDate.getTime() - backendDate.getTime()) <= OPTIMISTIC_SYNC_RECONCILIATION_WINDOW_MS) {
          changed = true;
          const timeoutId = optimisticSyncTimeoutsRef.current[institutionId];
          if (timeoutId) {
            clearTimeout(timeoutId);
            delete optimisticSyncTimeoutsRef.current[institutionId];
          }
          delete next[institutionId];
        }
      });

      return changed ? next : prev;
    });
  }, [accounts, OPTIMISTIC_SYNC_RECONCILIATION_WINDOW_MS]);

  useEffect(() => () => {
    Object.values(optimisticSyncTimeoutsRef.current).forEach(clearTimeout);
    optimisticSyncTimeoutsRef.current = {};
  }, []);

  const setConnectionJustSyncedState = (institution) => {
    const key = connectionStateKey(institution);
    setSyncState((prev) => ({
      ...prev,
      [key]: { status: 'ok', justSynced: true, provider: institution.provider },
    }));
    scheduleConnectionJustSyncedClear(institution.id);
  };

  useEffect(() => {
    const activeConnections = getActiveTransactionImportConnections(transactionImportStatus);
    setSyncState((prev) => {
      let changed = false;
      const next = { ...prev };
      institutions.forEach((inst) => {
        const key = connectionStateKey(inst);
        const local = next[key];
        if (!local?.awaitingTransactionImport || activeConnections.has(Number(inst.id))) {
          return;
        }
        const settlementStatus = getTransactionImportSettlementStatus(transactionImportStatus, inst);
        if (settlementStatus === 'unknown' || settlementStatus === 'active') {
          return;
        }
        if (settlementStatus !== 'complete') {
          const status = getTransactionImportSyncStatus(transactionImportStatus, inst) || TRANSACTION_IMPORT_RETRY_STATUS;
          const message = status === TRANSACTION_IMPORT_RETRY_STATUS
            ? getTransactionImportFailureMessage(transactionImportStatus, inst)
            : null;
          next[key] = { status, provider: inst.provider, message };
          changed = true;
          return;
        }
        const syncedAt = getAppNow().toISOString();
        const existingTimeoutId = optimisticSyncTimeoutsRef.current[inst.id];
        if (existingTimeoutId) {
          clearTimeout(existingTimeoutId);
        }
        optimisticSyncTimeoutsRef.current[inst.id] = setTimeout(() => {
          const institutionId = inst.id;
          delete optimisticSyncTimeoutsRef.current[institutionId];
          setOptimisticLastSyncedByInstitution((prevOptimistic) => {
            if (!(institutionId in prevOptimistic)) return prevOptimistic;
            const nextOptimistic = { ...prevOptimistic };
            delete nextOptimistic[institutionId];
            return nextOptimistic;
          });
        }, OPTIMISTIC_SYNC_TTL_MS);
        setOptimisticLastSyncedByInstitution((prevOptimistic) => ({
          ...prevOptimistic,
          [inst.id]: syncedAt,
        }));
        scheduleConnectionJustSyncedClear(inst.id);
        next[key] = { status: 'ok', justSynced: true, provider: inst.provider };
        changed = true;
      });
      return changed ? next : prev;
    });
  }, [institutions, transactionImportStatus, OPTIMISTIC_SYNC_TTL_MS, scheduleConnectionJustSyncedClear, setSyncState]);

  useEffect(() => {
    if (!institutions?.length) return;
    const failedIds = institutions
      .filter((inst) => {
        const status = getTransactionImportSyncStatus(transactionImportStatus, inst);
        return status === TRANSACTION_IMPORT_RETRY_STATUS || status === 'auth_required';
      })
      .map((inst) => inst.id);
    if (!failedIds.length) return;
    failedIds.forEach((institutionId) => {
      const timeoutId = optimisticSyncTimeoutsRef.current[institutionId];
      if (timeoutId) {
        clearTimeout(timeoutId);
        delete optimisticSyncTimeoutsRef.current[institutionId];
      }
    });
    setOptimisticLastSyncedByInstitution((prev) => {
      let changed = false;
      const next = { ...prev };
      failedIds.forEach((institutionId) => {
        if (institutionId in next) {
          delete next[institutionId];
          changed = true;
        }
      });
      return changed ? next : prev;
    });
  }, [institutions, transactionImportStatus]);

  useEffect(() => () => {
    Object.values(providerSyncSuccessTimeoutsRef.current).forEach(clearTimeout);
    providerSyncSuccessTimeoutsRef.current = {};
  }, []);

  const clearAutoSyncStateForConnection = (institutionId) => {
    const key = connectionStateKey(institutionId);
    if (!setAutoSyncStates) return;
    setAutoSyncStates((prev) => {
      if (!prev || !prev[key]) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const setTransientConnectionStatus = (institution, status, message, ttlMs = 5000) => {
    const key = connectionStateKey(institution);
    setSyncState((prev) => ({
      ...prev,
      [key]: { status, message, provider: institution.provider },
    }));
    setTimeout(() => {
      setSyncState((prev) => {
        if (prev?.[key]?.status !== status) {
          return prev;
        }
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }, ttlMs);
  };

  // While the popover is open `scopeDraft` holds in-progress toggles; otherwise
  // the central allScopeInstitutions prop is the single source of truth.
  const scopeInstitutions = scopeDraft ?? allScopeInstitutions;
  const hasHiddenInstitutions = scopeInstitutions.some((inst) => inst.hidden || inst.accounts.some((account) => account.hidden));
  const scopeTotalAccountCount = scopeInstitutions.reduce((total, inst) => total + inst.accounts.length, 0);
  const selectedScopeAccountIds = useMemo(
    () =>
      scopeInstitutions.flatMap((inst) => (
        inst.hidden
          ? []
          : inst.accounts.filter((account) => !account.hidden).map((account) => account.id)
      )),
    [scopeInstitutions]
  );
  const selectedScopeAccountIdSet = useMemo(
    () => new Set(selectedScopeAccountIds),
    [selectedScopeAccountIds]
  );
  const selectedScopeInstitutionCount = useMemo(
    () =>
      scopeInstitutions.filter((inst) =>
        !inst.hidden && inst.accounts.some((account) => !account.hidden)
      ).length,
    [scopeInstitutions]
  );
  const scopeSummary = formatScopeSelectionSummary({
    totalInstitutions: scopeInstitutions.length,
    selectedInstitutions: selectedScopeInstitutionCount,
    selectedAccounts: selectedScopeAccountIds.length,
  });
  const allScopeSourcesSelected = scopeTotalAccountCount > 0 && selectedScopeAccountIds.length === scopeTotalAccountCount;

  useLayoutEffect(() => {
    if (typeof document === 'undefined') {
      return undefined;
    }

    let frameId = null;
    const updateToolbarSlots = () => {
      setAccountsToolbarGroupSlot(document.getElementById('accounts-toolbar-group-slot'));
      setAccountsToolbarScopeSlot(document.getElementById('accounts-toolbar-scope-slot'));
      setAccountsToolbarActionsSlot(document.getElementById('accounts-toolbar-actions-slot'));
    };
    const scheduleToolbarSlotUpdate = () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      frameId = window.requestAnimationFrame(() => {
        frameId = null;
        updateToolbarSlots();
      });
    };

    updateToolbarSlots();
    const observer = new MutationObserver(scheduleToolbarSlotUpdate);
    observer.observe(document.body, { childList: true, subtree: true });

    return () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      observer.disconnect();
    };
  }, []);

  const closeScopeDropdownWithoutSave = useCallback(() => {
    setScopeDraft(null);
    setShowVisDropdown(false);
  }, []);

  const handleToggleScopeDropdown = () => {
    if (showVisDropdown) {
      closeScopeDropdownWithoutSave();
      return;
    }

    setScopeDraft(cloneScopeInstitutions(allScopeInstitutions));
    setShowVisDropdown(true);
  };

  useEffect(() => {
    if (!showVisDropdown) {
      return undefined;
    }

    const handlePointerDown = (event) => {
      if (event.target?.closest?.(APP_NON_DISMISS_INTERACTION_SELECTOR)) return;
      if (accountsScopePopoverRef.current && !accountsScopePopoverRef.current.contains(event.target)) {
        closeScopeDropdownWithoutSave();
      }
    };

    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        closeScopeDropdownWithoutSave();
      }
    };

    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('touchstart', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('touchstart', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [closeScopeDropdownWithoutSave, showVisDropdown]);

  const handleToggleScopeInstitution = (institution) => {
    const accountIds = institution.accountIds;

    if (accountIds.length === 0) {
      return;
    }

    setScopeDraft((previous) => previous.map((inst) => {
      if (inst.id !== institution.id) {
        return inst;
      }

      const accountsForInstitution = inst.accounts;
      const hasAnySelected = !inst.hidden && accountsForInstitution.some((account) => !account.hidden);
      return {
        ...inst,
        hidden: hasAnySelected,
        accounts: accountsForInstitution.map((account) => ({
          ...account,
          hidden: hasAnySelected,
        })),
      };
    }));
  };

  const handleToggleScopeAccount = (accountId) => {
    const isCurrentlySelected = selectedScopeAccountIdSet.has(accountId);

    setScopeDraft((previous) => previous.map((inst) => {
      const accountsForInstitution = inst.accounts;

      if (!accountsForInstitution.some((account) => account.id === accountId)) {
        return inst;
      }

      const nextAccounts = accountsForInstitution.map((account) => {
        if (account.id === accountId) {
          return { ...account, hidden: isCurrentlySelected };
        }

        if (inst.hidden && !isCurrentlySelected) {
          return { ...account, hidden: true };
        }

        return account;
      });
      const hasSelectedAccount = nextAccounts.some((account) => !account.hidden);

      return {
        ...inst,
        hidden: !hasSelectedAccount,
        accounts: nextAccounts,
      };
    }));
  };

  const handleToggleAllScopeSources = () => {
    setScopeDraft((previous) => previous.map((inst) => ({
      ...inst,
      hidden: allScopeSourcesSelected,
      accounts: inst.accounts.map((account) => ({
        ...account,
        hidden: allScopeSourcesSelected,
      })),
    })));
  };

  const handleApplyVisibility = async () => {
    const edited = scopeInstitutions;
    setScopeSaveError('');
    setScopeSaving(true);

    try {
      await persistVisibilityScope({
        apiBase: API,
        institutions: edited,
        previousInstitutions: allScopeInstitutions,
      });
      setShowVisDropdown(false);
      setScopeDraft(null);
      // The local visibility writes are complete; the expensive page/scope reload can
      // catch up in the background so Apply doesn't feel like a full app refresh.
      if (onDataChange) Promise.resolve(onDataChange()).catch(() => {});
      if (fetchAllScopeInstitutions) Promise.resolve(fetchAllScopeInstitutions()).catch(() => {});
    } catch (err) {
      console.error('Failed to save visibility:', err);
      setScopeSaveError(err?.message || 'Could not save the selected scope.');
      const authoritativeInstitutions = await reconcileVisibilityScope({
        onDataChange,
        fetchAllScopeInstitutions,
      });
      if (authoritativeInstitutions) {
        setScopeDraft(cloneScopeInstitutions(authoritativeInstitutions));
      }
    } finally {
      setScopeSaving(false);
    }
  };

  const isCustomRange = timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY;
  const periodBalanceDate = isCustomRange ? getCustomRangeEndDate(customDateRange) : null;
  const convertPeriodBalanceToPrimary = useCallback(
    (amount, fromCurrency) => convertAtDate(amount, fromCurrency, primaryCurrency, periodBalanceDate),
    [convertAtDate, periodBalanceDate, primaryCurrency],
  );
  const convertChangeToPrimaryAtDate = useCallback(
    (amount, fromCurrency, date) => convertAtDate(amount, fromCurrency, primaryCurrency, date),
    [convertAtDate, primaryCurrency],
  );

  const sortedInstitutions = useMemo(() => {
    const accountsByInstitutionId = accounts.reduce((map, account) => {
      const institutionAccounts = map.get(account.institution_id) || [];
      institutionAccounts.push(account);
      map.set(account.institution_id, institutionAccounts);
      return map;
    }, new Map());
    const getPeriodBalance = (account) => getAccountDisplayBalance(account, balanceHistory, timeframe, customDateRange);
    const getInstitutionSortModel = (institution) => {
      const institutionAccounts = accountsByInstitutionId.get(institution.id) || [];
      const changeSummary = getAccountGroupChangeSummary(institutionAccounts, balanceHistory, timeframe, customDateRange, convertChangeToPrimaryAtDate, getPeriodBalance, periodBalanceDate);
      const autoState = autoSyncStates?.[connectionStateKey(institution)] || null;
      const lastSynced = getInstLastSynced(
        accounts,
        institution.id,
        getMostRecentSyncedAt(
          optimisticLastSyncedByInstitution?.[institution.id] || null,
          autoState?.institutionId === institution.id
            ? getActiveOptimisticSyncedAt(autoState.optimisticLastSyncedAt)
            : null,
        ),
      );
      const parsedLastSynced = parseSyncedAt(lastSynced);
      return {
        label: institution.name,
        netTotal: getAccountGroupNetTotal(institutionAccounts, convertPeriodBalanceToPrimary, getPeriodBalance),
        startTotal: changeSummary.hasStartData ? changeSummary.start : null,
        changeSummary,
        syncTimestamp: parsedLastSynced ? parsedLastSynced.getTime() : null,
      };
    };
    return [...institutions].sort((a, b) => {
      const aModel = getInstitutionSortModel(a);
      const bModel = getInstitutionSortModel(b);
      const sortResult = compareAccountListSortValues(aModel, bModel, accountListSort);
      if (sortResult !== 0) return sortResult;
      const aTotal = aModel.netTotal;
      const bTotal = bModel.netTotal;
      if (aTotal !== bTotal) return bTotal - aTotal;
      return a.name.localeCompare(b.name);
    });
  }, [accountListSort, accounts, autoSyncStates, balanceHistory, convertChangeToPrimaryAtDate, convertPeriodBalanceToPrimary, customDateRange, institutions, optimisticLastSyncedByInstitution, periodBalanceDate, timeframe]);

  // Cash, user manual institutions, and net-worth asset/debt buckets are all
  // first-class rows in the Account List table now (folded in from the old Cash
  // panel). Dashboard net-worth totals already count them.
  const tableAccounts = accounts;
  const tableInstitutions = sortedInstitutions;

  useEffect(() => {
    if (!detailSelection) return;
    if (detailSelection.kind === 'account' && !tableAccounts.some((account) => String(account.id) === String(detailSelection.accountId))) {
      setDetailSelection(null);
      return;
    }
    if (detailSelection.kind === 'institution' && !tableInstitutions.some((institution) => String(institution.id) === String(detailSelection.institutionId))) {
      setDetailSelection(null);
    }
  }, [detailSelection, tableAccounts, tableInstitutions]);

  const handleRename = () => {
    // Persistence happens in EditableAccountName; trigger the central refresh.
    if (onDataChange) onDataChange();
  };

  const queueRefreshData = () => {
    refreshQueueRef.current = refreshQueueRef.current
      .catch(() => {})
      .then(() => { if (onDataChange) onDataChange(); });
    return refreshQueueRef.current;
  };

  const getAccountsSyncEndpoint = (provider, { userInitiated = false } = {}) => {
    if (userInitiated) {
      return getUserInitiatedSyncEndpoint(provider);
    }
    return getBackgroundSyncEndpoint(provider);
  };

  const getAccountsSyncTimeoutMs = ({ userInitiated = false } = {}) => {
    if (userInitiated) {
      return USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS;
    }
    return DEFAULT_SYNC_REQUEST_TIMEOUT_MS;
  };

  const runInstitutionSync = async (
    inst,
    { userInitiated = false, syncSource = null } = {},
  ) => {
    const stateKey = connectionStateKey(inst);
    const syncEndpoint = getAccountsSyncEndpoint(inst.provider, { userInitiated });
    const timeoutMs = getAccountsSyncTimeoutMs({ userInitiated });
    const initiationSource = syncSource || (userInitiated ? 'individual_sync' : 'autosync');
    if (!syncEndpoint) return null;
    if (syncInFlightConnectionsRef.current.has(stateKey)) return null;

    syncInFlightConnectionsRef.current.add(stateKey);
    clearAutoSyncStateForConnection(inst.id);
    setSyncState((prev) => ({
      ...prev,
      [stateKey]: { status: 'syncing', provider: inst.provider },
    }));

    try {
      const requestOptions = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildInstitutionSyncRequestBody(
          inst.id,
          null,
          initiationSource,
        )),
      };
      const resp = await fetchWithTimeout(`${API}${syncEndpoint}`, requestOptions, timeoutMs);
      const result = await resp.json();
      const networkBlocker = getSyncNetworkBlocker(result);

      if (networkBlocker) {
        clearLocalConnectionSyncState(inst.id);
        onSyncNetworkNoticeChange?.(networkBlocker);
      } else if (result.status === 'ok' && hasActiveTransactionImportJob(result)) {
        // Server reports an active tximport job — keep the chip on "Syncing..."
        // unconditionally. The useEffect that watches transactionImportStatus
        // will clear awaitingTransactionImport (and mark "just synced") once
        // the tximport actually leaves the active set, including the rare
        // case where the tximport finished before this response landed.
        setSyncState((prev) => ({
          ...prev,
          [stateKey]: {
            status: 'syncing',
            awaitingTransactionImport: true,
            provider: inst.provider,
          },
        }));
      } else if (result.status === 'ok') {
        markInstitutionJustSynced(inst.id);
        setConnectionJustSyncedState(inst);
      } else if (isProviderAuthStatus(result.status)) {
        setSyncState((prev) => ({
          ...prev,
          [stateKey]: {
            status: 'auth_required',
            message: result.message || null,
            provider: inst.provider,
          },
        }));
      } else if (result.status === 'already_syncing') {
        setTransientConnectionStatus(
          inst,
          'already_syncing',
          result.message || 'Sync already in progress',
        );
      } else if (result.status === 'network_error') {
        setSyncState((prev) => ({
          ...prev,
          [stateKey]: {
            status: 'network_error',
            message: formatSyncErrorMessage(inst.provider, result.message),
            provider: inst.provider,
          },
        }));
      } else {
        setSyncState((prev) => ({
          ...prev,
          [stateKey]: {
            status: 'error',
            message: formatSyncErrorMessage(inst.provider, result.message),
            provider: inst.provider,
          },
        }));
      }

      if (!networkBlocker) {
        onSyncNetworkNoticeChange?.(null);
      }
      return result;
    } catch (err) {
      const failureState = getClientSyncFailureState(inst.provider, err);
      const result = {
        status: failureState.status,
        message: failureState.message,
        clientOnly: true,
        updatedAt: getAppNow().toISOString(),
      };
      setSyncState((prev) => ({
        ...prev,
        [stateKey]: { ...result, provider: inst.provider },
      }));
      return result;
    } finally {
      syncInFlightConnectionsRef.current.delete(stateKey);
    }
  };

  const handleSyncClick = async (inst) => {
    const stateKey = connectionStateKey(inst);
    const localStatus = syncState?.[stateKey]?.status;
    const backendStatus = inst?.sync_status || null;
    const autoStatus = autoSyncStates?.[stateKey]?.status;
    if ([localStatus, backendStatus, autoStatus].includes('auth_required')) {
      handleAuthClick(inst);
      return;
    }
    const result = await runInstitutionSync(inst, { userInitiated: true });
    if (!result) return;
    await queueRefreshData();
  };

  const autoSyncBusy = autoSyncInProgress || Object.values(autoSyncStates || {}).some((s) => s.status === 'syncing');
  const durableSyncBusy = activeSyncConnectionIds.size > 0;
  const syncAllBusy = syncAllRunning || durableManualSyncRunning;

  useEffect(() => {
    setSyncAllBlocked?.(syncAllBusy);
    return () => setSyncAllBlocked?.(false);
  }, [setSyncAllBlocked, syncAllBusy]);

  const applyBatchSyncResult = (inst, result) => {
    const stateKey = connectionStateKey(inst);
    const networkBlocker = getSyncNetworkBlocker(result);
    if (networkBlocker) {
      clearLocalConnectionSyncState(inst.id);
      onSyncNetworkNoticeChange?.(networkBlocker);
    } else if (result.status === 'ok' && hasActiveTransactionImportJob(result)) {
      // Account/balance sync is terminal, but this connection remains active
      // until its durable background transaction job settles.
      setSyncState((prev) => ({
        ...prev,
        [stateKey]: {
          status: 'syncing',
          awaitingTransactionImport: true,
          provider: inst.provider,
        },
      }));
    } else if (result.status === 'ok') {
      const institutionId = result.institution_id || inst.id;
      markInstitutionJustSynced(institutionId);
      setConnectionJustSyncedState(inst);
    } else if (isProviderAuthStatus(result.status)) {
      setSyncState((prev) => ({
        ...prev,
        [stateKey]: {
          status: 'auth_required',
          message: result.message || null,
          provider: inst.provider,
        },
      }));
    } else if (result.status === 'already_syncing') {
      setTransientConnectionStatus(
        inst,
        'already_syncing',
        result.message || 'Sync already in progress',
      );
    } else if (result.status === 'network_error') {
      setSyncState((prev) => ({
        ...prev,
        [stateKey]: {
          status: 'network_error',
          message: formatSyncErrorMessage(inst.provider, result.message),
          provider: inst.provider,
        },
      }));
    } else {
      setSyncState((prev) => ({
        ...prev,
        [stateKey]: {
          status: 'error',
          message: formatSyncErrorMessage(inst.provider, result.message),
          provider: inst.provider,
        },
      }));
    }
  };

  const handleSyncAll = async () => {
    if (syncAllBusy || autoSyncBusy || durableSyncBusy) return;
    setSyncAllRunning(true);
    setAuthModal(null);
    setAuthSkipAutoLogin(false);
    let syncableInstitutions = [];
    let moomooSync = null;
    const processedConnections = new Set();

    try {
      syncableInstitutions = institutions
        .filter((inst) => Boolean(getUserInitiatedSyncEndpoint(inst.provider)));

      const batchInstitutions = syncableInstitutions.filter((inst) => inst.provider !== 'moomoo');
      const moomooInstitutions = syncableInstitutions.filter((inst) => inst.provider === 'moomoo');
      const institutionById = new Map(batchInstitutions.map((inst) => [Number(inst.id), inst]));
      syncableInstitutions.forEach((inst) => {
        const stateKey = connectionStateKey(inst);
        clearAutoSyncStateForConnection(inst.id);
        setSyncState((prev) => ({
          ...prev,
          [stateKey]: { status: 'syncing', provider: inst.provider },
        }));
      });

      const { moomooPromise, batchPromise } = startIndependentSyncLanes({
        startMoomoo: moomooInstitutions.length > 0
          ? () => Promise.all(moomooInstitutions.map((moomooInstitution) => (
            runInstitutionSync(moomooInstitution, {
              userInitiated: true,
              syncSource: 'sync_all',
            })
              .finally(() => {
                processedConnections.add(String(moomooInstitution.id));
              })
          )))
          : null,
        startBatch: batchInstitutions.length > 0
          ? () => runSyncBatchUntilDone({
            connections: batchInstitutions.map((inst) => ({
              provider: inst.provider,
              institution_id: inst.id,
            })),
            mode: 'manual',
            onConnectionResult: (_connectionKey, result) => {
              const institutionId = Number(result?.institution_id || 0);
              const processedKey = String(institutionId);
              if (processedConnections.has(processedKey)) return;
              if (!result?.status || result.status === 'pending' || result.status === 'syncing') return;
              processedConnections.add(processedKey);
              const inst = institutionById.get(institutionId);
              if (inst) {
                applyBatchSyncResult(inst, result);
              }
            },
          })
          : null,
      });
      moomooSync = moomooPromise;

      if (batchPromise) {
        const batch = await batchPromise;
        const finalStatus = String(batch?.status || '').toLowerCase();
        if (finalStatus === 'blocked') {
          const networkBlocker = getSyncNetworkBlocker(batch);
          batchInstitutions.forEach((inst) => {
            clearLocalConnectionSyncState(inst.id);
          });
          if (networkBlocker) {
            onSyncNetworkNoticeChange?.(networkBlocker);
          }
          if (moomooSync) {
            await moomooSync;
            queueRefreshData();
            await refreshQueueRef.current.catch(() => {});
          }
          if (networkBlocker) {
            onSyncNetworkNoticeChange?.(networkBlocker);
          }
          return;
        }
        if (!batch || finalStatus === 'not_found' || finalStatus === 'error') {
          throw new Error(batch?.message || 'Sync All batch failed');
        }
        onSyncNetworkNoticeChange?.(null);
      }

      if (moomooSync) {
        await moomooSync;
      }

      await queueRefreshData();
      await refreshQueueRef.current.catch(() => {});
    } catch (err) {
      if (moomooSync) {
        await moomooSync.catch(() => null);
      }
      syncableInstitutions.forEach((inst) => {
        if (processedConnections.has(String(inst.id))) return;
        const failureState = getClientSyncFailureState(inst.provider, err);
        const stateKey = connectionStateKey(inst);
        setSyncState((prev) => ({
          ...prev,
          [stateKey]: {
            status: failureState.status,
            message: failureState.message,
            provider: inst.provider,
            clientOnly: true,
            updatedAt: getAppNow().toISOString(),
          },
        }));
      });
      if (processedConnections.size > 0) {
        await queueRefreshData();
        await refreshQueueRef.current.catch(() => {});
      }
    } finally {
      setSyncAllRunning(false);
    }
  };

  const handleAuthClick = (inst) => {
    const action = getProviderAuthAction(inst.provider, inst.sync_status, 'auth_required');
    if (!action) {
      return;
    }
    if (action.modal === 'api') {
      setApiCredModal(inst);
      return;
    }
    setAuthSkipAutoLogin(Boolean(action.skipAutoLogin));
    setAuthModal(inst);
  };

  const handleCredErrorClick = (inst) => {
    const action = getProviderAuthAction(inst.provider, inst.sync_status, 'credential_error');
    if (!action) {
      return;
    }
    if (action.modal === 'api') {
      setApiCredModal(inst);
      return;
    }
    setAuthSkipAutoLogin(Boolean(action.skipAutoLogin));
    setAuthModal(inst);
  };

  const handleAuthSuccess = async () => {
    if (authModal) {
      clearAutoSyncStateForConnection(authModal.id);
      markInstitutionJustSynced(authModal.id);
      setConnectionJustSyncedState(authModal);
    }
    setAuthModal(null);
    if (onDataChange) onDataChange();
    if (fetchAllScopeInstitutions) await fetchAllScopeInstitutions();
  };

  const handleInstitutionResultUpdate = (institution, provider, status, message) => {
    if (!institution) return;
    const stateKey = connectionStateKey(institution);
    setSyncState((prev) => ({
      ...prev,
      [stateKey]: { status, message, provider },
    }));
    if (SETTLING_PRESERVED_FAILURE_STATUSES.has(status)) {
      void queueRefreshData();
    }
  };

  const visibleAccountGroupKeys = useMemo(() => {
    if (accountGroupBy !== ACCOUNT_GROUP_BY_INSTITUTION) {
      const { groups, getKey } = getGroupingForMode(accountGroupBy);
      const populatedKeys = new Set(accounts.map((account) => getCategoryGroupKey(getKey(account))));
      return groups
        .map((group) => getCategoryGroupKey(group.key))
        .filter((groupKey) => populatedKeys.has(groupKey));
    }
    return sortedInstitutions.map((institution) => getInstitutionGroupKey(institution.id));
  }, [accountGroupBy, accounts, sortedInstitutions]);
  // During the welcome tour (demo mode), expand every group so the account rows are visible.
  useEffect(() => {
    if (!isTourDemoActive() || visibleAccountGroupKeys.length === 0) return;
    setExpandedState((prev) => {
      let changed = false;
      const next = { ...prev };
      visibleAccountGroupKeys.forEach((groupKey) => {
        if (!next[groupKey]) { next[groupKey] = true; changed = true; }
      });
      return changed ? next : prev;
    });
  }, [visibleAccountGroupKeys]);
  useEffect(() => {
    const applyTourHint = () => {
      const hint = getTourHint();
      if (!isTourDemoActive() || hint?.page !== 'accounts' || !hint.institutionId) return;
      if (!tableInstitutions.some((institution) => String(institution.id) === String(hint.institutionId))) return;
      setExpandedState((prev) => {
        const groupKey = getInstitutionGroupKey(hint.institutionId);
        return prev[groupKey] ? prev : { ...prev, [groupKey]: true };
      });
      setDetailSelection({ kind: 'institution', institutionId: hint.institutionId });
    };
    applyTourHint();
    window.addEventListener('breaktwenty-tour-hint', applyTourHint);
    return () => window.removeEventListener('breaktwenty-tour-hint', applyTourHint);
  }, [tableInstitutions]);
  const currentTourHint = getTourHint();
  const lockedTourInstitutionId = currentTourHint?.page === 'accounts' ? currentTourHint.institutionId : null;
  const accountsTourTrayLocked = Boolean(
    detailSelection
    && isTourDemoActive()
    && lockedTourInstitutionId
    && (
      (detailSelection.kind === 'institution' && String(detailSelection.institutionId) === String(lockedTourInstitutionId))
      || (
        detailSelection.kind === 'account'
        && tableAccounts.some(
          (account) => String(account.id) === String(detailSelection.accountId)
            && String(account.institution_id) === String(lockedTourInstitutionId)
        )
      )
    )
  );
  const handleDetailSelect = useCallback((nextSelection) => {
    setDetailSelection((currentSelection) => {
      if (isSameAccountsDetailSelection(currentSelection, nextSelection)) {
        return accountsTourTrayLocked ? currentSelection : null;
      }
      return nextSelection;
    });
  }, [accountsTourTrayLocked]);
  const allVisibleAccountGroupsExpanded = visibleAccountGroupKeys.length > 0
    && visibleAccountGroupKeys.every((groupKey) => expandedState[groupKey]);
  const timeframeMenuLabel = timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY
    ? 'Custom Range'
    : PORTFOLIO_TIMEFRAMES.find((item) => item.label === timeframe)?.menuLabel || timeframe;
  const isCustomTimeframe = timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY;
  const isAllTimeframe = timeframe === 'All';
  const customStartLabel = formatAccountsDateLabel(getCustomRangeStartDate(customDateRange));
  const customEndLabel = formatAccountsDateLabel(getCustomRangeEndDate(customDateRange));
  const rollingStartLabel = formatAccountsDateLabel(getRollingRangeStartDate(timeframe));
  const changeColumnLabel = timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY
    ? 'Custom Change'
    : timeframe === 'All'
      ? 'All Time Change'
      : `${timeframeMenuLabel} Change`;
  const timelineStartColumnLabel = isAllTimeframe ? 'Initial Balance' : 'From';
  const timelineEndColumnLabel = isCustomTimeframe ? 'To' : 'Current Balance';
  const timelineStartShowDate = !isAllTimeframe;
  const timelineEndShowDate = isCustomTimeframe;
  const timelineStartHeaderLabel = isAllTimeframe
    ? 'Initial Balance'
    : `From ${isCustomTimeframe ? customStartLabel : rollingStartLabel}`.trim();
  const timelineEndHeaderLabel = isCustomTimeframe
    ? `To ${customEndLabel}`.trim()
    : 'Current Balance';

  const toggleAllVisibleAccountGroups = () => {
    const shouldExpand = !allVisibleAccountGroupsExpanded;
    const nextExpandedState = {};

    visibleAccountGroupKeys.forEach((groupKey) => {
      nextExpandedState[groupKey] = shouldExpand;
    });

    setExpandedState(nextExpandedState);

    if (!shouldExpand) {
      setDetailSelection((currentSelection) => (
        visibleAccountGroupKeys.some((groupKey) => selectionBelongsToGroup(currentSelection, groupKey))
          ? null
          : currentSelection
      ));
    }
  };

  const accountsScopeControl = (
    <div className="investments-filter-popover dashboard-scope-popover" ref={accountsScopePopoverRef}>
      <ScopeSelectorTrigger
        isOpen={showVisDropdown}
        summary={scopeSummary}
        controls="accounts-scope-filter-panel"
        className={hasHiddenInstitutions ? 'has-hidden-sources' : ''}
        onClick={handleToggleScopeDropdown}
      />
      <div
        id="accounts-scope-filter-panel"
        role="dialog"
        aria-label="Accounts scope filters"
        className={`investments-filter-panel scope-selector-panel dashboard-scope-panel ${showVisDropdown ? 'is-open' : ''}`.trim()}
        aria-hidden={!showVisDropdown}
      >
        <InstitutionAccountSelector
          key={showVisDropdown ? 'open' : 'closed'}
          institutions={scopeInstitutions}
          selectedAccountIds={selectedScopeAccountIdSet}
          onToggleInstitution={handleToggleScopeInstitution}
          onToggleAccount={handleToggleScopeAccount}
          headerActions={(
            <>
              <button
                type="button"
                className="institution-filter-toggle-all app-control-root"
                onClick={handleToggleAllScopeSources}
              >
                <span className="app-control-label">{allScopeSourcesSelected ? 'All Off' : 'All On'}</span>
              </button>
              <button
                type="button"
                className="btn-primary scope-filter-apply-btn app-control-root"
                onClick={handleApplyVisibility}
                disabled={scopeSaving}
              >
                <span className="app-control-label">{scopeSaving ? 'Saving' : 'Apply'}</span>
              </button>
            </>
          )}
        />
      </div>
    </div>
  );
  const accountGroupControl = (
    <AccountGroupControl value={accountGroupBy} onChange={setAccountGroupBy} />
  );
  const accountsToolbarActions = (
    <>
      <button
        type="button"
        className="investments-filter-trigger app-view-filter-trigger app-control-root accounts-section-btn accounts-sync-all-btn"
        onClick={handleSyncAll}
        disabled={syncAllBusy || autoSyncBusy || durableSyncBusy}
        aria-label="Sync all institutions"
      >
        <span className="investments-filter-trigger-icon app-view-filter-trigger-icon app-control-icon" aria-hidden="true">
          <MdSync className={syncAllBusy ? 'spin-icon' : ''} />
        </span>
        <span className="investments-view-filter-trigger-label app-view-filter-trigger-label app-control-label">Sync All</span>
      </button>
      <button
        type="button"
        className="investments-filter-trigger app-view-filter-trigger app-control-root accounts-section-btn accounts-expand-all-btn"
        onClick={toggleAllVisibleAccountGroups}
        aria-label={allVisibleAccountGroupsExpanded ? 'Collapse all account groups' : 'Expand all account groups'}
      >
        <span className="investments-filter-trigger-icon app-view-filter-trigger-icon app-control-icon" aria-hidden="true">
          {allVisibleAccountGroupsExpanded ? <MdUnfoldLess /> : <MdUnfoldMore />}
        </span>
        <span className="investments-view-filter-trigger-label app-view-filter-trigger-label app-control-label">{allVisibleAccountGroupsExpanded ? 'Collapse All' : 'Expand All'}</span>
      </button>
    </>
  );

  return (
    <>
      {accountsToolbarGroupSlot ? createPortal(accountGroupControl, accountsToolbarGroupSlot) : null}
      {accountsToolbarScopeSlot ? createPortal(accountsScopeControl, accountsToolbarScopeSlot) : null}
      <CsvExportButton dataset="balances" getParams={() => ({ latest_only: 'true' })} />
      {accountsToolbarActionsSlot ? createPortal(
        <div className="accounts-toolbar-action-controls">
          {accountsToolbarActions}
        </div>,
        accountsToolbarActionsSlot
      ) : null}
      <div className="accounts-section-layout">
        <AppStatusNotice
          title={syncNetworkNotice?.title}
          message={syncNetworkNotice?.message}
          onDismiss={() => onSyncNetworkNoticeChange?.(null)}
        />
        <AppStatusNotice
          title="Scope update failed"
          message={scopeSaveError}
          onDismiss={() => setScopeSaveError('')}
        />
        <div className="section accounts-section-card accounts-institution-panel-list">
          <div className={`accounts-table-shell accounts-institution-panels-shell ${accountGroupBy === ACCOUNT_GROUP_BY_INSTITUTION ? 'is-institution-grouped' : 'is-category-grouped'}`.trim()} data-accounts-horizontal-scroll-sync>
            {accountGroupBy === ACCOUNT_GROUP_BY_INSTITUTION ? (
              <div className="accounts-panel-header-strip accounts-institution-header-strip">
                <div className="accounts-panel-header-inner accounts-institution-header-inner" role="row">
                  <SortableTableHeader
                    label="Institution"
                    sortKey="institution"
                    sortConfig={accountListSort}
                    onSort={() => setAccountListSort((currentSort) => getNextSortConfig(currentSort, 'institution', ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS))}
                    defaultDirections={ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS}
                    className="accounts-panel-header-group"
                  />
                  <SortableTableHeader
                    label="Sync"
                    sortKey="sync"
                    sortConfig={accountListSort}
                    onSort={() => setAccountListSort((currentSort) => getNextSortConfig(currentSort, 'sync', ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS))}
                    defaultDirections={ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS}
                    className="accounts-panel-header-sync"
                  />
                  <span className="accounts-panel-header-flex" aria-hidden="true" />
                  <SortableTableHeader
                    label={timelineStartHeaderLabel}
                    sortKey="start"
                    sortConfig={accountListSort}
                    onSort={() => setAccountListSort((currentSort) => getNextSortConfig(currentSort, 'start', ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS))}
                    defaultDirections={ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS}
                    className="accounts-panel-header-start"
                  />
                  <SortableTableHeader
                    label={timelineEndHeaderLabel}
                    sortKey="balance"
                    sortConfig={accountListSort}
                    onSort={() => setAccountListSort((currentSort) => getNextSortConfig(currentSort, 'balance', ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS))}
                    defaultDirections={ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS}
                    className="accounts-panel-header-end"
                  />
                  <SortableTableHeader
                    label={changeColumnLabel}
                    sortKey="change"
                    sortConfig={accountListSort}
                    onSort={() => setAccountListSort((currentSort) => getNextSortConfig(currentSort, 'change', ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS))}
                    defaultDirections={ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS}
                    className="accounts-panel-header-change"
                  />
                  <span className="accounts-panel-header-spacer" aria-hidden="true" />
                </div>
              </div>
            ) : (
              <div className="accounts-panel-header-strip accounts-category-header-strip">
                <div className="accounts-panel-header-inner accounts-category-header-inner" role="row">
                  <SortableTableHeader
                    label={accountGroupBy === ACCOUNT_GROUP_BY_CATEGORY ? 'Account Type' : 'Section'}
                    sortKey="institution"
                    sortConfig={accountListSort}
                    onSort={() => setAccountListSort((currentSort) => getNextSortConfig(currentSort, 'institution', ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS))}
                    defaultDirections={ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS}
                    className="accounts-panel-header-group"
                  />
                  <span className="accounts-panel-header-flex" aria-hidden="true" />
                  <SortableTableHeader
                    label={timelineStartHeaderLabel}
                    sortKey="start"
                    sortConfig={accountListSort}
                    onSort={() => setAccountListSort((currentSort) => getNextSortConfig(currentSort, 'start', ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS))}
                    defaultDirections={ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS}
                    className="accounts-panel-header-start"
                  />
                  <SortableTableHeader
                    label={timelineEndHeaderLabel}
                    sortKey="balance"
                    sortConfig={accountListSort}
                    onSort={() => setAccountListSort((currentSort) => getNextSortConfig(currentSort, 'balance', ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS))}
                    defaultDirections={ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS}
                    className="accounts-panel-header-end"
                  />
                  <SortableTableHeader
                    label={changeColumnLabel}
                    sortKey="change"
                    sortConfig={accountListSort}
                    onSort={() => setAccountListSort((currentSort) => getNextSortConfig(currentSort, 'change', ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS))}
                    defaultDirections={ACCOUNT_LIST_SORT_DEFAULT_DIRECTIONS}
                    className="accounts-panel-header-change"
                  />
                  <span className="accounts-panel-header-spacer" aria-hidden="true" />
                </div>
              </div>
            )}
            <AccountsHorizontalScrollProxy className="accounts-table-top-scrollbar" />
            <div className="accounts-table-scroll-sizer" aria-hidden="true" />
            <AccountSummary
              institutions={tableInstitutions}
              accounts={tableAccounts}
              transactionImportStatus={transactionImportStatus}
              optimisticLastSyncedByInstitution={optimisticLastSyncedByInstitution}
              expandedState={expandedState}
              closingExpandedState={closingExpandedState}
              balancesHidden={balancesHidden}
              onToggle={toggleAccountGroup}
              timeframe={timeframe}
              customDateRange={customDateRange}
              balanceHistory={balanceHistory}
              convertAtDate={convertAtDate}
              viewCurrency={effectiveViewCurrency}
              primaryCurrency={primaryCurrency}
              onRename={handleRename}
              syncState={syncState}
              autoSyncStates={autoSyncStates}
              autoSyncInProgress={autoSyncInProgress}
              activeBatchStates={activeSyncBatchStates}
              syncActivity={syncActivity}
              onSyncClick={handleSyncClick}
              onAuthClick={handleAuthClick}
              onCredErrorClick={handleCredErrorClick}
              onSettingsClick={(inst) => setSettingsModal(inst)}
              onDetailSelect={handleDetailSelect}
              selectedDetail={detailSelection}
              groupBy={accountGroupBy}
              accountListSort={accountListSort}
              timelineStartColumnLabel={timelineStartColumnLabel}
              timelineEndColumnLabel={timelineEndColumnLabel}
              timelineStartShowDate={timelineStartShowDate}
              timelineEndShowDate={timelineEndShowDate}
            />
            <AccountsHorizontalScrollProxy className="accounts-table-bottom-scrollbar" />
          </div>
        </div>
      </div>

      <AccountsDetailTray
        selection={detailSelection}
        animatedOpen={accountsDetailTray.animatedOpen}
        accounts={tableAccounts}
        institutions={institutions}
        transactionImportStatus={transactionImportStatus}
        optimisticLastSyncedByInstitution={optimisticLastSyncedByInstitution}
        syncState={syncState}
        autoSyncStates={autoSyncStates}
        autoSyncInProgress={autoSyncInProgress}
        activeBatchStates={activeSyncBatchStates}
        syncActivity={syncActivity}
        balanceHistory={balanceHistory}
        convertAtDate={convertAtDate}
        primaryCurrency={primaryCurrency}
        balancesHidden={balancesHidden}
        timeframe={timeframe}
        customDateRange={customDateRange}
        onClose={() => setDetailSelection(null)}
        onDetailSelect={handleDetailSelect}
        dismissLocked={accountsTourTrayLocked}
      />

      {authModal && (
        <ScraperAuthModal
          key={`scraper-auth:${authModal.provider}:${authModal.id || 'new'}:${authModal.isNew ? 'add' : 'existing'}`}
          institution={authModal}
          userTimezone={userTimezone}
          userTimezoneConfigured={userTimezoneConfigured}
          onClose={() => { setAuthModal(null); setAuthSkipAutoLogin(false); }}
          onSuccess={handleAuthSuccess}
          onDataChange={handleDataChange}
          onResultUpdate={(provider, status, message) => (
            handleInstitutionResultUpdate(authModal, provider, status, message)
          )}
          skipAutoLogin={authSkipAutoLogin}
        />
      )}
      {settingsModal && (
        <InstitutionSettingsModal
          institution={settingsModal}
          accounts={tableAccounts}
          institutions={institutions}
          convert={convert}
          onClose={() => setSettingsModal(null)}
          onDataChange={handleDataChange}
          onLogoChange={handleInstitutionLogoChange}
          onReconnect={(targetInstitution) => {
            setSettingsModal(null);
            setApiCredModal({ ...targetInstitution, isNew: false });
          }}
        />
      )}
      {apiCredModal && (
        <ApiAuthModal
          institution={apiCredModal}
          onClose={() => setApiCredModal(null)}
          onResultUpdate={(provider, status, message) => (
            handleInstitutionResultUpdate(apiCredModal, provider, status, message)
          )}
          onSuccess={async () => {
            clearAutoSyncStateForConnection(apiCredModal.id);
            markInstitutionJustSynced(apiCredModal.id);
            setConnectionJustSyncedState(apiCredModal);
            setApiCredModal(null);
            if (onDataChange) onDataChange();
          }}
        />
      )}
    </>
  );
}

export default Accounts;
