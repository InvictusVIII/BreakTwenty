import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import BalancesToggleButton from '../components/BalancesToggleButton';
import CurrencyViewPicker from '../components/CurrencyViewPicker';
import CsvExportButton from '../components/CsvExportButton';
import HorizontalScrollProxy from '../components/HorizontalScrollProxy';
import AppStatusNotice from '../components/AppStatusNotice';
import AppViewFiltersMenu from '../components/AppViewFiltersMenu';
import { getTourHint, isTourDemoActive } from '../components/tourDemoData';
import { MdArrowDownward, MdArrowUpward, MdCheck } from 'react-icons/md';
import { useRightTrayOpenState, useCurrency, usePersistentPanelCollapsed, useTheme } from '../appState';
import { SideDetailDrawerPanel } from '../components/SideDetailDrawer';
import CurrentPeriodIcon from '../components/CurrentPeriodIcon';
import TriangleIcon from '../components/TriangleIcon';
import InstitutionLogo from '../components/InstitutionLogo';
import AccountTypeBadge from '../components/AccountTypeBadge';
import InstitutionAccountSelector, { ScopeSelectorTrigger, formatScopeSelectionSummary } from '../components/InstitutionAccountSelector';
import SortableTableHeader, { getNextSortConfig, usePersistentSortConfig } from '../components/SortableTableHeader';
import TimelineTrigger from '../components/TimelineTrigger';
import TimelineCustomRangePicker from '../components/TimelineCustomRangePicker';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import useBalancesHidden from '../hooks/useBalancesHidden';
import useIncomeTransactions from '../hooks/useIncomeTransactions';
import useTimelineCustomRangeDraft from '../hooks/useTimelineCustomRangeDraft';
import { API } from '../config';
import { ACCOUNT_TYPE_LABELS } from '../constants/providers';
import { SELECTABLE_CURRENCIES } from '../constants/currencies';
import {
  formatCompactMoney,
  currencySymbolFor,
  formatPercent,
  formatQuantity,
  formatSignedCompactMoney,
  formatSignedPercent,
} from '../utils/format';
import { formatShortDateValue as formatShortDateLabel } from '../utils/date';
import { cloneScopeInstitutions } from '../utils/portfolioViewUtils';
import { shouldResetInvestmentsTab, shouldShowInvestmentsTab } from '../utils/investmentsTabs';
import { subscribeMainWindowZoomStatus } from '../utils/desktopBridge';
import { makeCurrencyConverter } from '../utils/currencyView';
import { persistVisibilityScope, reconcileVisibilityScope } from '../utils/visibilityScope';
import { readJsonResponse } from '../utils/apiResponse';
import DividendsByPositionEChart from '../components/holdings/DividendsByPositionEChart';
import GainersLosersEChart from '../components/holdings/GainersLosersEChart';
import HoldingsPieEChart from '../components/holdings/HoldingsPieEChart';
import IncomeHistoryDetailDrawerContent from '../components/holdings/IncomeHistoryDetailDrawerContent';
import IncomeHistoryEChart from '../components/holdings/IncomeHistoryEChart';
import PerformanceLineEChart from '../components/holdings/PerformanceLineEChart';
import TopHoldingEChart from '../components/holdings/TopHoldingEChart';
import FitMoney from '../components/FitMoney';
import {
  formatMoneyNarrow,
  formatSignedMoneyNarrow,
  formatTableMoney,
} from '../components/holdings/holdingsTooltipUtils';
import {
  INCOME_CUSTOM_TIMELINE_KEY,
  INCOME_DETAIL_TRAY_TRANSITION_MS,
  INCOME_TIMELINE_PRESETS,
} from '../components/holdings/holdingsIncomeConstants';
import {
  getDividendPositionSymbolFromEventTarget,
  getInclusiveMonthSpan,
  getIncomeAggregationKey,
  getIncomeAggregationUnitLabel,
  getIncomeBucketMeta,
  getIncomeHistoryAxisStep,
  getIncomeTimelineBounds,
  getIncomeTimelineSummary,
  getTodayDateValue,
} from '../components/holdings/incomeHelpers';
import { getAppNow } from '../utils/appClock';
import './Holdings.css';

const PERFORMANCE_REFRESH_MS = 15 * 60 * 1000;

function isDividendIncomeTourTrayLocked() {
  const hint = getTourHint();
  return (
    isTourDemoActive()
    && hint?.tab === 'income'
    && hint?.incomeSubTab === 'dividends'
    && Boolean(hint?.selectBar)
  );
}

function getCurrentOptionsCalendarMonth() {
  const now = getAppNow();
  return { year: now.getFullYear(), monthIndex: now.getMonth() };
}

function HoldingsMetricsScrollController({ placement = 'top', enableFrameWheel = false }) {
  const controllerRef = useRef(null);
  const visibleRef = useRef(false);
  const [isVisible, setIsVisible] = useState(false);
  const [contentWidth, setContentWidth] = useState(0);

  useLayoutEffect(() => {
    const controller = controllerRef.current;
    const frameElement = controller?.closest?.('.holdings-combined-table-frame');
    const metricsViewport = frameElement?.querySelector?.('.holdings-metrics-clip');
    const metricsTracks = Array.from(frameElement?.querySelectorAll?.('.holdings-metrics-track') || []);
    if (!controller || !frameElement || !metricsViewport || metricsTracks.length === 0) {
      return undefined;
    }

    let scrollFrame = null;
    let scrollable = false;
    const syncPeerControllers = () => {
      frameElement.querySelectorAll('.holdings-metrics-scroll-controller').forEach((otherController) => {
        if (otherController === controller) return;
        if (Math.abs(otherController.scrollLeft - controller.scrollLeft) > 0.5) {
          otherController.scrollLeft = controller.scrollLeft;
        }
      });
    };
    const syncScrollOffset = () => {
      frameElement.style.setProperty('--holdings-metrics-scroll-left', `${controller.scrollLeft}px`);
      syncPeerControllers();
    };
    const scheduleScrollSync = () => {
      if (scrollFrame !== null) return;
      scrollFrame = window.requestAnimationFrame(() => {
        scrollFrame = null;
        syncScrollOffset();
      });
    };
    const measure = () => {
      const controllerWidth = controller.clientWidth || frameElement.clientWidth;
      const viewportWidth = metricsViewport.clientWidth;
      const metricsWidth = Math.max(
        viewportWidth,
        ...metricsTracks.map((track) => Math.max(track.scrollWidth, track.getBoundingClientRect().width)),
      );
      const maximumScrollLeft = Math.max(0, metricsWidth - viewportWidth);
      const controllerContentWidth = controllerWidth + maximumScrollLeft;
      const nextScrollable = controllerContentWidth > controllerWidth + 1;

      setContentWidth((current) => (Math.abs(current - controllerContentWidth) > 0.5 ? controllerContentWidth : current));
      if (controller.scrollLeft > maximumScrollLeft) {
        controller.scrollLeft = maximumScrollLeft;
      }
      if (!nextScrollable && controller.scrollLeft !== 0) {
        controller.scrollLeft = 0;
      }
      scrollable = nextScrollable;
      if (visibleRef.current !== nextScrollable) {
        visibleRef.current = nextScrollable;
        setIsVisible(nextScrollable);
      }
      syncScrollOffset();
    };
    const handleFrameWheel = (event) => {
      if (!scrollable || event.target?.closest?.('.holdings-metrics-scroll-controller')) return;
      const horizontalDelta = Math.abs(event.deltaX) > 0.5
        ? event.deltaX
        : event.shiftKey
          ? event.deltaY
          : 0;
      if (Math.abs(horizontalDelta) <= 0.5) return;

      const maximumScrollLeft = Math.max(0, controller.scrollWidth - controller.clientWidth);
      const nextScrollLeft = Math.min(maximumScrollLeft, Math.max(0, controller.scrollLeft + horizontalDelta));
      if (Math.abs(nextScrollLeft - controller.scrollLeft) <= 0.5) return;

      event.preventDefault();
      controller.scrollLeft = nextScrollLeft;
      scheduleScrollSync();
    };

    measure();
    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    [frameElement, metricsViewport, ...metricsTracks].forEach((element) => {
      resizeObserver?.observe(element);
    });
    controller.addEventListener('scroll', scheduleScrollSync, { passive: true });
    if (enableFrameWheel) {
      frameElement.addEventListener('wheel', handleFrameWheel, { passive: false });
    }
    window.addEventListener('resize', measure, { passive: true });
    window.visualViewport?.addEventListener('resize', measure, { passive: true });

    return () => {
      if (scrollFrame !== null) {
        window.cancelAnimationFrame(scrollFrame);
      }
      resizeObserver?.disconnect();
      controller.removeEventListener('scroll', scheduleScrollSync);
      if (enableFrameWheel) {
        frameElement.removeEventListener('wheel', handleFrameWheel);
      }
      window.removeEventListener('resize', measure);
      window.visualViewport?.removeEventListener('resize', measure);
      frameElement.style.removeProperty('--holdings-metrics-scroll-left');
    };
  }, [enableFrameWheel]);

  return (
    <div
      ref={controllerRef}
      className={`holdings-metrics-scroll-controller is-${placement} ${isVisible ? 'is-visible' : ''}`.trim()}
      style={{ '--holdings-metrics-scroll-content-width': `${contentWidth}px` }}
      aria-hidden="true"
    >
      <div className="holdings-metrics-scroll-controller-inner" />
    </div>
  );
}

const CASH_BALANCE_HORIZONTAL_SCROLL_PROXY_OPTIONS = {
  className: 'holdings-cash-top-scrollbar',
  innerClassName: 'holdings-cash-top-scrollbar-inner',
  contentWidthProperty: '--holdings-cash-scroll-content-width',
  deferTargetScrollSync: true,
  resolveTarget: (controller) => {
    const panel = controller.closest('.holdings-cash-panel');
    const target = panel?.querySelector('.holdings-cash-scroll-surface');
    const table = panel?.querySelector('.holdings-cash-compact-table');
    return target && table ? target : null;
  },
  getContentElements: ({ target }) => [
    target.closest('.holdings-cash-panel')?.querySelector('.holdings-cash-compact-table'),
  ],
  getObservedElements: ({ target, contentElements }) => [
    target.closest('.holdings-cash-panel'),
    target,
    ...contentElements,
  ],
};

function CashBalanceTopScrollController() {
  return <HorizontalScrollProxy options={CASH_BALANCE_HORIZONTAL_SCROLL_PROXY_OPTIONS} />;
}

const INCOME_ACTIVITY_HORIZONTAL_SCROLL_PROXY_OPTIONS = {
  className: 'income-activity-top-scrollbar',
  innerClassName: 'income-activity-top-scrollbar-inner',
  contentWidthProperty: '--income-activity-horizontal-scroll-content-width',
  targetViewportProperty: '--income-activity-horizontal-scroll-viewport-width',
  deferTargetScrollSync: true,
  resolveTarget: (controller) => controller.closest('.income-activity-table-wrap'),
  getContentElements: ({ target }) => [
    target.querySelector('.income-activity-scroll-surface'),
    ...target.querySelectorAll('.income-dividends-table'),
  ],
  getObservedElements: ({ target, contentElements }) => [target, ...contentElements],
};

function IncomeActivityTopScrollController() {
  return <HorizontalScrollProxy options={INCOME_ACTIVITY_HORIZONTAL_SCROLL_PROXY_OPTIONS} />;
}

function formatCountLabel(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

const INCOME_SUBTAB_OPTIONS = [
  { key: 'dividends', label: 'Dividends' },
  { key: 'interest', label: 'Interest' },
];

function getIncomeAccountLabel(tx) {
  const candidates = [
    tx?.account_name,
    tx?.accountName,
    tx?.account_label,
    tx?.accountLabel,
    tx?.account_display_name,
    tx?.accountDisplayName,
  ];
  const label = candidates.find((value) => String(value || '').trim());
  return label ? String(label).trim() : 'Unknown account';
}

function getIncomeInstitutionLabel(tx) {
  const candidates = [
    tx?.institution_name,
    tx?.institutionName,
    tx?.institution,
  ];
  const label = candidates.find((value) => String(value || '').trim());
  return label ? String(label).trim() : '';
}

function formatIncomeLongMonthLabel(monthKey) {
  if (!monthKey) {
    return '';
  }

  const [yearValue, monthValue] = String(monthKey).split('-').map(Number);
  if (!Number.isFinite(yearValue) || !Number.isFinite(monthValue)) {
    return String(monthKey);
  }

  return new Date(yearValue, monthValue - 1, 1).toLocaleDateString('en-US', {
    month: 'long',
    year: 'numeric',
  });
}

function sortIncomeDetailRows(rows) {
  return [...rows]
    .filter((row) => Math.abs(Number(row?.value) || 0) > 0.0001)
    .sort((left, right) => {
      const valueDiff = (Number(right?.value) || 0) - (Number(left?.value) || 0);
      if (Math.abs(valueDiff) > 0.0001) {
        return valueDiff;
      }

      const institutionCompare = String(left?.institution || '').localeCompare(String(right?.institution || ''), undefined, {
        sensitivity: 'base',
        numeric: true,
      });
      if (institutionCompare !== 0) {
        return institutionCompare;
      }

      const accountCompare = String(left?.account || '').localeCompare(String(right?.account || ''), undefined, {
        sensitivity: 'base',
        numeric: true,
      });
      if (accountCompare !== 0) {
        return accountCompare;
      }

      const labelCompare = String(left?.label || '').localeCompare(String(right?.label || ''), undefined, {
        sensitivity: 'base',
        numeric: true,
      });
      if (labelCompare !== 0) {
        return labelCompare;
      }

      return String(left?.meta || '').localeCompare(String(right?.meta || ''), undefined, {
        sensitivity: 'base',
        numeric: true,
      });
    });
}

function formatPerShareAmount(amount, currency) {
  if (!Number.isFinite(Number(amount))) return null;
  const value = Number(amount);
  const decimals = Math.abs(value) < 1 ? 4 : 3;
  return new Intl.NumberFormat('en-CA', {
    style: 'currency',
    currency,
    currencyDisplay: 'narrowSymbol',
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

function decorateDividendRowMeta(rows, primaryCurrency) {
  // Withholding txns carry no share quantity, so borrow it from the paired dividend
  // row (same symbol + account) to render a matching "shares · -tax/sh" sub-line.
  const dividendQtyByPosition = new Map();
  rows.forEach((row) => {
    if (row.tone !== 'positive') return;
    const quantity = Number(row._latestQty);
    if (Number.isFinite(quantity) && quantity > 0) {
      dividendQtyByPosition.set(`${row.label}::${row.institution || ''}::${row.account || ''}`, quantity);
    }
  });

  const buildSecondary = (quantity, perShareValue) => {
    const perShareText = formatPerShareAmount(perShareValue, primaryCurrency);
    const sharesText = `${formatQuantity(quantity)} sh`;
    return [sharesText, perShareText ? `${perShareText}/sh` : null].filter(Boolean).join(' · ');
  };

  return rows.map((row) => {
    if (row.tone === 'positive') {
      const quantity = Number(row._latestQty);
      if (!Number.isFinite(quantity) || quantity <= 0) return row;
      return { ...row, metaSecondary: buildSecondary(quantity, row._perShareSum) };
    }
    if (row.tone === 'withholding') {
      const quantity = dividendQtyByPosition.get(`${row.label}::${row.institution || ''}::${row.account || ''}`);
      if (!Number.isFinite(quantity) || quantity <= 0) return row;
      return { ...row, metaSecondary: buildSecondary(quantity, Number(row.value) / quantity) };
    }
    return row;
  });
}


function normalizeHoldingLabel(value) {
  return String(value || '')
    .replace(/\s+@[^ ]+$/u, '')
    .replace(/\s+/gu, ' ')
    .trim();
}

function formatOptionStrikeToken(strikeValue) {
  const strikeNumber = Number(strikeValue);

  if (!Number.isFinite(strikeNumber)) {
    return String(strikeValue || '').trim();
  }

  return `${strikeNumber}`.replace(/\.0+$/u, '').replace(/(\.\d*?)0+$/u, '$1');
}

const OPTION_MONTH_TOKENS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

function isBusinessDayUtc(date) {
  const dayOfWeek = date.getUTCDay();
  return dayOfWeek !== 0 && dayOfWeek !== 6;
}

function subtractBusinessDaysUtc(date, count) {
  const result = new Date(date);
  let remaining = Number(count) || 0;

  while (remaining > 0) {
    result.setUTCDate(result.getUTCDate() - 1);

    if (isBusinessDayUtc(result)) {
      remaining -= 1;
    }
  }

  return result;
}

function formatOptionExpiryDateToken(date) {
  const day = `${date.getUTCDate()}`.padStart(2, '0');
  const month = OPTION_MONTH_TOKENS[date.getUTCMonth()] || '';
  const year = `${date.getUTCFullYear()}`.slice(-2);

  return `${day}${month}${year}`;
}

function getClMonthlyFopExpiryToken(contractMonthToken, contractYearToken) {
  const monthIndex = OPTION_MONTH_TOKENS.indexOf(String(contractMonthToken || '').toUpperCase());
  const year = 2000 + Number(contractYearToken);

  if (monthIndex < 0 || !Number.isFinite(year)) {
    return null;
  }

  const priorMonthTwentyFifth = new Date(Date.UTC(year, monthIndex - 1, 25));
  const futuresTerminationOffset = isBusinessDayUtc(priorMonthTwentyFifth) ? 3 : 4;
  const futuresTerminationDate = subtractBusinessDaysUtc(priorMonthTwentyFifth, futuresTerminationOffset);
  const optionExpiryDate = subtractBusinessDaysUtc(futuresTerminationDate, 3);

  return formatOptionExpiryDateToken(optionExpiryDate);
}

function getOptionContractDisplayLabel(value) {
  const normalizedValue = normalizeHoldingLabel(value);

  if (!normalizedValue) {
    return null;
  }

  const verboseMatch = normalizedValue.match(
    /^([A-Z0-9.-]+)(?:\s+FOP)?\s+([A-Za-z]{3})(\d{2})'(\d{2})\s+([0-9.]+)\s+(CALL|PUT)(?:\s+\(([^)]*)\))?$/iu
  );

  if (verboseMatch) {
    const [, underlying, month, day, year, strike, side, suffixCode] = verboseMatch;
    const normalizedUnderlying = underlying.toUpperCase();
    const normalizedMonth = month.toUpperCase();
    const normalizedSuffix = String(suffixCode || '').toUpperCase();
    // Monthly CL LO contracts encode the underlying futures month in the verbose symbol,
    // so we derive the actual option expiry instead of displaying the misleading month token.
    const expiryToken = normalizedUnderlying === 'CL' && normalizedSuffix === 'LO'
      ? (getClMonthlyFopExpiryToken(normalizedMonth, year) || `${day}${normalizedMonth}${year}`)
      : `${day}${normalizedMonth}${year}`;

    return `${normalizedUnderlying} ${expiryToken} ${formatOptionStrikeToken(strike)} ${side.toUpperCase().startsWith('C') ? 'C' : 'P'}`;
  }

  const occMatch = normalizedValue.replace(/\s+/gu, '').match(/^([A-Z.-]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/iu);

  if (occMatch) {
    const [, root, year, month, day, side, strikeRaw] = occMatch;
    const monthIndex = Number(month);
    const monthToken = Number.isFinite(monthIndex) && monthIndex >= 1 && monthIndex <= 12
      ? new Date(Date.UTC(2000, monthIndex - 1, 1)).toLocaleString('en-US', { month: 'short', timeZone: 'UTC' }).toUpperCase()
      : month.toUpperCase();
    const strike = formatOptionStrikeToken((Number(strikeRaw) || 0) / 1000);

    return `${root} ${day}${monthToken}${year} ${strike} ${side.toUpperCase()}`;
  }

  const contractStyleMatch = normalizedValue.match(/^([A-Z0-9.-]+)\s+((?:\d{2}[A-Za-z]{3}\d{2})|(?:[A-Za-z]{3}\d{2}))\s+([0-9.]+)\s+([CP])$/iu);

  if (contractStyleMatch) {
    const [, underlying, expiryToken, strike, side] = contractStyleMatch;
    return `${underlying} ${expiryToken.toUpperCase()} ${formatOptionStrikeToken(strike)} ${side.toUpperCase()}`;
  }

  return null;
}

function getHoldingDisplaySymbol(symbol) {
  const optionContractLabel = getOptionContractDisplayLabel(symbol);

  if (optionContractLabel) {
    return optionContractLabel;
  }

  return normalizeHoldingLabel(symbol);
}

function parseOptionContractExpiry(displayLabel) {
  if (!displayLabel) return null;
  const parts = String(displayLabel).trim().split(/\s+/u);
  if (parts.length < 4) return null;
  const [ticker, expiryToken, strike, sideRaw] = parts;
  const tokenMatch = String(expiryToken).match(/^(\d{2})([A-Z]{3})(\d{2})$/u);
  if (!tokenMatch) return null;
  const [, dayPart, monthPart, yearPart] = tokenMatch;
  const monthIndex = OPTION_MONTH_TOKENS.indexOf(monthPart);
  const day = Number(dayPart);
  const year = 2000 + Number(yearPart);
  if (monthIndex < 0 || !Number.isFinite(day) || !Number.isFinite(year)) return null;
  const side = String(sideRaw || '').toUpperCase().startsWith('C') ? 'C' : 'P';
  return {
    ticker,
    monthIndex,
    year,
    day,
    monthKey: `${year}-${String(monthIndex + 1).padStart(2, '0')}`,
    dayKey: `${year}-${String(monthIndex + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`,
    strike,
    side,
  };
}

function getCanonicalHoldingSymbol(symbol) {
  const optionContractLabel = getOptionContractDisplayLabel(symbol);

  if (optionContractLabel) {
    return optionContractLabel;
  }

  return normalizeHoldingLabel(symbol).toUpperCase();
}

function getHoldingDisplayName(name, symbol) {
  const normalizedName = normalizeHoldingLabel(name);
  const normalizedSymbol = getHoldingDisplaySymbol(symbol);

  if (!normalizedName) {
    return null;
  }

  if (normalizedName.localeCompare(normalizedSymbol, undefined, { sensitivity: 'base' }) === 0) {
    return null;
  }

  return normalizedName;
}

function OverflowTooltipText({ className, text }) {
  const textRef = useRef(null);
  const [isTruncated, setIsTruncated] = useState(false);

  useLayoutEffect(() => {
    const element = textRef.current;
    if (!element) return undefined;

    const measure = () => setIsTruncated(element.scrollWidth > element.clientWidth + 0.5);
    measure();

    const observer = new ResizeObserver(measure);
    observer.observe(element);

    return () => observer.disconnect();
  }, [text]);

  return (
    <span
      ref={textRef}
      className={className}
      data-tooltip={isTruncated ? text : undefined}
      aria-label={isTruncated ? text : undefined}
    >
      {text}
    </span>
  );
}

const HOLDINGS_ACCOUNT_TYPES = ['margin', 'tfsa', 'rrsp', 'fhsa', 'resp', 'lira', 'crypto'];

function isInvestmentsAccount(account) {
  if (!account || account.is_liability) {
    return false;
  }
  if (HOLDINGS_ACCOUNT_TYPES.includes(account.account_type)) {
    return true;
  }
  const provider = String(account.provider || '').trim().toLowerCase();
  return account.account_type === 'cash'
    && provider
    && provider !== 'manual'
    && String(account.institution || '').trim().toLowerCase() !== 'cash';
}

function getInvestmentScopeInstitutions(institutions) {
  return (institutions || [])
    .map((institution) => ({
      ...institution,
      accounts: (institution.accounts || []).filter(isInvestmentsAccount),
    }))
    .filter((institution) => institution.accounts.length > 0)
    .map((institution) => ({
      ...institution,
      accountIds: institution.accounts.map((account) => account.id),
    }));
}

const CASH_SYMBOLS = ['CAD', 'USD', 'EUR', 'JPY', 'GBP', 'CHF', 'AUD', 'HKD', 'NZD', 'SEK', 'NOK', 'DKK', 'SGD', 'CNH'];
const CASH_SYMBOL_SET = new Set(CASH_SYMBOLS);
const CRYPTO_WALLET_SYMBOLS = ['BTC', 'ETH', 'SOL', 'USDC'];
const CRYPTO_WALLET_SYMBOL_SET = new Set(CRYPTO_WALLET_SYMBOLS);
const DEFAULT_CAD_FX_RATES = {
  CAD: 1,
  USD: 1.44,
};
const OPTION_TEXT_PATTERN = /\b(CALL|PUT|FOP)\b/i;
const OCC_OPTION_PATTERN = /^[A-Z.-]{1,6}\d{6}[CP]\d{5,8}$/i;
const IBKR_OPTION_SYMBOL_PATTERN = /^[A-Z.-]{1,10}\s+\d{6}[CP]\d{5,8}$/i;
const IBKR_FUTURES_OPTION_SYMBOL_PATTERN = /^[A-Z0-9]{2,6}\s+[CP]\d{3,6}(?:\.\d+)?$/i;
const IBKR_OPTION_NAME_PATTERN = /\b\d{1,2}[A-Z]{3}\d{2}\b.*\b[CP]\b/i;
const DERIVATIVE_TEXT_PATTERN = /\b(WARRANTS?|RIGHTS?|CFDS?|SWAPS?|FORWARDS?)\b/i;
const INTEREST_SYMBOLS = /CREDIT INT|DEBIT INT|BORROW FEES/i;
const INVESTMENTS_TABS = [
  { id: 'overview', label: 'Overview', isEnabled: true },
  { id: 'holdings', label: 'Holdings', isEnabled: true },
  { id: 'options', label: 'Options', isEnabled: true },
  { id: 'crypto', label: 'Crypto', isEnabled: true },
  { id: 'income', label: 'Income', isEnabled: true },
  { id: 'performance', label: 'Performance', isEnabled: true },
];
const GAINERS_LOSERS_MAX_GAINERS = 15;
const GAINERS_LOSERS_MAX_LOSERS = 15;
const TOUR_DEMO_UNREALIZED_GAIN_LOSS_CAD = 19573.28;
const HOLDINGS_TABLE_COLUMNS = [
  { id: 'security', label: 'Security', type: 'text', sortable: true, align: 'left', defaultDirection: 'asc' },
  { id: 'weight', label: 'Weight', type: 'number', sortable: true, align: 'right', defaultDirection: 'desc' },
  { id: 'lastPrice', label: 'Last Price', type: 'number', sortable: true, align: 'right', defaultDirection: 'desc' },
  { id: 'gainLossPct', label: 'Unrealized P&L %', type: 'percent', sortable: true, align: 'right', defaultDirection: 'desc' },
  { id: 'totalCostBasis', label: 'Cost Basis', type: 'number', sortable: true, align: 'right', defaultDirection: 'desc' },
  { id: 'marketValue', label: 'Current Value', type: 'number', sortable: true, align: 'right', defaultDirection: 'desc' },
  { id: 'quantity', label: 'Quantity', type: 'number', sortable: true, align: 'right', defaultDirection: 'desc' },
  { id: 'displayCurrency', label: 'Currency', type: 'text', sortable: true, align: 'right', defaultDirection: 'asc' },
];
const DEFAULT_HOLDINGS_TABLE_SORT = { key: 'marketValue', direction: 'desc' };
const HOLDINGS_TAB_SORT_STORAGE_KEY = 'breaktwenty_holdings_table_sort_v1';
const OPTIONS_TAB_SORT_STORAGE_KEY = 'breaktwenty_options_table_sort_v1';
const CRYPTO_TAB_SORT_STORAGE_KEY = 'breaktwenty_crypto_table_sort_v1';
const HOLDINGS_TABLE_SORT_DEFAULT_DIRECTIONS = Object.fromEntries(
  HOLDINGS_TABLE_COLUMNS
    .filter((column) => column.sortable)
    .map((column) => [column.id, column.defaultDirection || 'asc'])
);
const TOUR_DEMO_TOP_HOLDINGS_ROWS = [
  { id: 'HYLD.TO', symbol: 'HYLD.TO', name: 'Hamilton Enhanced U.S. Covered Call ETF', valueCad: 25910.00, sector: 'ETF', instrumentKind: 'etf', currency: 'CAD', institutionName: 'Questrade', accountName: 'Cash', accountType: 'cash', accountTypeLabel: 'Cash' },
  { id: 'BANK.TO', symbol: 'BANK.TO', name: 'Evolve Canadian Banks and Lifecos Enhanced Yield Index Fund', valueCad: 19000.00, sector: 'Financial Services', instrumentKind: 'etf', currency: 'CAD', institutionName: 'Questrade', accountName: 'Cash', accountType: 'cash', accountTypeLabel: 'Cash' },
  { id: 'RY.TO', symbol: 'RY.TO', name: 'Royal Bank of Canada', valueCad: 18200.00, sector: 'Financial Services', instrumentKind: 'equity', currency: 'CAD', institutionName: 'Wealthsimple', accountName: 'RRSP', accountType: 'rrsp', accountTypeLabel: 'RRSP' },
  { id: 'HHIS.TO', symbol: 'HHIS.TO', name: 'Harvest Diversified High Income Shares ETF', valueCad: 15000.00, sector: 'ETF', instrumentKind: 'etf', currency: 'CAD', institutionName: 'Questrade', accountName: 'Margin', accountType: 'margin', accountTypeLabel: 'Margin' },
  { id: 'VDY.TO', symbol: 'VDY.TO', name: 'Vanguard FTSE Canadian High Dividend Yield ETF', valueCad: 9200.00, sector: 'ETF', instrumentKind: 'etf', currency: 'CAD', institutionName: 'Wealthsimple', accountName: 'TFSA', accountType: 'tfsa', accountTypeLabel: 'TFSA' },
  { id: 'AAPL', symbol: 'AAPL', name: 'Apple Inc.', valueCad: 7000.00, sector: 'Technology', instrumentKind: 'equity', currency: 'USD', institutionName: 'Questrade', accountName: 'Margin', accountType: 'margin', accountTypeLabel: 'Margin' },
  { id: 'XEQT.TO', symbol: 'XEQT.TO', name: 'iShares Core Equity ETF Portfolio', valueCad: 7000.00, sector: 'ETF', instrumentKind: 'etf', currency: 'CAD', institutionName: 'Wealthsimple', accountName: 'TFSA', accountType: 'tfsa', accountTypeLabel: 'TFSA' },
  { id: 'BNS.TO', symbol: 'BNS.TO', name: 'Bank of Nova Scotia', valueCad: 5210.75, sector: 'Financial Services', instrumentKind: 'equity', currency: 'CAD', institutionName: 'Wealthsimple', accountName: 'TFSA', accountType: 'tfsa', accountTypeLabel: 'TFSA' },
  { id: 'VFV.TO', symbol: 'VFV.TO', name: 'Vanguard S&P 500 Index ETF', valueCad: 3740.20, sector: 'ETF', instrumentKind: 'etf', currency: 'CAD', institutionName: 'Questrade', accountName: 'Margin', accountType: 'margin', accountTypeLabel: 'Margin' },
  { id: 'NVDA', symbol: 'NVDA', name: 'NVIDIA Corp.', valueCad: 2800.00, sector: 'Technology', instrumentKind: 'equity', currency: 'USD', institutionName: 'Wealthsimple', accountName: 'TFSA', accountType: 'tfsa', accountTypeLabel: 'TFSA' },
];
const TOUR_DEMO_GAINERS_LOSERS_ROWS = [
  { id: 'HHIS.TO', symbol: 'HHIS.TO', name: 'Harvest Diversified High Income Shares ETF', gainLossPct: 50.3, gainLossValue: 5017.24, marketValue: 15000.00 },
  { id: 'HYLD.TO', symbol: 'HYLD.TO', name: 'Hamilton Enhanced U.S. Covered Call ETF', gainLossPct: 42.5, gainLossValue: 7730.15, marketValue: 25910.00 },
  { id: 'RY.TO', symbol: 'RY.TO', name: 'Royal Bank of Canada', gainLossPct: 34.8, gainLossValue: 4698.45, marketValue: 18200.00 },
  { id: 'VDY.TO', symbol: 'VDY.TO', name: 'Vanguard FTSE Canadian High Dividend Yield ETF', gainLossPct: 32.7, gainLossValue: 2268.48, marketValue: 9200.00 },
  { id: 'BNS.TO', symbol: 'BNS.TO', name: 'Bank of Nova Scotia', gainLossPct: 28.9, gainLossValue: 1168.62, marketValue: 5210.75 },
  { id: 'BANK.TO', symbol: 'BANK.TO', name: 'Evolve Canadian Banks and Lifecos Enhanced Yield Index Fund', gainLossPct: 23.6, gainLossValue: 3594.57, marketValue: 19000.00 },
  { id: 'VFV.TO', symbol: 'VFV.TO', name: 'Vanguard S&P 500 Index ETF', gainLossPct: 18.4, gainLossValue: 580.97, marketValue: 3740.20 },
  { id: 'NVDA', symbol: 'NVDA', name: 'NVIDIA Corp.', gainLossPct: 2.5, gainLossValue: 68.29, marketValue: 2800.00 },
  { id: 'AAPL', symbol: 'AAPL', name: 'Apple Inc.', gainLossPct: -1.2, gainLossValue: -85.02, marketValue: 7000.00 },
  { id: 'XEQT.TO', symbol: 'XEQT.TO', name: 'iShares Core Equity ETF Portfolio', gainLossPct: -4.8, gainLossValue: -352.94, marketValue: 7000.00 },
];
function buildTourDemoHoldingChartDataset() {
  return TOUR_DEMO_TOP_HOLDINGS_ROWS.map((holding) => ({
    ...holding,
    symbolKey: getHoldingSymbolKey(holding.symbol),
    canonicalSymbol: getCanonicalHoldingSymbol(holding.symbol),
    displaySymbol: holding.symbol,
    displayName: holding.name,
    displayCurrency: holding.currency,
    marketValueCad: holding.valueCad,
    accountTypeLabels: [holding.accountTypeLabel],
    institutionNames: [holding.institutionName],
    accountNames: [holding.accountName],
    isEtfLike: holding.sector === 'ETF' || hasBackendEtfOrFundInstrumentKind(holding.instrumentKind),
  }));
}
const HOLDINGS_PIE_OTHER_THRESHOLD_PCT = 1;
const ETF_LIKE_NAME_MARKERS = [
  ' ETF',
  'ETF ',
  ' ETF-',
  'INDEX ETF',
  'INDEX FUND',
  'INDEX FD',
  'MUTUAL FUND',
  'EXCHANGE TRADED FUND',
  ' ULTRAYLD',
  ' ENH YLD',
  ' YLD IDX',
];
const FUND_LIKE_NAME_MARKERS = [
  ' INCOME SHARES',
  ' ETF TRUST',
  ' IDX FD',
  ' HIGH INCOME',
  ' HIGH INC ',
  ' COVERED CALL',
];
const HOLDINGS_COMPOSITION_GROUP_MODES = [
  { key: 'sector', label: 'Sector' },
  { key: 'currency', label: 'Currency' },
  { key: 'accountType', label: 'Account Type' },
  { key: 'institution', label: 'Institution' },
  { key: 'account', label: 'Account' },
];

function getCompositionGroupModeOption(key) {
  return HOLDINGS_COMPOSITION_GROUP_MODES.find((option) => option.key === key) || HOLDINGS_COMPOSITION_GROUP_MODES[0];
}

function getGainersLosersAxisConfig(rows, isLossChart, showByValue) {
  const values = Array.isArray(rows)
    ? rows
      .map((row) => Number(row?.metricValue))
      .filter((value) => Number.isFinite(value))
    : [];

  if (values.length === 0) {
    return isLossChart
      ? { axisMin: showByValue ? -1 : -100, axisMax: 0, ticks: showByValue ? [-1, 0] : [-100, -75, -50, -25, 0] }
      : { axisMin: 0, axisMax: showByValue ? 1 : 100, ticks: showByValue ? [0, 1] : [0, 25, 50, 75, 100] };
  }

  const maxMagnitude = values.reduce((highest, value) => Math.max(highest, Math.abs(value)), 0);
  const paddedMagnitude = maxMagnitude * (isLossChart ? 1.14 : 1.08);
  const step = getIncomeHistoryAxisStep(paddedMagnitude || 1, isLossChart ? 5 : 4);
  const axisMagnitude = Math.max(step, Math.ceil(paddedMagnitude / step) * step);

  if (isLossChart) {
    const axisMin = -axisMagnitude;
    const ticks = [];
    for (let tickValue = axisMin; tickValue <= 0 + (step / 2); tickValue += step) {
      ticks.push(Number(tickValue.toFixed(10)));
    }
    return { axisMin, axisMax: 0, ticks };
  }

  const ticks = [];
  for (let tickValue = 0; tickValue <= axisMagnitude + (step / 2); tickValue += step) {
    ticks.push(Number(tickValue.toFixed(10)));
  }
  return { axisMin: 0, axisMax: axisMagnitude, ticks };
}

function getInstitutionKey(account) {
  if (account.institution_id !== undefined && account.institution_id !== null) {
    return `institution-${account.institution_id}`;
  }

  return `institution-${account.institution}`;
}

function getHoldingSymbolKey(symbol) {
  return String(symbol || 'unknown').trim().toUpperCase();
}

function getHoldingSymbolFamilyKey(symbol) {
  const normalizedSymbol = getHoldingLookupSymbol(symbol || 'unknown');
  const suffixes = ['.TO', '.UN', '.U', '.V', '.CN', '.NE'];
  for (const suffix of suffixes) {
    if (normalizedSymbol.endsWith(suffix)) {
      return normalizedSymbol.slice(0, -suffix.length).trim() || normalizedSymbol;
    }
  }
  return normalizedSymbol;
}

function getHoldingCurrency(holding, accountCurrency = 'CAD') {
  return String(holding.currency || accountCurrency || 'CAD').trim().toUpperCase();
}

function truncatePieSliceLabel(label, maxLength = 18) {
  const text = String(label || '').trim();
  if (!text) {
    return '';
  }
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1).trimEnd()}…`;
}

function mergeAccountBreakdownRows(rows) {
  const grouped = new Map();

  rows.forEach((account) => {
    if (!account?.label) {
      return;
    }

    const key = account.id || account.label;
    const existing = grouped.get(key) || {
      id: key,
      label: account.label,
      quantity: 0,
    };
    existing.quantity += Number(account.quantity) || 0;
    grouped.set(key, existing);
  });

  return Array.from(grouped.values()).sort((left, right) => (
    left.label.localeCompare(right.label, undefined, { sensitivity: 'base', numeric: true })
  ));
}

function mergePieRollupBreakdown(slices) {
  const grouped = new Map();

  slices.forEach((slice) => {
    const sourceItems = Array.isArray(slice.rollupBreakdown) && slice.rollupBreakdown.length > 0
      ? slice.rollupBreakdown
      : [{
        label: slice.tooltipLabel || slice.label || slice.symbol || 'Unknown',
        value: slice.value,
        sharePct: slice.sharePct,
        accountBreakdown: slice.accountBreakdown,
      }];

    sourceItems.forEach((item) => {
      if (!item || !item.label) {
        return;
      }

      const existing = grouped.get(item.label) || {
        label: item.label,
        value: 0,
        sharePct: 0,
        accountBreakdown: [],
      };
      existing.value += Number(item.value) || 0;
      existing.sharePct += Number(item.sharePct) || 0;
      existing.accountBreakdown = mergeAccountBreakdownRows([
        ...existing.accountBreakdown,
        ...(Array.isArray(item.accountBreakdown) ? item.accountBreakdown : []),
      ]);
      grouped.set(item.label, existing);
    });
  });

  return Array.from(grouped.values()).sort((left, right) => {
    if (right.sharePct !== left.sharePct) {
      return right.sharePct - left.sharePct;
    }
    return left.label.localeCompare(right.label, undefined, { sensitivity: 'base', numeric: true });
  });
}

function mergePieAccountBreakdown(slices) {
  return mergeAccountBreakdownRows(slices.flatMap((slice) => (
    Array.isArray(slice.accountBreakdown) ? slice.accountBreakdown : []
  )));
}

function rollupSmallPieSlices(slices, otherFill, thresholdPct = HOLDINGS_PIE_OTHER_THRESHOLD_PCT) {
  if (!Array.isArray(slices) || slices.length === 0) {
    return [];
  }

  const keptSlices = [];
  const smallSlices = [];

  slices.forEach((slice) => {
    if (Number(slice.sharePct) < thresholdPct) {
      smallSlices.push(slice);
      return;
    }
    keptSlices.push(slice);
  });

  if (smallSlices.length < 2) {
    return slices;
  }

  const otherValue = smallSlices.reduce((sum, slice) => sum + (Number(slice.value) || 0), 0);
  const otherSharePct = smallSlices.reduce((sum, slice) => sum + (Number(slice.sharePct) || 0), 0);
  const tooltipBreakdown = mergePieRollupBreakdown(smallSlices);
  const accountBreakdown = mergePieAccountBreakdown(smallSlices);

  return [
    ...keptSlices,
    {
      id: `other-${smallSlices.length}`,
      label: 'Other',
      symbol: 'Other',
      shortLabel: 'Other',
      value: otherValue,
      sharePct: otherSharePct,
      tooltipLabel: 'Other',
      tooltipSubLabel: `${smallSlices.length} slices under ${thresholdPct.toFixed(0)}%`,
      tooltipBreakdownLabel: 'Tickers',
      tooltipBreakdown,
      tooltipTickers: [],
      accountBreakdown,
      fill: otherFill,
      rollupBreakdown: tooltipBreakdown,
    },
  ];
}

function toTitleCaseLabel(label) {
  return String(label || '')
    .toLowerCase()
    .replace(/[a-z]+/g, (word) => word.charAt(0).toUpperCase() + word.slice(1));
}

function getHoldingLookupSymbol(symbol) {
  return getCanonicalHoldingSymbol(symbol);
}

function looksLikeEtfOrFund(symbol, name) {
  const normalizedSymbol = getHoldingLookupSymbol(symbol);
  const normalizedName = String(name || '').trim().toUpperCase();

  if (ETF_LIKE_NAME_MARKERS.some((marker) => normalizedName.includes(marker))) {
    return true;
  }
  if (FUND_LIKE_NAME_MARKERS.some((marker) => normalizedName.includes(marker))) {
    return true;
  }
  if (normalizedSymbol.endsWith('.U') && normalizedName.includes('SHARES')) {
    return true;
  }
  return false;
}

function hasBackendEtfOrFundInstrumentKind(instrumentKind) {
  const normalizedKind = String(instrumentKind || '').trim().toLowerCase();
  return normalizedKind === 'etf' || normalizedKind === 'fund';
}

function getHoldingCompositionGroupLabel(holding, groupMode) {
  switch (groupMode) {
    case 'currency':
      return String(
        holding.displayCurrency
        || (Array.isArray(holding.sourceCurrencies) && holding.sourceCurrencies.length === 1 ? holding.sourceCurrencies[0] : '')
        || getHoldingCurrency(holding, holding.accountCurrency)
      ).trim() || 'Unknown currency';
    case 'accountType':
      if (String(holding.accountTypeLabel || '').trim()) {
        return String(holding.accountTypeLabel).trim();
      }
      if (Array.isArray(holding.accountTypeLabels) && holding.accountTypeLabels.length > 0) {
        return String(holding.accountTypeLabels[0] || '').trim() || 'Other';
      }
      return String(holding.accountTypeLabel || ACCOUNT_TYPE_LABELS[holding.accountType] || 'Other').trim() || 'Other';
    case 'institution':
      if (String(holding.institutionName || '').trim()) {
        return String(holding.institutionName).trim();
      }
      if (Array.isArray(holding.institutionNames) && holding.institutionNames.length > 0) {
        return String(holding.institutionNames[0] || '').trim() || 'Unknown institution';
      }
      return String(holding.institutionName || 'Unknown institution').trim() || 'Unknown institution';
    case 'account':
      if (String(holding.accountName || '').trim()) {
        return String(holding.accountName).trim();
      }
      if (Array.isArray(holding.accountNames) && holding.accountNames.length > 0) {
        return String(holding.accountNames[0] || '').trim() || 'Unknown account';
      }
      return String(holding.accountName || 'Unknown account').trim() || 'Unknown account';
    case 'sector':
    default:
      if (String(holding.sector || '').trim()) {
        const sectorLabel = String(holding.sector).trim();
        return sectorLabel.toUpperCase() === 'ETF' ? 'ETF' : toTitleCaseLabel(sectorLabel);
      }
      if (hasBackendEtfOrFundInstrumentKind(holding.instrumentKind)) {
        return 'ETF';
      }
      if (holding.isEtfLike) {
        return 'ETF';
      }
      return 'Unclassified';
  }
}

function getHoldingCostBasis(holding) {
  return Number.isFinite(holding.average_cost) ? holding.average_cost : null;
}

function getHoldingLastPrice(holding) {
  return Number.isFinite(holding.last_price) ? Number(holding.last_price) : null;
}

function getHoldingChangePct(holding) {
  return Number.isFinite(holding.change_pct) ? Number(holding.change_pct) : null;
}

function getHoldingDailyPnl(holding) {
  return Number.isFinite(holding.daily_pnl) ? Number(holding.daily_pnl) : null;
}

function getHoldingContractMultiplier(holding, holdingCategory) {
  const sourceMultiplier = Number(holding.contract_multiplier);

  if (Number.isFinite(sourceMultiplier) && sourceMultiplier > 0) {
    return sourceMultiplier;
  }

  if (holdingCategory !== 'option') {
    return 1;
  }

  const optionDescriptor = `${holding.symbol || ''} ${holding.name || ''}`.toUpperCase();

  if (/\bFOP\b/u.test(optionDescriptor)) {
    return 1000;
  }

  return 100;
}

function getHoldingAverageQuotePrice(totalQuotedCostBasis, quantity) {
  const normalizedQuantity = Math.abs(Number(quantity) || 0);

  if (totalQuotedCostBasis === null || normalizedQuantity === 0) {
    return null;
  }

  return totalQuotedCostBasis / normalizedQuantity;
}

function getHoldingMoneyCostBasis(totalQuotedCostBasis, holdingCategory, contractMultiplier, quantity) {
  if (totalQuotedCostBasis === null) {
    return null;
  }

  const absoluteMoneyCostBasis = holdingCategory === 'option'
    ? totalQuotedCostBasis * contractMultiplier
    : totalQuotedCostBasis;
  const positionSign = Number(quantity) < 0 ? -1 : 1;

  return absoluteMoneyCostBasis * positionSign;
}

function deriveHoldingQuoteLastPrice(holding, marketValue, quantity, contractMultiplier, holdingCategory) {
  const sourceLastPrice = getHoldingLastPrice(holding);

  if (sourceLastPrice !== null) {
    return sourceLastPrice;
  }

  const normalizedQuantity = Math.abs(Number(quantity) || 0);

  if (normalizedQuantity === 0 || !Number.isFinite(Number(marketValue))) {
    return null;
  }

  const denominator = holdingCategory === 'option'
    ? normalizedQuantity * contractMultiplier
    : normalizedQuantity;

  if (!Number.isFinite(denominator) || denominator <= 0) {
    return null;
  }

  return Math.abs(Number(marketValue)) / denominator;
}

function isValidFxRateToCad(rate) {
  return Number.isFinite(rate) && rate > 0.5 && rate < 10;
}

function getFxRateToCad(currency, ratesToCad) {
  const normalizedCurrency = String(currency || 'CAD').trim().toUpperCase();

  return ratesToCad[normalizedCurrency] ?? DEFAULT_CAD_FX_RATES[normalizedCurrency] ?? 1;
}

function isCashHoldingSymbol(symbol) {
  return CASH_SYMBOL_SET.has(String(symbol || '').trim().toUpperCase());
}

function isOptionHolding(holding) {
  const symbol = String(holding.symbol || '').trim();
  const name = String(holding.name || '').trim();
  const optionDescriptor = `${symbol} ${name}`.replace(/\s+/g, ' ').trim();
  const collapsedSymbol = symbol.replace(/\s+/g, ' ').trim();

  if (getOptionContractDisplayLabel(symbol) || getOptionContractDisplayLabel(name)) {
    return true;
  }

  return (
    OPTION_TEXT_PATTERN.test(optionDescriptor) ||
    OCC_OPTION_PATTERN.test(symbol.replace(/\s+/g, '')) ||
    IBKR_OPTION_SYMBOL_PATTERN.test(collapsedSymbol) ||
    IBKR_FUTURES_OPTION_SYMBOL_PATTERN.test(collapsedSymbol) ||
    IBKR_OPTION_NAME_PATTERN.test(name)
  );
}

function isDerivativeHolding(holding) {
  if (isOptionHolding(holding)) {
    return true;
  }

  const descriptor = `${getHoldingLookupSymbol(holding.symbol)} ${normalizeHoldingLabel(holding.name || holding.symbol)}`.trim();
  return DERIVATIVE_TEXT_PATTERN.test(descriptor);
}

function isFutureHolding(holding) {
  // A futures contract carries a contract multiplier > 1 but is not an option (no
  // strike/CALL-PUT) or cash. The connector reports BOTH its market_value and average_cost
  // as signed notionals, so it must be valued by mark-to-market P&L (market − entry), not as
  // a spot holding whose notional would (wrongly) count as its worth.
  if (isOptionHolding(holding) || isCashHoldingSymbol(holding.symbol)) {
    return false;
  }
  const multiplier = Number(holding.contract_multiplier);
  return Number.isFinite(multiplier) && multiplier > 1;
}

function isCryptoWalletHolding(holding) {
  if (holding.isCrypto) {
    return true;
  }

  const lookupSymbol = getHoldingLookupSymbol(holding.symbol);
  const descriptor = `${lookupSymbol} ${normalizeHoldingLabel(holding.name || holding.symbol)}`.toUpperCase();

  return CRYPTO_WALLET_SYMBOL_SET.has(lookupSymbol) || /\bWALLET\b/u.test(descriptor);
}

function isEligibleInvestedPositionHolding(holding) {
  // Any non-zero spot position belongs on the Holdings table — long OR short — so futures
  // and other short contracts appear there (and the Holdings total reconciles with the
  // Overview's all-positions total) instead of being dropped by a long-only gate.
  const quantity = Number(holding.quantity);
  return (
    holding.holdingCategory === 'spot' &&
    !holding.isCash &&
    !isCryptoWalletHolding(holding) &&
    !isDerivativeHolding(holding) &&
    Number.isFinite(quantity) && quantity !== 0
  );
}

function getHoldingCategory(holding) {
  if (isCashHoldingSymbol(holding.symbol)) {
    return 'cash';
  }

  if (isOptionHolding(holding)) {
    return 'option';
  }

  return 'spot';
}

function deriveCurrencyRatesToCad(accounts, holdingsByAccount) {
  const derivedRates = new Map();

  accounts.forEach((account) => {
    const accountHoldings = holdingsByAccount[account.id] || [];

    if (accountHoldings.length === 0) {
      return;
    }

    const totalsByCurrency = accountHoldings.reduce((totals, holding) => {
      const currency = getHoldingCurrency(holding, account.currency);
      const marketValue = Number(holding.market_value) || 0;

      return {
        ...totals,
        [currency]: (totals[currency] || 0) + marketValue,
      };
    }, {});

    const cadNativeTotal = totalsByCurrency.CAD || 0;
    const foreignEntries = Object.entries(totalsByCurrency).filter(
      ([currency, total]) => currency !== 'CAD' && Math.abs(total) > 0.01
    );

    if (foreignEntries.length !== 1) {
      return;
    }

    const [currency, foreignNativeTotal] = foreignEntries[0];
    const accountBalanceCad = Number(account.balance) || 0;
    const impliedRate = (accountBalanceCad - cadNativeTotal) / foreignNativeTotal;

    if (!isValidFxRateToCad(impliedRate)) {
      return;
    }

    const current = derivedRates.get(currency) || { weightedRateSum: 0, weight: 0 };
    const weight = Math.abs(foreignNativeTotal);

    current.weightedRateSum += impliedRate * weight;
    current.weight += weight;
    derivedRates.set(currency, current);
  });

  const ratesToCad = { ...DEFAULT_CAD_FX_RATES };

  derivedRates.forEach((entry, currency) => {
    if (entry.weight > 0) {
      ratesToCad[currency] = entry.weightedRateSum / entry.weight;
    }
  });

  return ratesToCad;
}

function getHoldingsTableSortValue(holding, columnId) {
  switch (columnId) {
    case 'security':
      return holding.displaySymbol || holding.symbol || '';
    case 'accountSummary':
      return holding.accountSummaryLabel || holding.primaryAccountName || '';
    case 'accountTypeSummary':
      return holding.accountSummaryMeta || holding.accountTypeLabels?.join(' + ') || '';
    case 'weight':
      return holding.marketValueCad ?? holding.marketValue ?? null;
    case 'averageCostPerUnit':
      return holding.averageCostPerUnitCad ?? holding.averageCostPerUnit ?? null;
    case 'lastPrice':
      return holding.lastPrice;
    case 'gainLossPct':
      return holding.gainLossPct;
    case 'gainLoss':
      return holding.gainLossCad ?? holding.gainLoss ?? null;
    case 'quantity':
      return holding.quantity;
    case 'totalCostBasis':
      return holding.totalCostBasisCad ?? holding.totalCostBasis ?? null;
    case 'marketValue':
      return holding.marketValueCad ?? holding.marketValue ?? null;
    case 'displayCurrency':
      return holding.displayCurrency;
    default:
      return null;
  }
}

function compareHoldingsTableRows(leftHolding, rightHolding, sortConfig) {
  const column = HOLDINGS_TABLE_COLUMNS.find((candidate) => candidate.id === sortConfig?.key);

  if (!column || !column.sortable) {
    return 0;
  }

  const leftValue = getHoldingsTableSortValue(leftHolding, column.id);
  const rightValue = getHoldingsTableSortValue(rightHolding, column.id);
  const leftMissing = leftValue === null || leftValue === undefined || Number.isNaN(leftValue);
  const rightMissing = rightValue === null || rightValue === undefined || Number.isNaN(rightValue);

  if (leftMissing && rightMissing) {
    return 0;
  }

  if (leftMissing) {
    return 1;
  }

  if (rightMissing) {
    return -1;
  }

  let comparison = 0;

  if (column.type === 'text') {
    comparison = String(leftValue).localeCompare(String(rightValue), undefined, {
      sensitivity: 'base',
      numeric: true,
    });
  } else {
    comparison = Number(leftValue) - Number(rightValue);
  }

  if (comparison === 0) {
    return 0;
  }

  return sortConfig.direction === 'asc' ? comparison : -comparison;
}

function combineHoldingsBySymbol(holdings) {
  const grouped = new Map();

  holdings.forEach((holding) => {
    const symbolKey = getHoldingSymbolKey(holding.symbol);

    if (!grouped.has(symbolKey)) {
      grouped.set(symbolKey, {
        symbolKey,
        symbol: holding.symbol,
        canonicalSymbol: holding.canonicalSymbol || getCanonicalHoldingSymbol(holding.symbol),
        name: holding.name || holding.symbol,
        displayCurrency: holding.currency,
        quantity: 0,
        marketValue: 0,
        marketValueCad: 0,
        equityValueCad: 0,
        totalCostBasis: 0,
        totalCostBasisCad: 0,
        hasKnownCostBasis: false,
        quoteCostWeightedSum: 0,
        quoteCostWeight: 0,
        quoteLastWeightedSum: 0,
        quoteLastWeight: 0,
        changePctWeightedSum: 0,
        changePctWeight: 0,
        hasKnownDailyPnl: false,
        holdingCategory: holding.holdingCategory,
        accountIds: new Set(),
        accountNames: new Set(),
        accountTypeLabels: new Set(),
        institutionKeys: new Set(),
        institutionNames: new Set(),
        sectors: new Set(),
        sourceCurrencies: new Set(),
        nativeMarketValueByCurrency: new Map(),
        nativeCostBasisByCurrency: new Map(),
        nativeDailyPnlByCurrency: new Map(),
        isEtfLike: false,
      });
    }

    const combined = grouped.get(symbolKey);

    combined.symbol = combined.symbol || holding.symbol;

    if ((!combined.name || combined.name === combined.symbol) && holding.name) {
      combined.name = holding.name;
    }

    combined.quantity += holding.quantity;
    combined.marketValue += holding.marketValue;
    combined.marketValueCad += holding.marketValueCad;
    combined.equityValueCad += holding.equityValueCad;
    combined.accountIds.add(holding.accountId);
    combined.accountNames.add(holding.accountName);
    combined.accountTypeLabels.add(holding.accountTypeLabel);
    combined.institutionKeys.add(holding.institutionKey);
    combined.institutionNames.add(holding.institutionName);
    if (String(holding.sector || '').trim()) {
      combined.sectors.add(toTitleCaseLabel(String(holding.sector).trim()));
    }
    if (holding.isEtfLike) {
      combined.isEtfLike = true;
    }
    combined.sourceCurrencies.add(holding.currency);
    combined.nativeMarketValueByCurrency.set(
      holding.currency,
      (combined.nativeMarketValueByCurrency.get(holding.currency) || 0) + holding.marketValue
    );

    const normalizedQuantity = Math.abs(holding.quantity);

    if (holding.averageCostPerUnitQuote !== null && normalizedQuantity > 0) {
      combined.quoteCostWeightedSum += holding.averageCostPerUnitQuote * normalizedQuantity;
      combined.quoteCostWeight += normalizedQuantity;
    }

    if (holding.lastPriceQuote !== null && normalizedQuantity > 0) {
      combined.quoteLastWeightedSum += holding.lastPriceQuote * normalizedQuantity;
      combined.quoteLastWeight += normalizedQuantity;
    }

    if (holding.changePct !== null && Math.abs(holding.marketValue) > 0) {
      const changeWeight = Math.abs(holding.marketValue);
      combined.changePctWeightedSum += holding.changePct * changeWeight;
      combined.changePctWeight += changeWeight;
    }

    if (combined.displayCurrency !== holding.currency) {
      combined.displayCurrency = 'Mixed';
    }

    if (holding.totalCostBasis !== null) {
      combined.totalCostBasis += holding.totalCostBasis;
      combined.nativeCostBasisByCurrency.set(
        holding.currency,
        (combined.nativeCostBasisByCurrency.get(holding.currency) || 0) + holding.totalCostBasis
      );
    }

    if (holding.totalCostBasisCad !== null) {
      combined.totalCostBasisCad += holding.totalCostBasisCad;
      combined.hasKnownCostBasis = true;
    }

    if (holding.dailyPnl !== null) {
      combined.nativeDailyPnlByCurrency.set(
        holding.currency,
        (combined.nativeDailyPnlByCurrency.get(holding.currency) || 0) + holding.dailyPnl
      );
      combined.hasKnownDailyPnl = true;
    }
  });

  return Array.from(grouped.values())
    .map((combined) => {
      const sourceCurrencies = Array.from(combined.sourceCurrencies).sort();
      const accountNames = Array.from(combined.accountNames).sort((left, right) => left.localeCompare(right, undefined, {
        sensitivity: 'base',
        numeric: true,
      }));
      const accountTypeLabels = Array.from(combined.accountTypeLabels).sort((left, right) => left.localeCompare(right, undefined, {
        sensitivity: 'base',
        numeric: true,
      }));
      const institutionNames = Array.from(combined.institutionNames).sort((left, right) => left.localeCompare(right, undefined, {
        sensitivity: 'base',
        numeric: true,
      }));
      const sectors = Array.from(combined.sectors).sort((left, right) => left.localeCompare(right, undefined, {
        sensitivity: 'base',
        numeric: true,
      }));
      const hasSingleCurrency = sourceCurrencies.length === 1;
      const nativeCurrency = hasSingleCurrency ? sourceCurrencies[0] : 'Mixed';
      const marketValue = hasSingleCurrency
        ? combined.nativeMarketValueByCurrency.get(nativeCurrency) || 0
        : null;
      const totalCostBasis = hasSingleCurrency && combined.hasKnownCostBasis
        ? combined.nativeCostBasisByCurrency.get(nativeCurrency) ?? null
        : null;
      const dailyPnl = hasSingleCurrency && combined.hasKnownDailyPnl
        ? combined.nativeDailyPnlByCurrency.get(nativeCurrency) ?? null
        : null;
      const gainLoss = totalCostBasis !== null && marketValue !== null
        ? marketValue - totalCostBasis
        : null;
      const averageCostPerUnit = combined.quoteCostWeight > 0
        ? combined.quoteCostWeightedSum / combined.quoteCostWeight
        : null;
      const lastPrice = combined.quoteLastWeight > 0
        ? combined.quoteLastWeightedSum / combined.quoteLastWeight
        : null;
      const totalCostBasisCad = combined.hasKnownCostBasis ? combined.totalCostBasisCad : null;
      const gainLossCad = totalCostBasisCad !== null ? combined.marketValueCad - totalCostBasisCad : null;
      const gainLossPct = totalCostBasisCad !== null && Math.abs(totalCostBasisCad) > 0
        ? ((gainLossCad / Math.abs(totalCostBasisCad)) * 100)
        : null;
      const normalizedQuantity = combined.quantity !== 0 ? Math.abs(combined.quantity) : 0;
      const averageCostPerUnitCad = totalCostBasisCad !== null && normalizedQuantity !== 0
        ? totalCostBasisCad / normalizedQuantity
        : null;
      const changePct = combined.changePctWeight > 0
        ? combined.changePctWeightedSum / combined.changePctWeight
        : null;
      const accountCount = combined.accountIds.size;
      const institutionCount = combined.institutionKeys.size;
      const primaryAccountName = accountNames[0] || '';
      const primaryInstitutionName = institutionNames[0] || '';
      const displaySymbol = getHoldingDisplaySymbol(combined.symbol);
      const accountSummaryLabel = accountCount <= 1
        ? primaryAccountName
        : `${accountCount} Accounts`;
      const accountSummaryMeta = accountCount <= 1
        ? (accountTypeLabels[0] || null)
        : null;
      const displayName = getHoldingDisplayName(combined.name, displaySymbol);

      return {
        symbolKey: combined.symbolKey,
        symbol: combined.symbol,
        canonicalSymbol: combined.canonicalSymbol || getCanonicalHoldingSymbol(combined.symbol),
        displaySymbol,
        name: combined.name || combined.symbol,
        displayName,
        displayCurrency: nativeCurrency,
        primaryInstitutionName,
        primaryAccountName,
        accountSummaryLabel,
        accountSummaryMeta,
        changePct,
        dailyPnl,
        quantity: combined.quantity,
        lastPrice,
        marketValue,
        marketValueCad: combined.marketValueCad,
        equityValueCad: combined.equityValueCad,
        totalCostBasis,
        totalCostBasisCad,
        averageCostPerUnit,
        averageCostPerUnitCad,
        gainLoss,
        gainLossCad,
        gainLossPct,
        holdingCategory: combined.holdingCategory,
        isCash: combined.holdingCategory === 'cash',
        isOption: combined.holdingCategory === 'option',
        accountCount,
        institutionCount,
        accountNames,
        accountTypeLabels,
        institutionNames,
        sector: sectors.length === 1 ? sectors[0] : (sectors[0] || null),
        sectors,
        sourceCurrencies,
        isEtfLike: combined.isEtfLike,
      };
    })
    .sort((left, right) => right.marketValueCad - left.marketValueCad);
}

function buildChildPositionsBySymbol(holdings) {
  const grouped = new Map();

  holdings.forEach((holding) => {
    const symbolKey = getHoldingSymbolKey(holding.symbol);
    const gainLoss = holding.totalCostBasis !== null
      ? holding.marketValue - holding.totalCostBasis
      : null;
    const gainLossPct = holding.totalCostBasis !== null && Math.abs(holding.totalCostBasis) > 0
      ? ((gainLoss / Math.abs(holding.totalCostBasis)) * 100)
      : null;

    if (!grouped.has(symbolKey)) {
      grouped.set(symbolKey, []);
    }

    grouped.get(symbolKey).push({
      id: holding.id ?? `${holding.accountId}-${symbolKey}-${holding.currency}-${holding.quantity}-${holding.marketValue}`,
      institutionName: holding.institutionName,
      accountName: holding.accountName,
      accountType: holding.accountType,
      accountTypeLabel: holding.accountTypeLabel,
      symbol: holding.symbol,
      displaySymbol: getHoldingDisplaySymbol(holding.symbol),
      name: holding.name || holding.symbol,
      displayName: getHoldingDisplayName(holding.name, holding.symbol),
      quantity: holding.quantity,
      averageCostPerUnit: holding.averageCostPerUnitQuote,
      lastPrice: holding.lastPriceQuote,
      changePct: holding.changePct,
      dailyPnl: holding.dailyPnl,
      gainLossPct,
      totalCostBasis: holding.totalCostBasis,
      marketValue: holding.marketValue,
      marketValueCad: holding.marketValueCad,
      gainLoss,
      currency: holding.currency,
    });
  });

  grouped.forEach((positions, symbolKey) => {
    positions.sort((left, right) => {
      const institutionComparison = left.institutionName.localeCompare(right.institutionName, undefined, {
        sensitivity: 'base',
        numeric: true,
      });

      if (institutionComparison !== 0) {
        return institutionComparison;
      }

      const accountComparison = left.accountName.localeCompare(right.accountName, undefined, {
        sensitivity: 'base',
        numeric: true,
      });

      if (accountComparison !== 0) {
        return accountComparison;
      }

      return Math.abs(right.marketValue) - Math.abs(left.marketValue);
    });

    grouped.set(symbolKey, positions);
  });

  return grouped;
}

function buildAccountBreakdownBySymbol(holdings) {
  const grouped = new Map();

  const addHoldingToKey = (key, holding) => {
    if (!key) {
      return;
    }

    if (!grouped.has(key)) {
      grouped.set(key, new Map());
    }

    const accounts = grouped.get(key);
    const accountLabel = [
      holding.accountName,
      holding.accountTypeLabel,
    ].filter(Boolean).join(' · ') || 'Unknown account';
    const accountKey = `${holding.institutionKey || holding.institutionName || ''}:${holding.accountId || holding.accountName || accountLabel}:${holding.currency || ''}`;
    const existing = accounts.get(accountKey) || {
      id: accountKey,
      label: accountLabel,
      quantity: 0,
    };

    existing.quantity += Number(holding.quantity) || 0;
    accounts.set(accountKey, existing);
  };

  holdings.forEach((holding) => {
    const symbolKey = getHoldingSymbolKey(holding.symbol);
    const canonicalKey = getHoldingSymbolKey(holding.canonicalSymbol || getCanonicalHoldingSymbol(holding.symbol));

    addHoldingToKey(symbolKey, holding);
    if (canonicalKey !== symbolKey) {
      addHoldingToKey(canonicalKey, holding);
    }
  });

  const accountBreakdowns = new Map();
  grouped.forEach((accounts, key) => {
    accountBreakdowns.set(
      key,
      Array.from(accounts.values()).sort((left, right) => (
        left.label.localeCompare(right.label, undefined, { sensitivity: 'base', numeric: true })
      ))
    );
  });

  return accountBreakdowns;
}

function Holdings({
  data,
  showPageTitle = true,
  selectedAccountIds,
  setSelectedAccountIds,
  allScopeInstitutions = [],
  fetchAllScopeInstitutions,
  onDataChange,
  timeframe,
  setTimeframe,
  customDateRange,
  setCustomDateRange,
}) {
  const incomeTimelineKey = timeframe;
  const setIncomeTimelineKey = setTimeframe;
  const incomeCustomDateRange = customDateRange;
  const setIncomeCustomDateRange = setCustomDateRange;
  const performanceTimelineKey = timeframe;
  const setPerformanceTimelineKey = setTimeframe;
  const performanceCustomDateRange = customDateRange;
  const setPerformanceCustomDateRange = setCustomDateRange;
  const investmentAccounts = useMemo(
    () => (data.accounts || []).filter((account) => isInvestmentsAccount(account)),
    [data.accounts]
  );

  const [localScopeInstitutions, setLocalScopeInstitutions] = useState([]);
  const holdingsScopeDraftRef = useRef(null);

  useEffect(() => {
    if (holdingsScopeDraftRef.current) return;
    const investmentEligible = getInvestmentScopeInstitutions(allScopeInstitutions);
    setLocalScopeInstitutions(cloneScopeInstitutions(investmentEligible));
  }, [allScopeInstitutions]);

  const [balancesHidden] = useBalancesHidden();
  const maskMoney = useCallback((value) => (balancesHidden ? '******' : value), [balancesHidden]);
  const { primaryCurrency, fxRates, setPrimaryCurrency } = useCurrency();
  const primaryCurrencySymbol = currencySymbolFor(primaryCurrency);
  const { chartColors } = useTheme();
  const holdingsChartColors = chartColors.holdingsBars;
  const holdingsPieChartColors = chartColors.holdingsPie;
  const holdingsPieOtherFill = chartColors.other;
  const portfolioPerformanceColor = chartColors.netWorth;
  const dividendHistoryBarColor = chartColors.dividendIncome;
  const withholdingHistoryBarColor = chartColors.withholdingTax;
  const interestReceivedBarColor = chartColors.interestReceived;
  const interestPaidBarColor = chartColors.interestPaid;
  const performanceLineColors = useMemo(() => ({
    portfolio: portfolioPerformanceColor,
    sp500: holdingsChartColors[1],
    dow: holdingsChartColors[3],
    nasdaq: holdingsChartColors[5],
    tsx: holdingsChartColors[7],
  }), [holdingsChartColors, portfolioPerformanceColor]);
  const [investmentsNavSlot, setInvestmentsNavSlot] = useState(null);
  const [investmentsFiltersSlot, setInvestmentsFiltersSlot] = useState(null);

  // Position tables show native currency; consolidated figures render in the
  // selected primary currency at the current FX rate.
  const convert = useMemo(() => makeCurrencyConverter(primaryCurrency, fxRates), [primaryCurrency, fxRates]);
  const fxToPrimary = useCallback((cadValue) => convert(cadValue, 'CAD', primaryCurrency), [convert, primaryCurrency]);
  const convertToPrimary = useCallback(
    (amount, fromCurrency) => convert(amount, fromCurrency, primaryCurrency),
    [convert, primaryCurrency],
  );

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined;
    }
    let cancelled = false;
    const frameId = window.requestAnimationFrame(() => {
      if (cancelled) return;
      if (typeof document === 'undefined' || !showPageTitle) {
        setInvestmentsNavSlot(null);
        setInvestmentsFiltersSlot(null);
        return;
      }
      setInvestmentsNavSlot(document.getElementById('investments-toolbar-nav-slot'));
      setInvestmentsFiltersSlot(document.getElementById('investments-toolbar-filters-slot'));
    });
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(frameId);
    };
  }, [showPageTitle]);

  const [holdingsByAccount, setHoldingsByAccount] = useState({});
  const [holdingsLoadError, setHoldingsLoadError] = useState('');
  const [holdingsRetryVersion, setHoldingsRetryVersion] = useState(0);
  const failedHoldingAccountIdsRef = useRef(new Set());
  const [isInstitutionMenuOpen, setIsInstitutionMenuOpen] = useState(false);
  const [scopeSaveError, setScopeSaveError] = useState('');
  const [scopeSaving, setScopeSaving] = useState(false);
  const [activeTab, setActiveTab] = useState('overview');
  const [showGainersLosersByValue, setShowGainersLosersByValue] = useState(false);
  const [isAllocationSectionCollapsed, setIsAllocationSectionCollapsed] = usePersistentPanelCollapsed('holdings:allocation');
  const [isGainersLosersSectionCollapsed, setIsGainersLosersSectionCollapsed] = usePersistentPanelCollapsed('holdings:gainers-losers');
  const [isPositionsTableCollapsed, setIsPositionsTableCollapsed] = usePersistentPanelCollapsed('holdings:positions-table');
  const [isCashSectionCollapsed, setIsCashSectionCollapsed] = usePersistentPanelCollapsed('holdings:cash-balance');
  const [isOptionsTableCollapsed, setIsOptionsTableCollapsed] = usePersistentPanelCollapsed('holdings:options-table');
  const [isOptionsCalendarCollapsed, setIsOptionsCalendarCollapsed] = usePersistentPanelCollapsed('holdings:options-calendar');
  const [optionsCalendarMonth, setOptionsCalendarMonth] = useState(getCurrentOptionsCalendarMonth);
  const [isCryptoTableCollapsed, setIsCryptoTableCollapsed] = usePersistentPanelCollapsed('holdings:crypto-table');
  const [holdingsTableSort, setHoldingsTableSort] = usePersistentSortConfig(
    HOLDINGS_TAB_SORT_STORAGE_KEY,
    DEFAULT_HOLDINGS_TABLE_SORT,
    HOLDINGS_TABLE_SORT_DEFAULT_DIRECTIONS,
  );
  const [optionsTableSort, setOptionsTableSort] = usePersistentSortConfig(
    OPTIONS_TAB_SORT_STORAGE_KEY,
    DEFAULT_HOLDINGS_TABLE_SORT,
    HOLDINGS_TABLE_SORT_DEFAULT_DIRECTIONS,
  );
  const [cryptoTableSort, setCryptoTableSort] = usePersistentSortConfig(
    CRYPTO_TAB_SORT_STORAGE_KEY,
    DEFAULT_HOLDINGS_TABLE_SORT,
    HOLDINGS_TABLE_SORT_DEFAULT_DIRECTIONS,
  );
  const [selectedPositionDetail, setSelectedPositionDetail] = useState(null);
  const [performanceData, setPerformanceData] = useState(null);
  const [performanceLoading, setPerformanceLoading] = useState(false);
  const [performanceError, setPerformanceError] = useState('');
  const [visiblePerformanceBenchmarkIds, setVisiblePerformanceBenchmarkIds] = useState({});
  const [isIncomeChartCollapsed, setIsIncomeChartCollapsed] = usePersistentPanelCollapsed('holdings:dividend-income-history');
  const [isIncomByPositionCollapsed, setIsIncomeByPositionCollapsed] = usePersistentPanelCollapsed('holdings:dividends-by-position');
  const [isIncomeTableCollapsed, setIsIncomeTableCollapsed] = usePersistentPanelCollapsed('holdings:recent-dividend-income');
  const [incomeSubTab, setIncomeSubTab] = useState('dividends');
  const [selectedDividendPositionSymbol, setSelectedDividendPositionSymbol] = useState(null);
  const [incomeDetailSelection, setIncomeDetailSelection] = useState(null);
  // Welcome tour (demo mode) can ask Holdings to open a specific sub-view (e.g. Income).
  useEffect(() => {
    if (!isTourDemoActive()) return undefined;
    const applyHint = () => {
      const hint = getTourHint();
      if (!hint || !hint.tab) return; // no hint → leave the tab as-is (avoids an Overview flash on unmount)
      setActiveTab(hint.tab);
      if (hint.tab === 'income' && hint.incomeSubTab) setIncomeSubTab(hint.incomeSubTab);
    };
    applyHint();
    window.addEventListener('breaktwenty-tour-hint', applyHint);
    return () => window.removeEventListener('breaktwenty-tour-hint', applyHint);
  }, []);
  const [incomeDetailExpandedGroupKeys, setIncomeDetailExpandedGroupKeys] = useState({});
  // Viewport-Y top edge for the income-detail tray. Computed as
  // (panel viewport-center) − (initial shell height / 2) so the tray
  // visually opens centered on its chart panel, but the top stays
  // pinned to that y-value as the content grows (accordion
  // expansion). See App.css → .app-edge-tray.income-detail-tray.
  const [incomeDetailTrayTopY, setIncomeDetailTrayTopY] = useState(null);
  const [isIncomeTimelineMenuOpen, setIsIncomeTimelineMenuOpen] = useState(false);
  const [isPerformanceTimelineMenuOpen, setIsPerformanceTimelineMenuOpen] = useState(false);
  const {
    isCustomCommitted: isIncomeCustomTimelineCommitted,
    isCustomSelected: isIncomeCustomTimelineSelected,
    openCustomRangeDraft: openIncomeCustomRangeDraft,
    clearCustomRangeDraft: clearIncomeCustomRangeDraft,
  } = useTimelineCustomRangeDraft({
    isOpen: isIncomeTimelineMenuOpen,
    committedKey: incomeTimelineKey,
    customKey: INCOME_CUSTOM_TIMELINE_KEY,
  });
  const {
    isCustomCommitted: isPerformanceCustomTimelineCommitted,
    isCustomSelected: isPerformanceCustomTimelineSelected,
    openCustomRangeDraft: openPerformanceCustomRangeDraft,
    clearCustomRangeDraft: clearPerformanceCustomRangeDraft,
  } = useTimelineCustomRangeDraft({
    isOpen: isPerformanceTimelineMenuOpen,
    committedKey: performanceTimelineKey,
    customKey: INCOME_CUSTOM_TIMELINE_KEY,
  });
  const [isInterestChartCollapsed, setIsInterestChartCollapsed] = usePersistentPanelCollapsed('holdings:interest-history');
  const [isInterestTableCollapsed, setIsInterestTableCollapsed] = usePersistentPanelCollapsed('holdings:recent-interest-transactions');
  const [compositionGroupMode, setCompositionGroupMode] = useState('sector');
  const [positionDetailSnapshot, setPositionDetailSnapshot] = useState(null);
  const institutionMenuRef = useRef(null);
  const incomeTimelineMenuRef = useRef(null);
  const performanceTimelineMenuRef = useRef(null);
  const investmentsHubContentRef = useRef(null);
  const dividendHistorySectionRef = useRef(null);
  const dividendPositionSectionRef = useRef(null);
  const interestHistorySectionRef = useRef(null);
  const incomeDetailTrayShellRef = useRef(null);
  const positionDetailTrayShellRef = useRef(null);
  const combinedTableFrameRefs = useRef({});
  const combinedTableHoverPointerRef = useRef(null);

  const setCombinedTableFrameRef = useCallback((tabId, element) => {
    if (element) {
      combinedTableFrameRefs.current[tabId] = element;
      return;
    }
    delete combinedTableFrameRefs.current[tabId];
  }, []);

  const updateCombinedTableHoverPointer = useCallback((event) => {
    combinedTableHoverPointerRef.current = { x: event.clientX, y: event.clientY };
  }, []);

  const clearCombinedTableHoverPointer = useCallback(() => {
    combinedTableHoverPointerRef.current = null;
  }, []);

  const clearCombinedTableSelectedHighlight = useCallback(() => {
    Object.values(combinedTableFrameRefs.current).forEach((frameElement) => {
      delete frameElement.dataset.holdingsSelectedRowHighlight;
    });
  }, []);

  const clearCombinedTableHoverHighlight = useCallback((frameElement = null) => {
    const frameElements = frameElement ? [frameElement] : Object.values(combinedTableFrameRefs.current);
    frameElements.forEach((currentFrameElement) => {
      delete currentFrameElement.dataset.holdingsHoverRowHighlight;
    });
  }, []);

  const syncCombinedTableRowHighlight = useCallback((rowElement, mode) => {
    const frameElement = rowElement?.closest?.('.holdings-combined-table-frame');
    if (!frameElement || !rowElement) return;

    const frameRect = frameElement.getBoundingClientRect();
    const rowRect = rowElement.getBoundingClientRect();
    const highlightState = mode === 'selected' ? 'selected' : 'hover';
    frameElement.style.setProperty(`--holdings-${highlightState}-row-highlight-top`, `${rowRect.top - frameRect.top}px`);
    frameElement.style.setProperty(`--holdings-${highlightState}-row-highlight-height`, `${rowRect.height}px`);

    if (highlightState === 'selected') {
      frameElement.dataset.holdingsSelectedRowHighlight = 'true';
      return;
    }

    frameElement.dataset.holdingsHoverRowHighlight = 'true';
  }, []);

  const getSelectedCombinedTableRow = useCallback(() => {
    if (!selectedPositionDetail || selectedPositionDetail.tabId !== activeTab) return null;
    const frameElement = combinedTableFrameRefs.current[selectedPositionDetail.tabId];
    if (!frameElement) return null;
    return Array.from(frameElement.querySelectorAll('.holdings-summary-row'))
      .find((rowElement) => rowElement.dataset.holdingsRowDetailKey === selectedPositionDetail.key) || null;
  }, [activeTab, selectedPositionDetail]);

  const syncSelectedCombinedTableRowHighlight = useCallback(() => {
    const selectedRow = getSelectedCombinedTableRow();
    if (!selectedRow) {
      clearCombinedTableSelectedHighlight();
      return;
    }

    clearCombinedTableSelectedHighlight();
    syncCombinedTableRowHighlight(selectedRow, 'selected');
  }, [clearCombinedTableSelectedHighlight, getSelectedCombinedTableRow, syncCombinedTableRowHighlight]);

  const getCurrentCombinedTableHoverRow = useCallback((frameElement) => {
    if (!frameElement) return null;

    const pointer = combinedTableHoverPointerRef.current;
    const pointTarget = pointer && typeof document !== 'undefined'
      ? document.elementFromPoint(pointer.x, pointer.y)
      : null;
    const rowAtPointer = pointTarget?.closest?.('.holdings-summary-row');
    if (rowAtPointer && frameElement.contains(rowAtPointer)) {
      return rowAtPointer;
    }

    return frameElement.querySelector('.holdings-summary-row:hover, .holdings-summary-row:focus-visible');
  }, []);

  const syncVisibleCombinedTableRowHighlights = useCallback(() => {
    if (selectedPositionDetail?.tabId === activeTab) {
      syncSelectedCombinedTableRowHighlight();
    }

    Object.values(combinedTableFrameRefs.current).forEach((frameElement) => {
      const hoverRow = getCurrentCombinedTableHoverRow(frameElement);
      if (hoverRow) {
        syncCombinedTableRowHighlight(hoverRow, 'hover');
        return;
      }

      clearCombinedTableHoverHighlight(frameElement);
    });
  }, [
    activeTab,
    clearCombinedTableHoverHighlight,
    getCurrentCombinedTableHoverRow,
    selectedPositionDetail,
    syncCombinedTableRowHighlight,
    syncSelectedCombinedTableRowHighlight,
  ]);

  useLayoutEffect(() => {
    syncSelectedCombinedTableRowHighlight();
  }, [syncSelectedCombinedTableRowHighlight]);

  useEffect(() => {
    if (!selectedPositionDetail || selectedPositionDetail.tabId !== activeTab || typeof window === 'undefined') {
      return undefined;
    }

    let frameId = null;
    const scheduleSync = () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      frameId = window.requestAnimationFrame(() => {
        frameId = null;
        syncSelectedCombinedTableRowHighlight();
      });
    };

    const selectedRow = getSelectedCombinedTableRow();
    const frameElement = combinedTableFrameRefs.current[selectedPositionDetail.tabId];
    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(scheduleSync);
    [frameElement, selectedRow].filter(Boolean).forEach((element) => {
      resizeObserver?.observe(element);
    });

    scheduleSync();
    window.addEventListener('resize', scheduleSync);
    window.visualViewport?.addEventListener('resize', scheduleSync);

    return () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      resizeObserver?.disconnect();
      window.removeEventListener('resize', scheduleSync);
      window.visualViewport?.removeEventListener('resize', scheduleSync);
    };
  }, [
    activeTab,
    getSelectedCombinedTableRow,
    selectedPositionDetail,
    syncSelectedCombinedTableRowHighlight,
  ]);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;

    let frameId = null;
    const scheduleSync = () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      frameId = window.requestAnimationFrame(() => {
        frameId = null;
        syncVisibleCombinedTableRowHighlights();
      });
    };

    const unsubscribeZoom = subscribeMainWindowZoomStatus(scheduleSync);
    window.addEventListener('resize', scheduleSync);
    window.visualViewport?.addEventListener('resize', scheduleSync);

    return () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      unsubscribeZoom();
      window.removeEventListener('resize', scheduleSync);
      window.visualViewport?.removeEventListener('resize', scheduleSync);
    };
  }, [syncVisibleCombinedTableRowHighlights]);

  const closeInstitutionMenu = useCallback(() => {
    if (holdingsScopeDraftRef.current) {
      setLocalScopeInstitutions(cloneScopeInstitutions(holdingsScopeDraftRef.current));
      holdingsScopeDraftRef.current = null;
    }
    setIsInstitutionMenuOpen(false);
  }, []);

  const openInstitutionMenu = useCallback(() => {
    setLocalScopeInstitutions((previous) => {
      holdingsScopeDraftRef.current = cloneScopeInstitutions(previous);
      return previous;
    });
    setIsInstitutionMenuOpen(true);
  }, []);
  const closeIncomeTimelineMenu = useCallback(() => {
    clearIncomeCustomRangeDraft();
    setIsIncomeTimelineMenuOpen(false);
  }, [clearIncomeCustomRangeDraft]);
  const closePerformanceTimelineMenu = useCallback(() => {
    clearPerformanceCustomRangeDraft();
    setIsPerformanceTimelineMenuOpen(false);
  }, [clearPerformanceCustomRangeDraft]);
  useDismissibleLayer({
    open: isInstitutionMenuOpen,
    ref: institutionMenuRef,
    onDismiss: closeInstitutionMenu,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

  useDismissibleLayer({
    open: isIncomeTimelineMenuOpen,
    ref: incomeTimelineMenuRef,
    onDismiss: closeIncomeTimelineMenu,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

  useDismissibleLayer({
    open: isPerformanceTimelineMenuOpen,
    ref: performanceTimelineMenuRef,
    onDismiss: closePerformanceTimelineMenu,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (cancelled) return;
      closeInstitutionMenu();
      closeIncomeTimelineMenu();
      closePerformanceTimelineMenu();
    });
    return () => {
      cancelled = true;
    };
  }, [activeTab, closeIncomeTimelineMenu, closeInstitutionMenu, closePerformanceTimelineMenu]);

  const availableScopeAccountIds = useMemo(
    () => localScopeInstitutions.flatMap((inst) => inst.accountIds),
    [localScopeInstitutions]
  );

  const selectedScopeAccountIds = useMemo(
    () => localScopeInstitutions.flatMap((inst) => (
      inst.hidden
        ? []
        : (inst.accounts || []).filter((account) => !account.hidden).map((account) => account.id)
    )),
    [localScopeInstitutions]
  );

  const selectedScopeAccountIdSet = useMemo(
    () => new Set(selectedScopeAccountIds),
    [selectedScopeAccountIds]
  );

  const hasHiddenInstitutions = useMemo(
    () => localScopeInstitutions.some((inst) => inst.hidden || (inst.accounts || []).some((account) => account.hidden)),
    [localScopeInstitutions]
  );

  const allScopeSourcesSelected = availableScopeAccountIds.length > 0
    && selectedScopeAccountIds.length === availableScopeAccountIds.length;

  // Track a serialized fingerprint of accounts — when balances or sync timestamps
  // change (after fetchData/sync), clear holdingsByAccount so everything re-fetches.
  const accountsFingerprint = useMemo(() => {
    if (!data.accounts || data.accounts.length === 0) return '';
    return JSON.stringify(
      data.accounts.map((a) => ({
        id: a.id,
        balance: a.balance,
        last_synced: a.last_synced,
      }))
    );
  }, [data.accounts]);

  const {
    incomeTransactions,
    incomeLoading,
    incomeError,
    retryIncomeTransactions,
  } = useIncomeTransactions({
    active: activeTab === 'income',
    currency: primaryCurrency,
    resourceKey: accountsFingerprint,
  });

  const prevAccountsFingerprintRef = useRef(accountsFingerprint);
  useEffect(() => {
    if (accountsFingerprint !== prevAccountsFingerprintRef.current) {
      prevAccountsFingerprintRef.current = accountsFingerprint;
      failedHoldingAccountIdsRef.current.clear();
      setHoldingsLoadError('');
      setHoldingsByAccount({});
      setPerformanceData(null);
    }
  }, [accountsFingerprint]);

  useEffect(() => {
    const activeIds = new Set(investmentAccounts.map((account) => account.id));

    let cancelled = false;
    Promise.resolve().then(() => {
      if (cancelled) return;
      setHoldingsByAccount((previous) => {
        const next = Object.fromEntries(
          Object.entries(previous).filter(([accountId]) => activeIds.has(Number(accountId)))
        );
        const previousKeys = Object.keys(previous);
        const nextKeys = Object.keys(next);

        if (
          previousKeys.length === nextKeys.length &&
          previousKeys.every((key) => nextKeys.includes(key))
        ) {
          return previous;
        }

        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [investmentAccounts]);

  useEffect(() => {
    let cancelled = false;

    const missingAccounts = investmentAccounts.filter(
      (account) => (
        !Object.prototype.hasOwnProperty.call(holdingsByAccount, account.id)
        && !failedHoldingAccountIdsRef.current.has(account.id)
      )
    );

    if (missingAccounts.length === 0) {
      return undefined;
    }

    const controller = new AbortController();
    const loadHoldings = async () => {
      const results = await Promise.allSettled(
        missingAccounts.map(async (account) => {
          const response = await fetch(`${API}/accounts/${account.id}/holdings`, {
            signal: controller.signal,
          });
          const payload = await readJsonResponse(response, {
            label: `${account.name || 'Account'} holdings`,
            validate: Array.isArray,
          });
          return [account.id, payload];
        })
      );

      if (cancelled) {
        return;
      }

      const successfulResults = results
        .filter((result) => result.status === 'fulfilled')
        .map((result) => result.value);
      const failedResults = results
        .map((result, index) => ({ result, account: missingAccounts[index] }))
        .filter(({ result }) => result.status === 'rejected' && result.reason?.name !== 'AbortError');

      if (successfulResults.length > 0) {
        setHoldingsByAccount((previous) => {
          const next = { ...previous };
          successfulResults.forEach(([accountId, accountHoldings]) => {
            next[accountId] = accountHoldings;
          });
          return next;
        });
      }
      if (failedResults.length > 0) {
        failedResults.forEach(({ account }) => failedHoldingAccountIdsRef.current.add(account.id));
        setHoldingsLoadError(
          failedResults.length === 1
            ? failedResults[0].result.reason?.message || 'Holdings could not be loaded.'
            : `${failedResults.length} accounts could not load holdings.`,
        );
      }
    };

    void loadHoldings();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [holdingsByAccount, holdingsRetryVersion, investmentAccounts]);

  const appliedAccountIds = useMemo(
    () => investmentAccounts.map((account) => account.id),
    [investmentAccounts]
  );

  const selectedAccountIdsForView = appliedAccountIds;
  const selectedAccountIdSet = useMemo(
    () => new Set(appliedAccountIds),
    [appliedAccountIds]
  );

  const performanceTimelineBounds = useMemo(
    () => getIncomeTimelineBounds(performanceTimelineKey, performanceCustomDateRange),
    [performanceCustomDateRange, performanceTimelineKey]
  );

  const performanceTimelineSummary = useMemo(
    () => getIncomeTimelineSummary(performanceTimelineKey, performanceCustomDateRange),
    [performanceCustomDateRange, performanceTimelineKey]
  );

  const draftSelectedAccountIdSet = selectedScopeAccountIdSet;

  // ── Income tab: transactions use per-date FX and remain keyed to currency ──
  // Rows are fetched with `convert_to=<primary>` so the backend stamps each
  // payment's `amount_primary` using the HISTORICAL FX rate on its own date
  // (accurate for multi-year dividend/interest history). The loader does not
  // request until Income is active and never exposes a prior currency's cache.

  useEffect(() => {
    if (activeTab !== 'performance') {
      return undefined;
    }

    let isCancelled = false;
    if (selectedAccountIdsForView.length === 0) {
      Promise.resolve().then(() => {
        if (isCancelled) return;
        setPerformanceData(null);
        setPerformanceError('');
        setPerformanceLoading(false);
      });
      return () => {
        isCancelled = true;
      };
    }

    let inFlight = false;
    let activeController = null;

    const loadPerformance = async ({ showLoading = true } = {}) => {
      if (inFlight) return;
      inFlight = true;
      const controller = new AbortController();
      activeController = controller;
      if (showLoading) {
        setPerformanceLoading(true);
      }
      setPerformanceError('');

      const params = new URLSearchParams();
      params.set('account_ids', selectedAccountIdsForView.join(','));
      params.set('timeline', performanceTimelineKey);
      if (performanceTimelineKey === INCOME_CUSTOM_TIMELINE_KEY) {
        if (performanceTimelineBounds.startDate) {
          params.set('start_date', performanceTimelineBounds.startDate);
        }
        if (performanceTimelineBounds.endDate) {
          params.set('end_date', performanceTimelineBounds.endDate);
        }
      }

      try {
        const response = await fetch(`${API}/investments/performance?${params.toString()}`, {
          signal: controller.signal,
        });
        const payload = await response.json();
        if (!response.ok || payload?.status === 'error') {
          throw new Error(payload?.message || 'Failed to fetch performance data');
        }
        if (!isCancelled) {
          setPerformanceData(payload);
        }
      } catch (err) {
        if (err.name === 'AbortError' || isCancelled) {
          return;
        }
        console.error('Failed to fetch performance data:', err);
        setPerformanceError(err.message || 'Failed to fetch performance data');
        if (showLoading) {
          setPerformanceData(null);
        }
      } finally {
        inFlight = false;
        if (activeController === controller) {
          activeController = null;
        }
        if (!controller.signal.aborted && !isCancelled && showLoading) {
          setPerformanceLoading(false);
        }
      }
    };

    Promise.resolve().then(() => {
      if (!isCancelled) {
        loadPerformance();
      }
    });
    const intervalId = window.setInterval(() => loadPerformance({ showLoading: false }), PERFORMANCE_REFRESH_MS);

    return () => {
      isCancelled = true;
      window.clearInterval(intervalId);
      if (activeController) {
        activeController.abort();
      }
    };
  }, [
    activeTab,
    performanceTimelineBounds,
    performanceTimelineKey,
    selectedAccountIdsForView,
  ]);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (cancelled) return;
      setIncomeDetailSelection(null);
      setIncomeDetailExpandedGroupKeys({});
      setSelectedDividendPositionSymbol(null);
    });
    return () => {
      cancelled = true;
    };
  }, [incomeSubTab]);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) {
        setIncomeDetailExpandedGroupKeys({});
      }
    });
    return () => {
      cancelled = true;
    };
  }, [incomeDetailSelection]);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (cancelled) return;
      if (activeTab !== 'income') {
        setIsIncomeTimelineMenuOpen(false);
        setSelectedDividendPositionSymbol(null);
        setIncomeDetailSelection(null);
      }
      if (activeTab !== 'performance') {
        setIsPerformanceTimelineMenuOpen(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [activeTab]);

  useEffect(() => {
    if (selectedPositionDetail && selectedPositionDetail.tabId !== activeTab) {
      let cancelled = false;
      Promise.resolve().then(() => {
        if (!cancelled) {
          setSelectedPositionDetail(null);
        }
      });
      return () => {
        cancelled = true;
      };
    }
    return undefined;
  }, [activeTab, selectedPositionDetail]);


  const closeIncomeDetailDrawer = useCallback(() => {
    setIncomeDetailSelection(null);
    setIncomeDetailExpandedGroupKeys({});
  }, []);

  // Position-detail tray uses the standard side-panel pattern:
  // position:fixed at a stable viewport top (see App.css). No anchor-
  // to-row math, no measurement, no resize observer, no edge cases at
  // the top/bottom of the table. The clicked row's highlight stripe +
  // the tray's title bar provide the link to the row.

  const handleIncomeDetailGroupToggle = useCallback((groupKey) => {
    setIncomeDetailExpandedGroupKeys((previous) => (
      previous[groupKey]
        ? {}
        : { [groupKey]: true }
    ));
  }, []);

  const handleIncomeHistoryBucketSelect = useCallback((chartId, entry) => {
    if (!chartId || !entry?._monthKey) {
      return;
    }

    const preserveTourDividendTray = chartId === 'dividends' && isDividendIncomeTourTrayLocked();

    // Don't measure inline — measurement happens in a useLayoutEffect that
    // runs AFTER React commits the selection state. Measuring here would
    // read the pre-click layout and the first-ever open of the tray would
    // land at a different top than subsequent re-opens (after the bar chart
    // has settled into a selected/highlighted state).
    setIncomeDetailSelection((current) => {
      const isCurrentBucket = current?.chartId === chartId && current?.bucketKey === entry._monthKey;
      if (isCurrentBucket && preserveTourDividendTray) {
        return current;
      }
      return isCurrentBucket
        ? null
        : {
          chartId,
          bucketKey: entry._monthKey,
        };
    });
    setIncomeDetailExpandedGroupKeys({});
  }, []);

  const handleDividendPositionSelect = useCallback((entry) => {
    const nextSymbol = entry?.symbol;
    if (!nextSymbol) {
      return;
    }

    setSelectedDividendPositionSymbol(nextSymbol);
    setIncomeDetailExpandedGroupKeys({});
  }, []);

  // ── Income: filter by selected accounts, compute datasets ──
  const isInterestTx = useCallback(
    (tx) => tx.type === 'interest' || INTEREST_SYMBOLS.test(tx.symbol || ''),
    []
  );

  const incomeTypes = useMemo(() => new Set(['dividend', 'distribution', 'interest']), []);

  const filteredIncomeTransactions = useMemo(() => {
    if (!Array.isArray(incomeTransactions)) return [];
    return incomeTransactions.filter((tx) => selectedAccountIdSet.has(tx.account_id));
  }, [incomeTransactions, selectedAccountIdSet]);

  // All income-type transactions (dividends + interest combined)
  const allIncomeTransactions = useMemo(
    () => filteredIncomeTransactions.filter((tx) => incomeTypes.has(tx.type)),
    [filteredIncomeTransactions, incomeTypes]
  );

  // Split: pure dividends (exclude interest entries) vs interest
  const dividendTransactions = useMemo(
    () => allIncomeTransactions.filter((tx) => !isInterestTx(tx)),
    [allIncomeTransactions, isInterestTx]
  );

  const interestTransactions = useMemo(
    () => filteredIncomeTransactions.filter((tx) => isInterestTx(tx)),
    [filteredIncomeTransactions, isInterestTx]
  );

  const withholdingTransactions = useMemo(
    () => filteredIncomeTransactions.filter((tx) => tx.type === 'withholding_tax'),
    [filteredIncomeTransactions]
  );

  const incomeTimelineBounds = useMemo(
    () => getIncomeTimelineBounds(incomeTimelineKey, incomeCustomDateRange),
    [incomeCustomDateRange, incomeTimelineKey]
  );

  const incomeTimelineSummary = useMemo(
    () => getIncomeTimelineSummary(incomeTimelineKey, incomeCustomDateRange),
    [incomeCustomDateRange, incomeTimelineKey]
  );

  const incomeAggregationReferenceTransactions = useMemo(
    () => [...dividendTransactions, ...withholdingTransactions, ...interestTransactions],
    [dividendTransactions, interestTransactions, withholdingTransactions]
  );

  const incomeAggregationKey = useMemo(
    () => getIncomeAggregationKey(incomeTimelineBounds, incomeAggregationReferenceTransactions),
    [incomeAggregationReferenceTransactions, incomeTimelineBounds]
  );

  const resolveIncomeSymbol = useCallback(
    (tx) => tx.symbol || tx.description || 'Other',
    []
  );

  const isWithinIncomeTimeline = useCallback((tx) => {
    const txDateValue = tx.date ? tx.date.slice(0, 10) : null;

    if (!txDateValue) {
      return false;
    }

    if (incomeTimelineBounds.startDate && txDateValue < incomeTimelineBounds.startDate) {
      return false;
    }

    if (incomeTimelineBounds.endDate && txDateValue > incomeTimelineBounds.endDate) {
      return false;
    }

    return true;
  }, [incomeTimelineBounds]);

  const buildIncomePositionData = useCallback((transactions, { useAbsoluteValues = false } = {}) => {
    const symbolTotals = {};

    transactions.forEach((tx) => {
      const symbol = resolveIncomeSymbol(tx);
      const rawValue = (tx.amount_primary != null ? tx.amount_primary : convertToPrimary(tx.amount, tx.currency));
      const value = useAbsoluteValues ? Math.abs(rawValue) : rawValue;
      symbolTotals[symbol] = (symbolTotals[symbol] || 0) + value;
    });

    return Object.entries(symbolTotals)
      .map(([symbol, total]) => ({ symbol, total }))
      .sort((left, right) => right.total - left.total);
  }, [convertToPrimary, resolveIncomeSymbol]);

  // Interest summary metrics
  const interestTimelineTransactions = useMemo(
    () => interestTransactions.filter((tx) => isWithinIncomeTimeline(tx)),
    [interestTransactions, isWithinIncomeTimeline]
  );

  const interestSummary = useMemo(() => {
    const earned = interestTimelineTransactions
      .filter((tx) => tx.amount > 0)
      .reduce((sum, tx) => sum + (tx.amount_primary != null ? tx.amount_primary : convertToPrimary(tx.amount, tx.currency)), 0);
    const paid = interestTimelineTransactions
      .filter((tx) => tx.amount < 0)
      .reduce((sum, tx) => sum + (tx.amount_primary != null ? tx.amount_primary : convertToPrimary(tx.amount, tx.currency)), 0);
    return { earned, paid, net: earned + paid };
  }, [interestTimelineTransactions, convertToPrimary]);

  // Interest monthly chart data
  const interestMonthlyChartData = useMemo(() => {
    if (interestTimelineTransactions.length === 0) return { months: [], symbols: [] };
    const bucketMap = {};
    const buildDrawerRows = (bucketEntry) => sortIncomeDetailRows([
      ...Object.values(bucketEntry.earnedAccounts || {}).map((entry) => ({
        key: `earned-${entry.key}`,
        institution: entry.institution,
        account: entry.account,
        meta: 'Earned',
        value: entry.value,
        tone: 'positive',
      })),
      ...Object.values(bucketEntry.paidAccounts || {}).map((entry) => ({
        key: `paid-${entry.key}`,
        institution: entry.institution,
        account: entry.account,
        meta: 'Paid',
        value: entry.value,
        tone: 'negative',
      })),
    ]);
    const getAccountCount = (bucketEntry) => (
      new Set([
        ...Object.keys(bucketEntry.earnedAccounts || {}),
        ...Object.keys(bucketEntry.paidAccounts || {}),
      ]).size
    );

    interestTimelineTransactions.forEach((tx) => {
      const dateValue = tx.date ? tx.date.slice(0, 10) : null;
      const bucketMeta = getIncomeBucketMeta(dateValue, incomeAggregationKey);
      const monthMeta = getIncomeBucketMeta(dateValue, 'monthly');
      if (!bucketMeta || !monthMeta) {
        return;
      }

      const bucketKey = bucketMeta.key;
      if (!bucketMap[bucketKey]) {
        bucketMap[bucketKey] = {
          label: bucketMeta.label,
          earned: 0,
          paid: 0,
          earnedAccounts: {},
          paidAccounts: {},
          months: {},
        };
      }

      if (!bucketMap[bucketKey].months[monthMeta.key]) {
        bucketMap[bucketKey].months[monthMeta.key] = {
          label: formatIncomeLongMonthLabel(monthMeta.key),
          earned: 0,
          paid: 0,
          earnedAccounts: {},
          paidAccounts: {},
        };
      }

      const converted = (tx.amount_primary != null ? tx.amount_primary : convertToPrimary(tx.amount, tx.currency));
      const accountLabel = getIncomeAccountLabel(tx);
      const institutionLabel = getIncomeInstitutionLabel(tx);
      const accountKey = `${institutionLabel}::${accountLabel}`;
      const monthEntry = bucketMap[bucketKey].months[monthMeta.key];
      if (tx.amount >= 0) {
        bucketMap[bucketKey].earned += converted;
        bucketMap[bucketKey].earnedAccounts[accountKey] = bucketMap[bucketKey].earnedAccounts[accountKey] || {
          key: accountKey,
          institution: institutionLabel,
          account: accountLabel,
          value: 0,
        };
        bucketMap[bucketKey].earnedAccounts[accountKey].value += converted;
        monthEntry.earned += converted;
        monthEntry.earnedAccounts[accountKey] = monthEntry.earnedAccounts[accountKey] || {
          key: accountKey,
          institution: institutionLabel,
          account: accountLabel,
          value: 0,
        };
        monthEntry.earnedAccounts[accountKey].value += converted;
      } else {
        const paidValue = Math.abs(converted);
        bucketMap[bucketKey].paid += paidValue;
        bucketMap[bucketKey].paidAccounts[accountKey] = bucketMap[bucketKey].paidAccounts[accountKey] || {
          key: accountKey,
          institution: institutionLabel,
          account: accountLabel,
          value: 0,
        };
        bucketMap[bucketKey].paidAccounts[accountKey].value += paidValue;
        monthEntry.paid += paidValue;
        monthEntry.paidAccounts[accountKey] = monthEntry.paidAccounts[accountKey] || {
          key: accountKey,
          institution: institutionLabel,
          account: accountLabel,
          value: 0,
        };
        monthEntry.paidAccounts[accountKey].value += paidValue;
      }
    });

    const months = Object.keys(bucketMap).sort().map((bucketKey) => {
      const bucketEntry = bucketMap[bucketKey];
      const segments = [
        { key: 'Earned', label: 'Earned', value: bucketEntry.earned, color: interestReceivedBarColor },
        { key: 'Paid', label: 'Paid', value: bucketEntry.paid, color: interestPaidBarColor },
      ].filter((segment) => Math.abs(segment.value) > 0.0001);
      const rows = buildDrawerRows(bucketEntry);
      const usesGroupedDetail = incomeAggregationKey !== 'monthly';
      const groups = Object.keys(bucketEntry.months)
        .sort()
        .map((monthKey) => {
          const monthEntry = bucketEntry.months[monthKey];
          const monthRows = buildDrawerRows(monthEntry);

          return {
            key: monthKey,
            label: monthEntry.label,
            total: (Number(monthEntry.earned) || 0) - (Number(monthEntry.paid) || 0),
            earned: monthEntry.earned,
            paid: monthEntry.paid,
            rows: monthRows,
            summaryItems: [
              {
                key: `${monthKey}-earned`,
                label: 'Earned',
                kind: 'money',
                value: monthEntry.earned,
              },
              {
                key: `${monthKey}-paid`,
                label: 'Paid',
                kind: 'money',
                value: monthEntry.paid,
              },
              {
                key: `${monthKey}-accounts`,
                text: formatCountLabel(getAccountCount(monthEntry), 'account'),
              },
            ],
          };
        });
      const accountCount = getAccountCount(bucketEntry);
      const netTotal = (Number(bucketEntry.earned) || 0) - (Number(bucketEntry.paid) || 0);

      return {
        month: bucketEntry.label,
        _monthKey: bucketKey,
        total: netTotal,
        segments,
        Earned: bucketEntry.earned,
        Paid: bucketEntry.paid,
        _drawerDetail: {
          kind: 'interest',
          periodLabel: bucketEntry.label,
          summaryItems: [
            {
              key: 'total',
              label: 'Total',
              kind: 'money',
              value: netTotal,
            },
            {
              key: 'earned',
              label: 'Earned',
              kind: 'money',
              value: bucketEntry.earned,
              tone: 'positive',
            },
            {
              key: 'paid',
              label: 'Paid',
              kind: 'money',
              value: bucketEntry.paid,
              tone: 'negative',
            },
            {
              key: 'accounts',
              label: 'Accounts',
              text: formatCountLabel(accountCount, 'account'),
            },
          ],
          listTitle: usesGroupedDetail ? 'Monthly Breakdown' : 'Account Breakdown',
          listMeta: usesGroupedDetail
            ? formatCountLabel(groups.length, 'month')
            : formatCountLabel(accountCount, 'account'),
          rows: usesGroupedDetail ? [] : rows,
          groups: usesGroupedDetail ? groups : [],
          rowCount: accountCount,
          groupCount: usesGroupedDetail ? groups.length : 0,
          rowSingular: 'account',
          rowPlural: 'accounts',
          emptyMessage: 'No account detail is available for this period.',
        },
      };
    });

    return { months, symbols: ['Earned', 'Paid'] };
  }, [convertToPrimary, incomeAggregationKey, interestPaidBarColor, interestReceivedBarColor, interestTimelineTransactions]);

  const recentInterest = useMemo(() => {
    return [...interestTransactions]
      .sort((a, b) => new Date(b.date) - new Date(a.date))
      .slice(0, 20);
  }, [interestTransactions]);

  const dividendTimelineTransactions = useMemo(
    () => dividendTransactions.filter((tx) => isWithinIncomeTimeline(tx)),
    [dividendTransactions, isWithinIncomeTimeline]
  );

  const withholdingTimelineTransactions = useMemo(
    () => withholdingTransactions.filter((tx) => isWithinIncomeTimeline(tx)),
    [isWithinIncomeTimeline, withholdingTransactions]
  );

  const selectedDividendTimelineTransactions = useMemo(() => {
    if (!selectedDividendPositionSymbol) {
      return dividendTimelineTransactions;
    }

    return dividendTimelineTransactions.filter((tx) => resolveIncomeSymbol(tx) === selectedDividendPositionSymbol);
  }, [dividendTimelineTransactions, resolveIncomeSymbol, selectedDividendPositionSymbol]);

  const selectedWithholdingTimelineTransactions = useMemo(() => {
    if (!selectedDividendPositionSymbol) {
      return withholdingTimelineTransactions;
    }

    return withholdingTimelineTransactions.filter((tx) => resolveIncomeSymbol(tx) === selectedDividendPositionSymbol);
  }, [resolveIncomeSymbol, selectedDividendPositionSymbol, withholdingTimelineTransactions]);

  useEffect(() => {
    if (!selectedDividendPositionSymbol) {
      return;
    }

    const hasSelectedSymbol = [...dividendTransactions, ...withholdingTransactions]
      .some((tx) => resolveIncomeSymbol(tx) === selectedDividendPositionSymbol);

    if (!hasSelectedSymbol) {
      let cancelled = false;
      Promise.resolve().then(() => {
        if (cancelled) return;
        setSelectedDividendPositionSymbol(null);
        closeIncomeDetailDrawer();
      });
      return () => {
        cancelled = true;
      };
    }
    return undefined;
  }, [
    closeIncomeDetailDrawer,
    dividendTransactions,
    resolveIncomeSymbol,
    selectedDividendPositionSymbol,
    withholdingTransactions,
  ]);

  const dividendAverageMonthCount = useMemo(() => {
    const today = getTodayDateValue();
    const dividendDateValues = dividendTimelineTransactions
      .map((tx) => (tx?.date ? tx.date.slice(0, 10) : null))
      .filter(Boolean)
      .sort();
    const earliestDividendDate = dividendDateValues[0] || incomeTimelineBounds.startDate || incomeTimelineBounds.endDate || today;
    const latestDividendDate = dividendDateValues[dividendDateValues.length - 1] || incomeTimelineBounds.endDate || today;
    const startDate = incomeTimelineBounds.startDate || earliestDividendDate;
    const endDate = incomeTimelineBounds.endDate || (incomeTimelineBounds.startDate ? today : latestDividendDate);

    return getInclusiveMonthSpan(startDate, endDate);
  }, [dividendTimelineTransactions, incomeTimelineBounds]);

  const incomeSummary = useMemo(() => {
    const totalDividends = dividendTimelineTransactions.reduce(
      (sum, tx) => sum + (tx.amount_primary != null ? tx.amount_primary : convertToPrimary(tx.amount, tx.currency)),
      0
    );
    const totalWithholding = withholdingTimelineTransactions.reduce(
      (sum, tx) => sum + Math.abs((tx.amount_primary != null ? tx.amount_primary : convertToPrimary(tx.amount, tx.currency))),
      0
    );

    const avgMonthly = dividendAverageMonthCount > 0 ? totalDividends / dividendAverageMonthCount : 0;

    return { totalDividends, avgMonthly, totalWithholding };
  }, [convertToPrimary, dividendAverageMonthCount, dividendTimelineTransactions, withholdingTimelineTransactions]);

  const combinedDividendHistoryData = useMemo(() => {
    if (selectedDividendTimelineTransactions.length === 0 && selectedWithholdingTimelineTransactions.length === 0) {
      return { months: [], symbols: ['Dividends', 'Withholding Tax'] };
    }

    const bucketMap = {};
    const addBucketTransaction = (tx, kind) => {
      const dateValue = tx.date ? tx.date.slice(0, 10) : null;
      const bucketMeta = getIncomeBucketMeta(dateValue, incomeAggregationKey);
      const monthMeta = getIncomeBucketMeta(dateValue, 'monthly');
      if (!bucketMeta || !monthMeta) {
        return;
      }

      const bucketKey = bucketMeta.key;
      const symbol = resolveIncomeSymbol(tx);
      const accountLabel = getIncomeAccountLabel(tx);
      const institutionLabel = getIncomeInstitutionLabel(tx);
      const converted = (tx.amount_primary != null ? tx.amount_primary : convertToPrimary(tx.amount, tx.currency));
      const signedValue = kind === 'withholding' ? -Math.abs(converted) : converted;
      const rowKey = `${kind}:${symbol}:${institutionLabel}:${accountLabel}`;

      if (!bucketMap[bucketKey]) {
        bucketMap[bucketKey] = {
          label: bucketMeta.label,
          dividends: 0,
          withholding: 0,
          rowMap: {},
          months: {},
        };
      }

      if (!bucketMap[bucketKey].months[monthMeta.key]) {
        bucketMap[bucketKey].months[monthMeta.key] = {
          label: formatIncomeLongMonthLabel(monthMeta.key),
          dividends: 0,
          withholding: 0,
          rowMap: {},
        };
      }

      if (!bucketMap[bucketKey].rowMap[rowKey]) {
        bucketMap[bucketKey].rowMap[rowKey] = {
          key: rowKey,
          label: symbol,
          institution: institutionLabel,
          account: accountLabel,
          tone: kind === 'withholding' ? 'withholding' : 'positive',
          value: 0,
          _latestQty: null,
          _latestDate: null,
          _perShareSum: 0,
        };
      }

      if (!bucketMap[bucketKey].months[monthMeta.key].rowMap[rowKey]) {
        bucketMap[bucketKey].months[monthMeta.key].rowMap[rowKey] = {
          key: rowKey,
          label: symbol,
          institution: institutionLabel,
          account: accountLabel,
          tone: kind === 'withholding' ? 'withholding' : 'positive',
          value: 0,
          _latestQty: null,
          _latestDate: null,
          _perShareSum: 0,
        };
      }

      bucketMap[bucketKey].rowMap[rowKey].value += signedValue;
      bucketMap[bucketKey].months[monthMeta.key].rowMap[rowKey].value += signedValue;

      if (kind === 'dividend') {
        const quantity = Number(tx.quantity);
        if (Number.isFinite(quantity) && quantity > 0) {
          const bucketRow = bucketMap[bucketKey].rowMap[rowKey];
          const monthRow = bucketMap[bucketKey].months[monthMeta.key].rowMap[rowKey];
          bucketRow._perShareSum += converted / quantity;
          monthRow._perShareSum += converted / quantity;
          if (!bucketRow._latestDate || (dateValue && dateValue >= bucketRow._latestDate)) {
            bucketRow._latestDate = dateValue;
            bucketRow._latestQty = quantity;
          }
          if (!monthRow._latestDate || (dateValue && dateValue >= monthRow._latestDate)) {
            monthRow._latestDate = dateValue;
            monthRow._latestQty = quantity;
          }
        }
      }

      if (kind === 'withholding') {
        bucketMap[bucketKey].withholding += signedValue;
        bucketMap[bucketKey].months[monthMeta.key].withholding += signedValue;
      } else {
        bucketMap[bucketKey].dividends += signedValue;
        bucketMap[bucketKey].months[monthMeta.key].dividends += signedValue;
      }
    };

    selectedDividendTimelineTransactions.forEach((tx) => addBucketTransaction(tx, 'dividend'));
    selectedWithholdingTimelineTransactions.forEach((tx) => addBucketTransaction(tx, 'withholding'));

    const months = Object.keys(bucketMap).sort().map((bucketKey) => {
      const bucketEntry = bucketMap[bucketKey];
      const rows = decorateDividendRowMeta(sortIncomeDetailRows(Object.values(bucketEntry.rowMap)), primaryCurrency);
      const usesGroupedDetail = incomeAggregationKey !== 'monthly';
      const uniqueSymbolCount = new Set(rows.map((row) => row.label)).size;
      const groups = Object.keys(bucketEntry.months)
        .sort()
        .map((monthKey) => {
          const monthEntry = bucketEntry.months[monthKey];
          const monthRows = decorateDividendRowMeta(sortIncomeDetailRows(Object.values(monthEntry.rowMap)), primaryCurrency);

          return {
            key: monthKey,
            label: monthEntry.label,
            total: (Number(monthEntry.dividends) || 0) + (Number(monthEntry.withholding) || 0),
            dividends: monthEntry.dividends,
            withholding: monthEntry.withholding,
            rows: monthRows,
            summaryItems: [
              {
                key: `${monthKey}-dividends`,
                label: 'Dividends',
                kind: 'money',
                value: monthEntry.dividends,
                tone: 'positive',
              },
              {
                key: `${monthKey}-withholding`,
                label: 'Withholding Tax',
                kind: 'money',
                value: monthEntry.withholding,
                tone: 'withholding',
              },
              {
                key: `${monthKey}-symbols`,
                text: formatCountLabel(new Set(monthRows.map((row) => row.label)).size, 'symbol'),
              },
            ],
          };
        });
      const total = (Number(bucketEntry.dividends) || 0) + (Number(bucketEntry.withholding) || 0);

      return {
        month: bucketEntry.label,
        _monthKey: bucketKey,
        total,
        segments: [
          { key: 'Dividends', label: 'Dividends', value: bucketEntry.dividends, color: dividendHistoryBarColor },
          { key: 'Withholding Tax', label: 'Withholding Tax', value: bucketEntry.withholding, color: withholdingHistoryBarColor },
        ].filter((segment) => Math.abs(Number(segment.value) || 0) > 0.0001),
        Dividends: bucketEntry.dividends,
        Withholding: bucketEntry.withholding,
        Net: total,
        _drawerDetail: {
          kind: 'combinedIncome',
          periodLabel: bucketEntry.label,
          summaryItems: [
            {
              key: 'net',
              label: 'Net',
              kind: 'money',
              value: total,
            },
            {
              key: 'dividends',
              label: 'Dividends',
              kind: 'money',
              value: bucketEntry.dividends,
              tone: 'positive',
            },
            {
              key: 'withholding',
              label: 'Withholding Tax',
              kind: 'money',
              value: bucketEntry.withholding,
              tone: 'withholding',
            },
          ],
          listTitle: usesGroupedDetail ? 'Monthly Breakdown' : 'Symbol Breakdown',
          listMeta: usesGroupedDetail
            ? formatCountLabel(groups.length, 'month')
            : formatCountLabel(uniqueSymbolCount, 'symbol'),
          rows: usesGroupedDetail ? [] : rows,
          groups: usesGroupedDetail ? groups : [],
          rowCount: uniqueSymbolCount,
          groupCount: usesGroupedDetail ? groups.length : 0,
          rowSingular: 'symbol',
          rowPlural: 'symbols',
          emptyMessage: 'No dividend or withholding tax detail is available for this period.',
        },
      };
    });

    return { months, symbols: ['Dividends', 'Withholding Tax'] };
  }, [
    convertToPrimary,
    dividendHistoryBarColor,
    incomeAggregationKey,
    primaryCurrency,
    resolveIncomeSymbol,
    selectedDividendTimelineTransactions,
    selectedWithholdingTimelineTransactions,
    withholdingHistoryBarColor,
  ]);

  const dividendsByPosition = useMemo(() => {
    const withholdingBySymbol = withholdingTimelineTransactions.reduce((accumulator, tx) => {
      const symbol = resolveIncomeSymbol(tx);
      const value = Math.abs((tx.amount_primary != null ? tx.amount_primary : convertToPrimary(tx.amount, tx.currency)));
      accumulator[symbol] = (accumulator[symbol] || 0) + value;
      return accumulator;
    }, {});

    return buildIncomePositionData(dividendTimelineTransactions)
      .map((entry) => ({
        ...entry,
        grossDividends: entry.total,
        withholdingTaxTotal: withholdingBySymbol[entry.symbol] || 0,
        netTotal: (Number(entry.total) || 0) - (withholdingBySymbol[entry.symbol] || 0),
        total: (Number(entry.total) || 0) - (withholdingBySymbol[entry.symbol] || 0),
      }))
      .sort((left, right) => {
        if (right.total !== left.total) {
          return right.total - left.total;
        }
        return left.symbol.localeCompare(right.symbol);
      });
  }, [
    buildIncomePositionData,
    convertToPrimary,
    dividendTimelineTransactions,
    resolveIncomeSymbol,
    withholdingTimelineTransactions,
  ]);

  // Recent dividend transactions (last 20)
  const recentDividendIncomeTransactions = useMemo(() => {
    return [...dividendTransactions, ...withholdingTransactions]
      .sort((a, b) => new Date(b.date) - new Date(a.date))
      .slice(0, 20);
  }, [dividendTransactions, withholdingTransactions]);


  const dividendHistoryBucketLabel = getIncomeAggregationUnitLabel(
    incomeAggregationKey,
    combinedDividendHistoryData.months.length
  );
  const interestHistoryBucketLabel = getIncomeAggregationUnitLabel(
    incomeAggregationKey,
    interestMonthlyChartData.months.length
  );
  const incomeDetailCharts = useMemo(
    () => ({
      dividends: {
        title: 'Dividend Income History',
        data: combinedDividendHistoryData.months,
      },
      interest: {
        title: 'Interest History',
        data: interestMonthlyChartData.months,
      },
    }),
    [
      combinedDividendHistoryData.months,
      interestMonthlyChartData.months,
    ]
  );
  const selectedIncomeDetail = useMemo(() => {
    if (!incomeDetailSelection?.chartId || !incomeDetailSelection?.bucketKey) {
      return null;
    }

    const selectedChart = incomeDetailCharts[incomeDetailSelection.chartId];
    if (!selectedChart) {
      return null;
    }

    const entry = selectedChart.data.find((bucket) => bucket._monthKey === incomeDetailSelection.bucketKey);
    if (!entry?._drawerDetail) {
      return null;
    }

    return {
      chartId: incomeDetailSelection.chartId,
      chartTitle: selectedChart.title,
      entry,
      detail: entry._drawerDetail,
    };
  }, [incomeDetailCharts, incomeDetailSelection]);
  // Tour: once the dividend chart has buckets, pre-select the biggest month so its detail
  // tray opens (best-effort — if the data isn't ready the chart still shows).
  useEffect(() => {
    if (!isTourDemoActive() || activeTab !== 'income') return;
    const hint = getTourHint();
    if (!hint || hint.tab !== 'income' || !hint.selectBar) return;
    const buckets = (incomeDetailCharts.dividends && incomeDetailCharts.dividends.data) || [];
    const selectable = buckets.filter((bucket) => bucket && bucket._monthKey && bucket._drawerDetail);
    if (selectable.length === 0) return;
    const best = selectable.reduce((a, b) => ((b.total || 0) > (a.total || 0) ? b : a), selectable[0]);
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) {
        setIncomeDetailSelection({ chartId: 'dividends', bucketKey: best._monthKey });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [incomeDetailCharts, activeTab]);
  const activeIncomeDetailChartId = incomeSubTab === 'interest' ? 'interest' : 'dividends';
  const activeIncomeSelectedBucketKey = incomeDetailSelection?.chartId === activeIncomeDetailChartId
    ? incomeDetailSelection.bucketKey
    : null;
  const [incomeDrawerSnapshot, setIncomeDrawerSnapshot] = useState(null);
  const isIncomeDetailDrawerOpen = Boolean(selectedIncomeDetail);
  const visibleIncomeDrawerDetail = selectedIncomeDetail || incomeDrawerSnapshot;
  const isIncomeDetailTrayVisible = activeTab === 'income' && Boolean(visibleIncomeDrawerDetail);
  const isPositionDetailDrawerVisible = Boolean(selectedPositionDetail) && selectedPositionDetail.tabId === activeTab;
  const visiblePositionDetail = selectedPositionDetail || positionDetailSnapshot;
  // Tray-present keeps the DOM mounted through the slide-out animation by
  // falling back on the snapshot once the source selection is cleared.
  const isPositionDetailTrayPresent = Boolean(visiblePositionDetail) && visiblePositionDetail.tabId === activeTab;
  const positionDetailTray = useRightTrayOpenState('position-detail', isPositionDetailDrawerVisible);
  const incomeDetailTray = useRightTrayOpenState('income-detail', isIncomeDetailDrawerOpen);
  const activeRightTrayKey = incomeDetailTray.activeKey;
  const isIncomeCustomTimelinePickerVisible = isIncomeTimelineMenuOpen && isIncomeCustomTimelineSelected;
  const isPerformanceCustomTimelinePickerVisible = isPerformanceTimelineMenuOpen && isPerformanceCustomTimelineSelected;

  useEffect(() => {
    if (incomeDetailSelection && !selectedIncomeDetail) {
      let cancelled = false;
      Promise.resolve().then(() => {
        if (!cancelled) {
          setIncomeDetailSelection(null);
        }
      });
      return () => {
        cancelled = true;
      };
    }
    return undefined;
  }, [incomeDetailSelection, selectedIncomeDetail]);

  useEffect(() => {
    if (selectedIncomeDetail) {
      let cancelled = false;
      Promise.resolve().then(() => {
        if (!cancelled) {
          setIncomeDrawerSnapshot(selectedIncomeDetail);
        }
      });
      return () => {
        cancelled = true;
      };
    }

    if (!incomeDrawerSnapshot) {
      return undefined;
    }

    const timeoutId = window.setTimeout(() => {
      setIncomeDrawerSnapshot(null);
    }, INCOME_DETAIL_TRAY_TRANSITION_MS);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [incomeDrawerSnapshot, selectedIncomeDetail]);

  // Mirror the income-detail snapshot recipe for position-detail so the tray
  // keeps its DOM (and its measured top) through the slide-out animation.
  useEffect(() => {
    if (selectedPositionDetail) {
      let cancelled = false;
      Promise.resolve().then(() => {
        if (!cancelled) {
          setPositionDetailSnapshot(selectedPositionDetail);
        }
      });
      return () => {
        cancelled = true;
      };
    }
    if (!positionDetailSnapshot) {
      return undefined;
    }
    const timeoutId = window.setTimeout(() => {
      setPositionDetailSnapshot(null);
    }, INCOME_DETAIL_TRAY_TRANSITION_MS);
    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [positionDetailSnapshot, selectedPositionDetail]);

  // Cached at first-open time: the shell's offsetHeight on the frame
  // it mounts, before any accordion expansion or content reflow. We
  // anchor the tray's top using THIS height so growth from accordion
  // expansion only extends the tray downward — the top edge stays
  // pinned, no upward drift into the page header.
  const incomeDetailTrayInitialShellHeightRef = useRef(0);

  // Compute the tray's fixed viewport top from the source chart, then clamp it
  // into the usable viewport so low charts at high zoom do not open the tray
  // below the screen.
  const measureIncomeDetailTrayTop = useCallback((chartId) => {
    const sectionNode = chartId === 'interest'
      ? interestHistorySectionRef.current
      : dividendHistorySectionRef.current;
    if (!sectionNode) {
      return;
    }
    const sectionRect = sectionNode.getBoundingClientRect();
    const centerInViewport = sectionRect.top + (sectionRect.height / 2);
    const topY = centerInViewport - (incomeDetailTrayInitialShellHeightRef.current / 2);
    const probe = document.createElement('div');
    probe.style.cssText = 'position:fixed;top:var(--app-table-tray-top);visibility:hidden;pointer-events:none;';
    document.body.appendChild(probe);
    const minTopY = parseFloat(getComputedStyle(probe).top) || 0;
    document.body.removeChild(probe);
    const viewportHeight = window.visualViewport?.height || window.innerHeight;
    const bottomGap = 16;
    const maxTopY = Math.max(minTopY, viewportHeight - incomeDetailTrayInitialShellHeightRef.current - bottomGap);
    setIncomeDetailTrayTopY(Math.min(Math.max(topY, minTopY), maxTopY));
  }, []);

  // Cache the shell's initial height and compute the initial top.
  // Re-runs on tray open AND on bucket change (a new bucket may have
  // a different content height, so we want a fresh centered start).
  useLayoutEffect(() => {
    if (!isIncomeDetailDrawerOpen || !incomeDetailSelection?.chartId) {
      return undefined;
    }
    if (activeRightTrayKey !== 'income-detail') {
      return undefined;
    }
    const shell = incomeDetailTrayShellRef.current;
    if (shell) {
      incomeDetailTrayInitialShellHeightRef.current = shell.offsetHeight;
    }
    measureIncomeDetailTrayTop(incomeDetailSelection.chartId);
    return undefined;
  }, [
    isIncomeDetailDrawerOpen,
    incomeDetailSelection?.chartId,
    incomeDetailSelection?.bucketKey,
    activeRightTrayKey,
    measureIncomeDetailTrayTop,
  ]);

  // No scroll listener AND no resize listener: the tray is fully
  // static once opened. Top is measured exactly once at open
  // (useLayoutEffect above) and never touched again until the tray
  // is closed and re-opened. Any code path that updates the top after
  // open would make the tray drift — the user has repeatedly
  // requested zero motion.

  // Refs the income tray treats as "inside" — clicks on these don't
  // dismiss it: the tray itself, the chart section it belongs to
  // (so re-clicking a bar switches buckets), and the income timeline
  // menu popover. Memoized so useDismissibleLayer doesn't churn its
  // listener on every render.
  const incomeTrayDismissRefs = useMemo(() => (
    activeIncomeDetailChartId === 'interest'
      ? [incomeDetailTrayShellRef, interestHistorySectionRef, incomeTimelineMenuRef]
      : [incomeDetailTrayShellRef, dividendHistorySectionRef, incomeTimelineMenuRef]
  ), [activeIncomeDetailChartId]);

  const handleIncomeTrayDismiss = useCallback((event) => {
    if (isDividendIncomeTourTrayLocked()) {
      return;
    }

    // Clicks on a Dividends-by-Position symbol switch the active
    // ticker filter rather than dismissing the tray. The "inside"
    // refs above can't express this (the dividend-position section
    // contains both the symbol bars AND non-symbol whitespace), so
    // the check is a data-attribute lookup here.
    const dividendPositionSectionNode = dividendPositionSectionRef.current;
    if (
      dividendPositionSectionNode
      && dividendPositionSectionNode.contains(event.target)
      && getDividendPositionSymbolFromEventTarget(event.target)
    ) {
      return;
    }
    setSelectedDividendPositionSymbol(null);
    closeIncomeDetailDrawer();
  }, [closeIncomeDetailDrawer]);

  useDismissibleLayer({
    open: isIncomeDetailDrawerOpen || Boolean(selectedDividendPositionSymbol),
    refs: incomeTrayDismissRefs,
    ignoreAppChrome: true,
    onDismiss: handleIncomeTrayDismiss,
  });

  function handleSelectIncomeTimeline(nextTimelineKey) {
    if (nextTimelineKey === INCOME_CUSTOM_TIMELINE_KEY) {
      openIncomeCustomRangeDraft();
      return;
    }

    setIncomeCustomDateRange({ start: '', end: '' });
    clearIncomeCustomRangeDraft();
    setIncomeTimelineKey(nextTimelineKey);
    setIsIncomeTimelineMenuOpen(false);
  }

  function handleApplyIncomeCustomDateRange(nextRange) {
    setIncomeCustomDateRange(nextRange);
    setIncomeTimelineKey(INCOME_CUSTOM_TIMELINE_KEY);
    clearIncomeCustomRangeDraft();
    setIsIncomeTimelineMenuOpen(false);
  }

  function handleCancelIncomeCustomDateRange() {
    clearIncomeCustomRangeDraft();
    setIsIncomeTimelineMenuOpen(false);
  }

  function handleSelectPerformanceTimeline(nextTimelineKey) {
    if (nextTimelineKey === INCOME_CUSTOM_TIMELINE_KEY) {
      openPerformanceCustomRangeDraft();
      return;
    }

    setPerformanceCustomDateRange({ start: '', end: '' });
    clearPerformanceCustomRangeDraft();
    setPerformanceTimelineKey(nextTimelineKey);
    setIsPerformanceTimelineMenuOpen(false);
  }

  function handleApplyPerformanceCustomDateRange(nextRange) {
    setPerformanceCustomDateRange(nextRange);
    setPerformanceTimelineKey(INCOME_CUSTOM_TIMELINE_KEY);
    clearPerformanceCustomRangeDraft();
    setIsPerformanceTimelineMenuOpen(false);
  }

  function handleCancelPerformanceCustomDateRange() {
    clearPerformanceCustomRangeDraft();
    setIsPerformanceTimelineMenuOpen(false);
  }

  const incomeTimelineFilterControl = (
    <div className="investments-filter-popover income-toolbar-timeline-popover" ref={incomeTimelineMenuRef}>
      <TimelineTrigger
        isOpen={isIncomeTimelineMenuOpen}
        summary={incomeTimelineSummary}
        controls="income-timeline-filter-panel"
        className="income-timeline-trigger"
        ariaLabel="Income timeline"
        onClick={() => setIsIncomeTimelineMenuOpen((previous) => !previous)}
      />

      <div
        id="income-timeline-filter-panel"
        role="dialog"
        aria-label="Income timeline filters"
        className={`investments-filter-panel income-timeline-panel timeline-range-panel ${isIncomeTimelineMenuOpen ? 'is-open' : ''}`.trim()}
        aria-hidden={!isIncomeTimelineMenuOpen}
      >
        <div className="income-timeline-menu-list" role="menu" aria-label="Income timeline ranges">
          {INCOME_TIMELINE_PRESETS.map((preset) => {
            const isSelected = incomeTimelineKey === preset.key;
            return (
              <button
                key={preset.key}
                type="button"
                role="menuitemradio"
                aria-checked={isSelected}
                className={`income-timeline-menu-item ${isSelected ? 'is-selected' : ''}`.trim()}
                onClick={() => handleSelectIncomeTimeline(preset.key)}
              >
                <span className="income-timeline-menu-item-copy">
                  <span className="income-timeline-menu-item-label">{preset.label}</span>
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
            aria-checked={isIncomeCustomTimelineCommitted}
            className={`income-timeline-menu-item ${isIncomeCustomTimelineSelected ? 'is-selected' : ''}`.trim()}
            onClick={() => handleSelectIncomeTimeline(INCOME_CUSTOM_TIMELINE_KEY)}
          >
            <span className="income-timeline-menu-item-copy">
              <span className="income-timeline-menu-item-label">Custom Range</span>
            </span>
            <span className={`income-timeline-checkbox ${isIncomeCustomTimelineCommitted ? 'is-selected' : ''}`.trim()} aria-hidden="true">
              {isIncomeCustomTimelineCommitted ? <MdCheck size={14} /> : null}
            </span>
          </button>
        </div>

        {isIncomeCustomTimelinePickerVisible ? (
          <div
            className="timeline-range-picker-popover is-open"
            aria-label="Custom income timeline range"
          >
            <TimelineCustomRangePicker
              startDate={incomeTimelineKey === INCOME_CUSTOM_TIMELINE_KEY ? incomeCustomDateRange.start : ''}
              endDate={incomeTimelineKey === INCOME_CUSTOM_TIMELINE_KEY ? incomeCustomDateRange.end : ''}
              onApply={handleApplyIncomeCustomDateRange}
              onCancel={handleCancelIncomeCustomDateRange}
            />
          </div>
        ) : null}
      </div>
    </div>
  );

  const performanceTimelineFilterControl = (
    <div className="investments-filter-popover income-toolbar-timeline-popover" ref={performanceTimelineMenuRef}>
      <TimelineTrigger
        isOpen={isPerformanceTimelineMenuOpen}
        summary={performanceTimelineSummary}
        controls="performance-timeline-filter-panel"
        className="income-timeline-trigger"
        ariaLabel="Performance timeline"
        onClick={() => setIsPerformanceTimelineMenuOpen((previous) => !previous)}
      />

      <div
        id="performance-timeline-filter-panel"
        role="dialog"
        aria-label="Performance timeline filters"
        className={`investments-filter-panel income-timeline-panel timeline-range-panel ${isPerformanceTimelineMenuOpen ? 'is-open' : ''}`.trim()}
        aria-hidden={!isPerformanceTimelineMenuOpen}
      >
        <div className="income-timeline-menu-list" role="menu" aria-label="Performance timeline ranges">
          {INCOME_TIMELINE_PRESETS.map((preset) => {
            const isSelected = performanceTimelineKey === preset.key;
            return (
              <button
                key={preset.key}
                type="button"
                role="menuitemradio"
                aria-checked={isSelected}
                className={`income-timeline-menu-item ${isSelected ? 'is-selected' : ''}`.trim()}
                onClick={() => handleSelectPerformanceTimeline(preset.key)}
              >
                <span className="income-timeline-menu-item-copy">
                  <span className="income-timeline-menu-item-label">{preset.label}</span>
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
            aria-checked={isPerformanceCustomTimelineCommitted}
            className={`income-timeline-menu-item ${isPerformanceCustomTimelineSelected ? 'is-selected' : ''}`.trim()}
            onClick={() => handleSelectPerformanceTimeline(INCOME_CUSTOM_TIMELINE_KEY)}
          >
            <span className="income-timeline-menu-item-copy">
              <span className="income-timeline-menu-item-label">Custom Range</span>
            </span>
            <span className={`income-timeline-checkbox ${isPerformanceCustomTimelineCommitted ? 'is-selected' : ''}`.trim()} aria-hidden="true">
              {isPerformanceCustomTimelineCommitted ? <MdCheck size={14} /> : null}
            </span>
          </button>
        </div>

        {isPerformanceCustomTimelinePickerVisible ? (
          <div
            className="timeline-range-picker-popover is-open"
            aria-label="Custom performance timeline range"
          >
            <TimelineCustomRangePicker
              startDate={performanceTimelineKey === INCOME_CUSTOM_TIMELINE_KEY ? performanceCustomDateRange.start : ''}
              endDate={performanceTimelineKey === INCOME_CUSTOM_TIMELINE_KEY ? performanceCustomDateRange.end : ''}
              onApply={handleApplyPerformanceCustomDateRange}
              onCancel={handleCancelPerformanceCustomDateRange}
            />
          </div>
        ) : null}
      </div>
    </div>
  );

  const institutionMenuInstitutions = useMemo(
    () => localScopeInstitutions.map((institution) => {
      const accountIds = institution.accountIds;
      const selectedCount = accountIds.filter((accountId) => draftSelectedAccountIdSet.has(accountId)).length;
      const hasMultipleAccounts = accountIds.length > 1;
      const isAllSelected = accountIds.length > 0 && selectedCount === accountIds.length;
      const isPartiallySelected = selectedCount > 0 && !isAllSelected;
      return {
        ...institution,
        accountIds,
        selectedCount,
        hasMultipleAccounts,
        isAllSelected,
        isPartiallySelected,
        isSelected: selectedCount > 0,
      };
    }),
    [localScopeInstitutions, draftSelectedAccountIdSet]
  );

  const filteredAccounts = useMemo(
    () => investmentAccounts.filter((account) => selectedAccountIdSet.has(account.id)),
    [investmentAccounts, selectedAccountIdSet]
  );

  const currencyRatesToCad = useMemo(
    () => deriveCurrencyRatesToCad(filteredAccounts, holdingsByAccount),
    [filteredAccounts, holdingsByAccount]
  );

  const holdingsReady = filteredAccounts.every((account) =>
    Object.prototype.hasOwnProperty.call(holdingsByAccount, account.id)
  );

  // Shared holdings datasets for summary metrics, charts, and the combined table.
  const filteredHoldingsDataset = useMemo(
    () =>
      filteredAccounts.flatMap((account) => {
        const institutionKey = getInstitutionKey(account);

        return (holdingsByAccount[account.id] || []).map((holding) => {
          const holdingCategory = getHoldingCategory(holding);
          const isFuture = isFutureHolding(holding);
          const currency = getHoldingCurrency(holding, account.currency);
          const quantity = Number(holding.quantity) || 0;
          const marketValue = Number(holding.market_value) || 0;
          const quotedCostBasis = getHoldingCostBasis(holding);
          const contractMultiplier = getHoldingContractMultiplier(holding, holdingCategory);
          const totalCostBasis = getHoldingMoneyCostBasis(
            quotedCostBasis,
            holdingCategory,
            contractMultiplier,
            quantity
          );
          const fxRateToCad = getFxRateToCad(currency, currencyRatesToCad);
          const isCrypto = account.account_type === 'crypto';
          const averageCostPerUnitQuote = getHoldingAverageQuotePrice(quotedCostBasis, quantity);
          const lastPriceQuote = deriveHoldingQuoteLastPrice(
            holding,
            marketValue,
            quantity,
            contractMultiplier,
            holdingCategory
          );
          const changePct = holdingCategory === 'option' ? getHoldingChangePct(holding) : null;
          const dailyPnl = holdingCategory === 'option' ? getHoldingDailyPnl(holding) : null;
          // Futures: the connector reports BOTH market_value and average_cost as SIGNED totals
          // (negative for a short), so the cost basis is average_cost as-is — the per-position
          // sign must NOT be re-applied (`getHoldingMoneyCostBasis` would double-flip it, since
          // it expects an unsigned per-unit cost like options/stocks). Cost, current value and
          // P&L = market_value − average_cost then all read like any other short position.
          const positionTotalCostBasis = isFuture ? quotedCostBasis : totalCostBasis;
          // For AGGREGATIONS (totals, weight %) a future contributes only its mark-to-market
          // P&L (market − cost), not its notional — leverage isn't capital you hold. The
          // notional still shows in the row via marketValueCad/marketValue.
          const positionEquityValueCad = (isFuture && quotedCostBasis !== null
            ? marketValue - quotedCostBasis
            : marketValue) * fxRateToCad;

          return {
            ...holding,
            symbol: holding.symbol,
            name: holding.name || holding.symbol,
            canonicalSymbol: getCanonicalHoldingSymbol(holding.symbol),
            sectorLookupSymbol: getHoldingLookupSymbol(holding.symbol),
            instrumentKind: String(holding.instrument_kind || '').trim().toLowerCase() || null,
            currency,
            quantity,
            marketValue,
            quotedCostBasis,
            totalCostBasis: positionTotalCostBasis,
            fxRateToCad,
            marketValueCad: marketValue * fxRateToCad,
            equityValueCad: positionEquityValueCad,
            totalCostBasisCad: positionTotalCostBasis !== null ? positionTotalCostBasis * fxRateToCad : null,
            averageCostPerUnitQuote,
            lastPriceQuote,
            contractMultiplier,
            changePct,
            dailyPnl,
            accountId: account.id,
            accountName: account.name,
            accountType: account.account_type,
            accountTypeLabel: ACCOUNT_TYPE_LABELS[account.account_type] || account.account_type,
            accountCurrency: account.currency,
            institutionId: account.institution_id,
            institutionKey,
            institutionName: account.institution,
            holdingCategory,
            isCash: holdingCategory === 'cash',
            isOption: holdingCategory === 'option',
            isFuture,
            isCrypto,
          };
        });
      }),
    [currencyRatesToCad, filteredAccounts, holdingsByAccount]
  );

  const fundLikeSymbolFamilies = useMemo(
    () => new Set(
      filteredHoldingsDataset
        .filter((holding) => looksLikeEtfOrFund(holding.sectorLookupSymbol || holding.symbol, holding.name || holding.symbol))
        .map((holding) => getHoldingSymbolFamilyKey(holding.sectorLookupSymbol || holding.symbol))
    ),
    [filteredHoldingsDataset]
  );

  const enrichedHoldingsDataset = useMemo(
    () =>
      filteredHoldingsDataset.map((holding) => ({
        ...holding,
        hasBackendEtfOrFundInstrumentKind: hasBackendEtfOrFundInstrumentKind(holding.instrumentKind),
        isEtfLike:
          looksLikeEtfOrFund(holding.sectorLookupSymbol || holding.symbol, holding.name || holding.symbol)
          || fundLikeSymbolFamilies.has(getHoldingSymbolFamilyKey(holding.sectorLookupSymbol || holding.symbol)),
      })),
    [filteredHoldingsDataset, fundLikeSymbolFamilies]
  );

  const filteredAllPositionsDataset = useMemo(
    () => enrichedHoldingsDataset.filter((holding) => !holding.isCash),
    [enrichedHoldingsDataset]
  );

  const filteredCashHoldingsDataset = useMemo(
    () => enrichedHoldingsDataset.filter((holding) => holding.isCash),
    [enrichedHoldingsDataset]
  );

  const filteredSpotHoldingsDataset = useMemo(
    () => filteredAllPositionsDataset.filter((holding) => holding.holdingCategory === 'spot'),
    [filteredAllPositionsDataset]
  );

  const filteredCryptoHoldingsDataset = useMemo(
    () => filteredSpotHoldingsDataset.filter((holding) => isCryptoWalletHolding(holding)),
    [filteredSpotHoldingsDataset]
  );

  const eligibleInvestedPositionsDataset = useMemo(
    () => filteredSpotHoldingsDataset.filter((holding) => isEligibleInvestedPositionHolding(holding)),
    [filteredSpotHoldingsDataset]
  );

  const filteredPositionsHoldingsDataset = useMemo(
    () => eligibleInvestedPositionsDataset,
    [eligibleInvestedPositionsDataset]
  );

  const filteredOptionHoldingsDataset = useMemo(
    () => filteredAllPositionsDataset.filter((holding) => holding.holdingCategory === 'option'),
    [filteredAllPositionsDataset]
  );

  const combinedSpotHoldingsDataset = useMemo(
    () => combineHoldingsBySymbol(filteredPositionsHoldingsDataset),
    [filteredPositionsHoldingsDataset]
  );

  const combinedCryptoHoldingsDataset = useMemo(
    () => combineHoldingsBySymbol(filteredCryptoHoldingsDataset),
    [filteredCryptoHoldingsDataset]
  );

  const combinedOptionHoldingsDataset = useMemo(
    () => combineHoldingsBySymbol(filteredOptionHoldingsDataset),
    [filteredOptionHoldingsDataset]
  );

  const combinedTableDataset = useMemo(
    () => combinedSpotHoldingsDataset,
    [combinedSpotHoldingsDataset]
  );

  const investmentsTabsVisibility = useMemo(() => ({
    overview: true,
    holdings: combinedSpotHoldingsDataset.length > 0,
    income: true,
    performance: true,
    options: combinedOptionHoldingsDataset.length > 0,
    crypto: combinedCryptoHoldingsDataset.length > 0,
  }), [
    combinedSpotHoldingsDataset,
    combinedOptionHoldingsDataset,
    combinedCryptoHoldingsDataset,
  ]);

  const visibleInvestmentsTabs = useMemo(
    () => INVESTMENTS_TABS.filter((tab) => shouldShowInvestmentsTab(
      tab.id,
      activeTab,
      investmentsTabsVisibility,
      holdingsReady,
    )),
    [activeTab, holdingsReady, investmentsTabsVisibility]
  );

  useEffect(() => {
    if (shouldResetInvestmentsTab(activeTab, investmentsTabsVisibility, holdingsReady)) {
      let cancelled = false;
      Promise.resolve().then(() => {
        if (!cancelled) {
          setActiveTab('overview');
        }
      });
      return () => {
        cancelled = true;
      };
    }
    return undefined;
  }, [investmentsTabsVisibility, activeTab, holdingsReady]);

  const chartSourceDataset = useMemo(
    () => {
      if (isTourDemoActive()) {
        return buildTourDemoHoldingChartDataset();
      }

      return [...combinedTableDataset]
        .filter((holding) => Number.isFinite(holding.marketValueCad) && holding.marketValueCad > 0)
        .sort((left, right) => right.marketValueCad - left.marketValueCad);
    },
    [combinedTableDataset]
  );

  const allocationTotalValueCad = useMemo(
    () => chartSourceDataset.reduce((sum, holding) => sum + holding.marketValueCad, 0),
    [chartSourceDataset]
  );

  const accountBreakdownBySymbol = useMemo(
    () => buildAccountBreakdownBySymbol(filteredPositionsHoldingsDataset),
    [filteredPositionsHoldingsDataset]
  );

  const allocationChartData = useMemo(() => {
    if (allocationTotalValueCad <= 0) {
      return [];
    }

    const grouped = new Map();

    chartSourceDataset.forEach((holding) => {
      const canonicalSymbol = holding.canonicalSymbol || getCanonicalHoldingSymbol(holding.symbol);
      const existing = grouped.get(canonicalSymbol) || {
        id: canonicalSymbol,
        symbol: canonicalSymbol,
        name: holding.displayName || '',
        valueCad: 0,
      };

      existing.valueCad += holding.marketValueCad;

      if (!existing.name && holding.displayName) {
        existing.name = holding.displayName;
      }

      grouped.set(canonicalSymbol, existing);
    });

    const slices = Array.from(grouped.values())
      .sort((left, right) => right.valueCad - left.valueCad)
      .map((entry, index) => ({
        id: entry.id,
        symbol: entry.symbol,
        name: entry.name || null,
        value: fxToPrimary(entry.valueCad),
        sharePct: (entry.valueCad / allocationTotalValueCad) * 100,
        tooltipLabel: entry.symbol,
        tooltipSubLabel: entry.name || '',
        accountBreakdown: accountBreakdownBySymbol.get(getHoldingSymbolKey(entry.id)) || [],
        fill: holdingsPieChartColors[index % holdingsPieChartColors.length],
        rollupBreakdown: [{
          label: entry.symbol,
          value: fxToPrimary(entry.valueCad),
          sharePct: (entry.valueCad / allocationTotalValueCad) * 100,
          accountBreakdown: accountBreakdownBySymbol.get(getHoldingSymbolKey(entry.id)) || [],
        }],
      }));

    return rollupSmallPieSlices(slices, holdingsPieOtherFill);
  }, [accountBreakdownBySymbol, allocationTotalValueCad, chartSourceDataset, fxToPrimary, holdingsPieChartColors, holdingsPieOtherFill]);

  const topHoldingsBarData = useMemo(
    () => {
      if (isTourDemoActive()) {
        return TOUR_DEMO_TOP_HOLDINGS_ROWS.map((holding, index) => ({
          id: holding.id,
          symbol: holding.symbol,
          name: holding.name,
          value: fxToPrimary(holding.valueCad),
          accountBreakdown: [],
          fill: holdingsChartColors[index % holdingsChartColors.length],
        }));
      }

      return chartSourceDataset.map((holding, index) => ({
        id: holding.symbolKey,
        symbol: holding.displaySymbol || holding.symbol,
        name: holding.displayName,
        value: fxToPrimary(holding.marketValueCad),
        accountBreakdown: accountBreakdownBySymbol.get(holding.symbolKey) || [],
        fill: holdingsChartColors[index % holdingsChartColors.length],
      }));
    },
    [accountBreakdownBySymbol, chartSourceDataset, fxToPrimary, holdingsChartColors]
  );

  const gainersLosersChartData = useMemo(
    () => {
      if (isTourDemoActive()) {
        return TOUR_DEMO_GAINERS_LOSERS_ROWS.map((holding, index) => ({
          ...holding,
          accountBreakdown: [],
          metricValue: showGainersLosersByValue ? holding.gainLossValue : holding.gainLossPct,
          fill: holdingsChartColors[index % holdingsChartColors.length],
        }));
      }

      return combinedTableDataset
        .map((holding, index) => {
          const gainLossPct = Number(holding.gainLossPct);
          const gainLossCad = Number(holding.gainLossCad);
          const marketValueCad = Number(holding.marketValueCad);

          if (!Number.isFinite(gainLossPct) || !Number.isFinite(gainLossCad)) {
            return null;
          }

          const gainLossValue = fxToPrimary(gainLossCad);
          const metricValue = showGainersLosersByValue ? gainLossValue : gainLossPct;

          if (!Number.isFinite(metricValue) || Math.abs(metricValue) <= 0.0001) {
            return null;
          }

          return {
            id: holding.symbolKey,
            symbol: holding.displaySymbol || holding.symbol,
            name: holding.displayName,
            gainLossPct,
            gainLossValue,
            marketValue: Number.isFinite(marketValueCad) ? fxToPrimary(marketValueCad) : null,
            accountBreakdown: accountBreakdownBySymbol.get(holding.symbolKey) || [],
            metricValue,
            fill: holdingsChartColors[index % holdingsChartColors.length],
          };
        })
        .filter(Boolean);
    },
    [accountBreakdownBySymbol, combinedTableDataset, fxToPrimary, holdingsChartColors, showGainersLosersByValue]
  );

  const topUnrealizedGainersData = useMemo(
    () =>
      gainersLosersChartData
        .filter((holding) => holding.metricValue > 0)
        .sort((left, right) => right.metricValue - left.metricValue)
        .slice(0, GAINERS_LOSERS_MAX_GAINERS),
    [gainersLosersChartData]
  );

  const topUnrealizedLosersData = useMemo(
    () =>
      gainersLosersChartData
        .filter((holding) => holding.metricValue < 0)
        .sort((left, right) => left.metricValue - right.metricValue)
        .slice(0, GAINERS_LOSERS_MAX_LOSERS),
    [gainersLosersChartData]
  );

  const compositionSourceDataset = useMemo(
    () => {
      if (isTourDemoActive()) {
        return buildTourDemoHoldingChartDataset();
      }

      return [...eligibleInvestedPositionsDataset]
        .filter((holding) => Number.isFinite(holding.marketValueCad) && holding.marketValueCad > 0)
        .sort((left, right) => right.marketValueCad - left.marketValueCad);
    },
    [eligibleInvestedPositionsDataset]
  );

  const compositionTotalValueCad = useMemo(
    () => compositionSourceDataset.reduce((sum, holding) => sum + holding.marketValueCad, 0),
    [compositionSourceDataset]
  );

  const compositionChart = useMemo(() => {
    if (compositionTotalValueCad <= 0) {
      return { slices: [], groupCount: 0 };
    }

    const grouped = new Map();

    compositionSourceDataset.forEach((holding) => {
      const label = getHoldingCompositionGroupLabel(holding, compositionGroupMode);
      const existing = grouped.get(label) || {
        label,
        valueCad: 0,
        holdingCount: 0,
        tickers: new Set(),
        tickerValueCad: new Map(),
      };
      existing.valueCad += holding.marketValueCad;
      existing.holdingCount += 1;
      const canonicalSymbol = holding.canonicalSymbol || getCanonicalHoldingSymbol(holding.symbol);
      existing.tickers.add(canonicalSymbol);
      existing.tickerValueCad.set(canonicalSymbol, (existing.tickerValueCad.get(canonicalSymbol) || 0) + holding.marketValueCad);
      grouped.set(label, existing);
    });

    const sortedGroups = Array.from(grouped.values()).sort((left, right) => {
      if (right.valueCad !== left.valueCad) {
        return right.valueCad - left.valueCad;
      }
      return left.label.localeCompare(right.label, undefined, { sensitivity: 'base' });
    });

    const slices = sortedGroups.map((entry, index) => ({
        id: `${compositionGroupMode}-${entry.label}`,
        label: entry.label,
        shortLabel: truncatePieSliceLabel(entry.label, sortedGroups.length > 6 ? 12 : 18),
        value: fxToPrimary(entry.valueCad),
        sharePct: (entry.valueCad / compositionTotalValueCad) * 100,
        tooltipLabel: entry.label,
        tooltipTickers: Array.from(entry.tickers).sort((left, right) => left.localeCompare(right, undefined, {
          sensitivity: 'base',
          numeric: true,
        })),
        fill: holdingsPieChartColors[index % holdingsPieChartColors.length],
        rollupBreakdown: Array.from(entry.tickerValueCad.entries())
          .map(([label, valueCad]) => ({
            label,
            value: fxToPrimary(valueCad),
            sharePct: (valueCad / compositionTotalValueCad) * 100,
          }))
          .sort((left, right) => {
            if (right.sharePct !== left.sharePct) {
              return right.sharePct - left.sharePct;
            }
            return left.label.localeCompare(right.label, undefined, { sensitivity: 'base', numeric: true });
          }),
      }));

    return {
      groupCount: sortedGroups.length,
      slices: rollupSmallPieSlices(slices, holdingsPieOtherFill),
    };
  }, [compositionGroupMode, compositionSourceDataset, compositionTotalValueCad, fxToPrimary, holdingsPieChartColors, holdingsPieOtherFill]);

  const currentCompositionGroupMode = getCompositionGroupModeOption(compositionGroupMode);
  const currentCompositionModeIndex = HOLDINGS_COMPOSITION_GROUP_MODES.findIndex((mode) => mode.key === currentCompositionGroupMode.key);
  const previousCompositionGroupMode = HOLDINGS_COMPOSITION_GROUP_MODES[
    (currentCompositionModeIndex - 1 + HOLDINGS_COMPOSITION_GROUP_MODES.length) % HOLDINGS_COMPOSITION_GROUP_MODES.length
  ];
  const nextCompositionGroupMode = HOLDINGS_COMPOSITION_GROUP_MODES[
    (currentCompositionModeIndex + 1) % HOLDINGS_COMPOSITION_GROUP_MODES.length
  ];
  const cycleCompositionGroupMode = useCallback((direction) => {
    const currentIndex = HOLDINGS_COMPOSITION_GROUP_MODES.findIndex((mode) => mode.key === compositionGroupMode);
    const safeIndex = currentIndex >= 0 ? currentIndex : 0;
    const nextIndex = (safeIndex + direction + HOLDINGS_COMPOSITION_GROUP_MODES.length) % HOLDINGS_COMPOSITION_GROUP_MODES.length;
    setCompositionGroupMode(HOLDINGS_COMPOSITION_GROUP_MODES[nextIndex].key);
  }, [compositionGroupMode]);

  const childPositionsBySymbol = useMemo(
    () => buildChildPositionsBySymbol(filteredPositionsHoldingsDataset),
    [filteredPositionsHoldingsDataset]
  );

  const childCryptoPositionsBySymbol = useMemo(
    () => buildChildPositionsBySymbol(filteredCryptoHoldingsDataset),
    [filteredCryptoHoldingsDataset]
  );

  const childOptionPositionsBySymbol = useMemo(
    () => buildChildPositionsBySymbol(filteredOptionHoldingsDataset),
    [filteredOptionHoldingsDataset]
  );

  const sortedCombinedTableDataset = useMemo(() => {
    const nextRows = [...combinedTableDataset];
    const effectiveSort = holdingsTableSort || DEFAULT_HOLDINGS_TABLE_SORT;
    nextRows.sort((leftHolding, rightHolding) => compareHoldingsTableRows(leftHolding, rightHolding, effectiveSort));
    return nextRows;
  }, [combinedTableDataset, holdingsTableSort]);

  const sortedCombinedCryptoDataset = useMemo(() => {
    const nextRows = [...combinedCryptoHoldingsDataset];
    const effectiveSort = cryptoTableSort || DEFAULT_HOLDINGS_TABLE_SORT;
    nextRows.sort((leftHolding, rightHolding) => compareHoldingsTableRows(leftHolding, rightHolding, effectiveSort));
    return nextRows;
  }, [combinedCryptoHoldingsDataset, cryptoTableSort]);

  const sortedCombinedOptionDataset = useMemo(() => {
    const nextRows = [...combinedOptionHoldingsDataset];
    const effectiveSort = optionsTableSort || DEFAULT_HOLDINGS_TABLE_SORT;
    nextRows.sort((leftHolding, rightHolding) => compareHoldingsTableRows(leftHolding, rightHolding, effectiveSort));
    return nextRows;
  }, [combinedOptionHoldingsDataset, optionsTableSort]);

  const optionExpirationsByDayKey = useMemo(() => {
    const grouped = new Map();
    combinedOptionHoldingsDataset.forEach((holding) => {
      const expiry = parseOptionContractExpiry(holding.displaySymbol || holding.symbol);
      if (!expiry) return;
      const quantity = Number(holding.quantity) || 0;
      if (quantity === 0) return;
      const list = grouped.get(expiry.dayKey) || [];
      list.push({
        key: `${holding.symbolKey}-${expiry.dayKey}`,
        ticker: expiry.ticker,
        side: expiry.side,
        strike: expiry.strike,
        quantity,
        absQuantity: Math.abs(quantity),
        isShort: quantity < 0,
        displaySymbol: holding.displaySymbol || holding.symbol,
        year: expiry.year,
        monthIndex: expiry.monthIndex,
        day: expiry.day,
      });
      grouped.set(expiry.dayKey, list);
    });
    grouped.forEach((entries) => {
      entries.sort((leftEntry, rightEntry) => {
        if (leftEntry.isShort !== rightEntry.isShort) {
          return leftEntry.isShort ? 1 : -1;
        }
        const tickerCompare = String(leftEntry.ticker).localeCompare(String(rightEntry.ticker), undefined, { sensitivity: 'base' });
        if (tickerCompare !== 0) return tickerCompare;
        const strikeDiff = (Number(leftEntry.strike) || 0) - (Number(rightEntry.strike) || 0);
        if (Math.abs(strikeDiff) > 1e-6) return strikeDiff;
        return String(leftEntry.side).localeCompare(String(rightEntry.side));
      });
    });
    return grouped;
  }, [combinedOptionHoldingsDataset]);

  const optionsCalendarGrid = useMemo(() => {
    const { year, monthIndex } = optionsCalendarMonth;
    const firstOfMonth = new Date(year, monthIndex, 1);
    const startWeekday = firstOfMonth.getDay();
    const gridStart = new Date(year, monthIndex, 1 - startWeekday);
    const cells = [];
    const today = getAppNow();
    const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
    for (let cellIndex = 0; cellIndex < 42; cellIndex += 1) {
      const cellDate = new Date(gridStart);
      cellDate.setDate(gridStart.getDate() + cellIndex);
      const cellYear = cellDate.getFullYear();
      const cellMonth = cellDate.getMonth();
      const cellDay = cellDate.getDate();
      const dayKey = `${cellYear}-${String(cellMonth + 1).padStart(2, '0')}-${String(cellDay).padStart(2, '0')}`;
      cells.push({
        dayKey,
        day: cellDay,
        isOutsideMonth: cellMonth !== monthIndex,
        isToday: dayKey === todayKey,
        entries: optionExpirationsByDayKey.get(dayKey) || [],
      });
    }
    return cells;
  }, [optionExpirationsByDayKey, optionsCalendarMonth]);

  const optionsCalendarMonthLabel = useMemo(() => {
    const labelDate = new Date(optionsCalendarMonth.year, optionsCalendarMonth.monthIndex, 1);
    return labelDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
  }, [optionsCalendarMonth]);

  const handleOptionsCalendarStep = useCallback((direction) => {
    setOptionsCalendarMonth((previous) => {
      const total = previous.year * 12 + previous.monthIndex + (direction > 0 ? 1 : -1);
      return { year: Math.floor(total / 12), monthIndex: ((total % 12) + 12) % 12 };
    });
  }, []);

  const isOptionsCalendarCurrentMonth = useMemo(() => {
    const current = getCurrentOptionsCalendarMonth();
    return optionsCalendarMonth.year === current.year && optionsCalendarMonth.monthIndex === current.monthIndex;
  }, [optionsCalendarMonth]);

  const handleOptionsCalendarToday = useCallback(() => {
    setOptionsCalendarMonth(getCurrentOptionsCalendarMonth());
  }, []);


  // Summary metrics stay on the broader filtered positions dataset; the default positions table and pies use the stricter eligible invested-position scope.
  const investedValue = holdingsReady
    ? filteredAllPositionsDataset.reduce((sum, holding) => sum + holding.equityValueCad, 0)
    : null;
  const cashValue = holdingsReady
    ? filteredCashHoldingsDataset.reduce((sum, holding) => sum + holding.marketValueCad, 0)
    : null;
  const totalPortfolioValue = investedValue === null || cashValue === null
    ? null
    : investedValue + cashValue;
  const computedUnrealizedGainLoss = holdingsReady
    ? [...combinedTableDataset, ...combinedCryptoHoldingsDataset, ...combinedOptionHoldingsDataset].reduce((sum, holding) => sum + (holding.gainLossCad ?? 0), 0)
    : null;
  const unrealizedGainLoss = isTourDemoActive() && holdingsReady
    ? TOUR_DEMO_UNREALIZED_GAIN_LOSS_CAD
    : computedUnrealizedGainLoss;

  const totalPortfolioDisplay = totalPortfolioValue === null ? 'Loading' : maskMoney(formatMoneyNarrow(fxToPrimary(totalPortfolioValue), primaryCurrency));
  const investedValueDisplay = investedValue === null ? 'Loading' : maskMoney(formatMoneyNarrow(fxToPrimary(investedValue), primaryCurrency));
  const cashDisplay = cashValue === null ? 'Loading' : maskMoney(formatMoneyNarrow(fxToPrimary(cashValue), primaryCurrency));
  const unrealizedDisplay = unrealizedGainLoss === null ? 'Loading' : maskMoney(formatMoneyNarrow(fxToPrimary(unrealizedGainLoss), primaryCurrency));
  const totalPortfolioCompact = totalPortfolioValue === null ? 'Loading' : maskMoney(formatCompactMoney(fxToPrimary(totalPortfolioValue), primaryCurrency));
  const investedValueCompact = investedValue === null ? 'Loading' : maskMoney(formatCompactMoney(fxToPrimary(investedValue), primaryCurrency));
  const cashCompact = cashValue === null ? 'Loading' : maskMoney(formatCompactMoney(fxToPrimary(cashValue), primaryCurrency));
  const unrealizedCompact = unrealizedGainLoss === null ? 'Loading' : maskMoney(formatCompactMoney(fxToPrimary(unrealizedGainLoss), primaryCurrency));
  const allDraftAccountsSelected = allScopeSourcesSelected;
  const combinedPositionsMeta = holdingsReady
    ? `${combinedTableDataset.length} Positions`
    : 'Loading holdings';
  const combinedOptionsMeta = holdingsReady
    ? `${combinedOptionHoldingsDataset.length} Positions`
    : 'Loading holdings';
  const combinedCryptoMeta = holdingsReady
    ? `${combinedCryptoHoldingsDataset.length} Positions`
    : 'Loading holdings';
  const positionsChartsEmptyMessage = filteredAccounts.length === 0
    ? 'No investment accounts are included in this view.'
    : !holdingsReady
      ? 'Loading holdings...'
      : combinedTableDataset.length === 0
        ? 'No eligible invested positions are available for the selected institutions.'
        : 'No positive market-value holdings are available for charting.';
  const gainersLosersEmptyMessage = filteredAccounts.length === 0
    ? 'No investment accounts are included in this view.'
    : !holdingsReady
      ? 'Loading holdings...'
      : combinedTableDataset.length === 0
        ? 'No eligible invested positions are available for the selected institutions.'
        : 'No unrealized P&L data is available for the selected positions.';
  const cashBalanceRows = useMemo(() => {
    const groupedRows = new Map();

    filteredCashHoldingsDataset.forEach((holding) => {
      const currency = holding.currency || holding.accountCurrency || 'CAD';
      const groupKey = `${holding.accountId}:${currency}`;

      if (!groupedRows.has(groupKey)) {
        groupedRows.set(groupKey, {
          key: groupKey,
          accountId: holding.accountId,
          institutionName: holding.institutionName || 'Institution',
          accountName: holding.accountName || 'Account',
          accountType: holding.accountType || 'other',
          accountTypeLabel: holding.accountTypeLabel || '',
          currency,
          nativeAmount: 0,
          valueCad: 0,
        });
      }

      const groupedRow = groupedRows.get(groupKey);
      groupedRow.nativeAmount += Number(holding.marketValue) || 0;
      groupedRow.valueCad += Number(holding.marketValueCad) || 0;
    });

    return Array.from(groupedRows.values());
  }, [filteredCashHoldingsDataset]);
  const cashAccountRows = useMemo(() => {
    const groupedAccounts = new Map();

    cashBalanceRows.forEach((balanceRow) => {
      const accountKey = balanceRow.accountId || `${balanceRow.institutionName}:${balanceRow.accountName}`;

      if (!groupedAccounts.has(accountKey)) {
        groupedAccounts.set(accountKey, {
          key: accountKey,
          institutionName: balanceRow.institutionName,
          accountName: balanceRow.accountName,
          accountType: balanceRow.accountType,
          accountTypeLabel: balanceRow.accountTypeLabel,
          valueCad: 0,
          balances: [],
        });
      }

      const groupedAccount = groupedAccounts.get(accountKey);
      groupedAccount.valueCad += balanceRow.valueCad;
      groupedAccount.balances.push(balanceRow);
    });

    return Array.from(groupedAccounts.values())
      .map((accountRow) => {
        const balances = accountRow.balances.sort((leftRow, rightRow) => {
          const valueDiff = Math.abs(rightRow.valueCad) - Math.abs(leftRow.valueCad);
          if (Math.abs(valueDiff) > 0.0001) {
            return valueDiff;
          }
          return leftRow.currency.localeCompare(rightRow.currency, undefined, {
            sensitivity: 'base',
            numeric: true,
          });
        });

        return {
          ...accountRow,
          balances,
        };
      })
      .sort((leftRow, rightRow) => {
        const valueDiff = rightRow.valueCad - leftRow.valueCad;
        if (Math.abs(valueDiff) > 0.0001) {
          return valueDiff;
        }
        return `${leftRow.institutionName} ${leftRow.accountName}`.localeCompare(
          `${rightRow.institutionName} ${rightRow.accountName}`,
          undefined,
          { sensitivity: 'base', numeric: true }
        );
      });
  }, [cashBalanceRows]);
  const institutionFilterSummary = formatScopeSelectionSummary({
    totalInstitutions: localScopeInstitutions.length,
    selectedInstitutions: institutionMenuInstitutions.filter((institution) => institution.isSelected).length,
    selectedAccounts: selectedScopeAccountIds.length,
  });
  const topSummaryMetrics = useMemo(
    () => ([
      {
        id: 'total-portfolio',
        label: 'Total Portfolio',
        value: totalPortfolioDisplay,
        valueCompact: totalPortfolioCompact,
        tone: totalPortfolioValue === null ? '' : totalPortfolioValue > 0 ? 'is-positive' : totalPortfolioValue < 0 ? 'is-negative' : '',
      },
      {
        id: 'invested-value',
        label: 'Invested Value',
        value: investedValueDisplay,
        valueCompact: investedValueCompact,
        tone: investedValue === null ? '' : investedValue > 0 ? 'is-positive' : investedValue < 0 ? 'is-negative' : '',
      },
      {
        id: 'cash',
        label: 'Cash',
        value: cashDisplay,
        valueCompact: cashCompact,
        tone: cashValue === null ? '' : cashValue > 0 ? 'is-positive' : cashValue < 0 ? 'is-negative' : '',
      },
      {
        id: 'unrealized-gain-loss',
        label: 'Unrealized Gain/Loss',
        value: unrealizedDisplay,
        valueCompact: unrealizedCompact,
        tone: unrealizedGainLoss === null ? '' : unrealizedGainLoss > 0 ? 'is-positive' : unrealizedGainLoss < 0 ? 'is-negative' : '',
      },
    ]),
    [
      cashDisplay,
      cashValue,
      investedValueDisplay,
      investedValue,
      totalPortfolioDisplay,
      totalPortfolioValue,
      unrealizedDisplay,
      unrealizedGainLoss,
      totalPortfolioCompact,
      investedValueCompact,
      cashCompact,
      unrealizedCompact,
    ]
  );
  const isIncomeSummaryPending = activeTab === 'income'
    && incomeLoading
    && !Array.isArray(incomeTransactions);
  const isIncomeSummaryUnavailable = activeTab === 'income'
    && Boolean(incomeError)
    && !Array.isArray(incomeTransactions);
  const incomeTopSummaryMetrics = useMemo(() => {
    if (isIncomeSummaryUnavailable) return [];

    const formatIncomeMetric = (value) => ({
      value: isIncomeSummaryPending ? 'Loading' : maskMoney(formatMoneyNarrow(value, primaryCurrency)),
      valueCompact: isIncomeSummaryPending ? '' : maskMoney(formatCompactMoney(value, primaryCurrency)),
    });

    if (incomeSubTab === 'interest') {
      return [
        {
          id: 'interest-earned',
          label: 'Interest Earned',
          ...formatIncomeMetric(interestSummary.earned),
          tone: isIncomeSummaryPending ? '' : 'is-positive',
        },
        {
          id: 'interest-paid',
          label: 'Interest Paid',
          ...formatIncomeMetric(interestSummary.paid),
          tone: isIncomeSummaryPending ? '' : 'is-negative',
        },
        {
          id: 'net-interest',
          label: 'Net Interest',
          ...formatIncomeMetric(interestSummary.net),
          tone: isIncomeSummaryPending ? '' : interestSummary.net >= 0 ? 'is-positive' : 'is-negative',
        },
      ];
    }

    return [
      {
        id: 'total-dividend-income',
        label: 'Total Dividend Income',
        ...formatIncomeMetric(incomeSummary.totalDividends),
        tone: isIncomeSummaryPending ? '' : 'is-positive',
      },
      {
        id: 'withholding-tax-paid',
        label: 'Withholding Tax Paid',
        ...formatIncomeMetric(incomeSummary.totalWithholding),
        tone: isIncomeSummaryPending ? '' : 'is-withholding',
      },
      {
        id: 'avg-monthly-dividends',
        label: 'Avg Monthly Dividends',
        ...formatIncomeMetric(incomeSummary.avgMonthly),
        tone: '',
      },
    ];
  }, [
    incomeSubTab,
    incomeSummary.avgMonthly,
    incomeSummary.totalDividends,
    incomeSummary.totalWithholding,
    interestSummary.earned,
    interestSummary.net,
    interestSummary.paid,
    isIncomeSummaryPending,
    isIncomeSummaryUnavailable,
    maskMoney,
    primaryCurrency,
  ]);
  const performanceChartData = useMemo(
    () => (Array.isArray(performanceData?.series) ? performanceData.series : []),
    [performanceData]
  );
  const performanceBenchmarkOptions = useMemo(
    () => (performanceData?.benchmarks || [])
      .filter((benchmark) => benchmark?.as_of_date)
      .map((benchmark, index) => ({
        ...benchmark,
        color: performanceLineColors[benchmark.id] || holdingsChartColors[index % holdingsChartColors.length],
        dataKey: `${benchmark.id}_return_pct`,
        isVisible: visiblePerformanceBenchmarkIds[benchmark.id] !== false,
      })),
    [holdingsChartColors, performanceData, performanceLineColors, visiblePerformanceBenchmarkIds]
  );
  const performanceBenchmarkDataNote = useMemo(() => {
    if (performanceData?.market_data?.mode !== 'keyless') return '';
    const asOfDates = (performanceData?.benchmarks || [])
      .filter((benchmark) => benchmark?.provider === 'fred' && benchmark?.as_of_date)
      .map((benchmark) => benchmark.as_of_date)
      .sort();
    if (!asOfDates.length) return '';
    const asOfDate = asOfDates[0];
    return `Benchmark data through EOD ${formatShortDateLabel(asOfDate)}. Without a market-data API key, default benchmark data is end-of-day only and may lag today's portfolio return.`;
  }, [performanceData]);
  const performanceSummary = useMemo(
    () => performanceData?.summary || {},
    [performanceData]
  );
  const togglePerformanceBenchmark = useCallback((benchmarkId) => {
    setVisiblePerformanceBenchmarkIds((current) => ({
      ...current,
      [benchmarkId]: current[benchmarkId] === false,
    }));
  }, []);
  const isPerformanceSummaryPending = activeTab === 'performance' && performanceLoading && !performanceData;
  const performanceTopSummaryMetrics = useMemo(() => {
    const sourceCurrency = performanceData?.currency || 'CAD';
    const formatPerformanceMoney = (value, signed = false, compact = false) => {
      if (isPerformanceSummaryPending) return 'Loading';
      if (!Number.isFinite(Number(value))) {
        if (signed) {
          return maskMoney(compact ? formatSignedCompactMoney(value, primaryCurrency) : formatSignedMoneyNarrow(value, primaryCurrency));
        }
        return maskMoney(compact ? formatCompactMoney(value, primaryCurrency) : formatMoneyNarrow(value, primaryCurrency));
      }
      const convertedValue = convertToPrimary(Number(value), sourceCurrency);
      return maskMoney(
        signed
          ? (compact ? formatSignedCompactMoney(convertedValue, primaryCurrency) : formatSignedMoneyNarrow(convertedValue, primaryCurrency))
          : (compact ? formatCompactMoney(convertedValue, primaryCurrency) : formatMoneyNarrow(convertedValue, primaryCurrency)),
      );
    };
    const formatPerformancePercent = (value) => (
      isPerformanceSummaryPending ? 'Loading' : formatSignedPercent(value)
    );

    return [
      {
        id: 'performance-value',
        label: 'Portfolio Value',
        value: formatPerformanceMoney(performanceSummary.end_value),
        valueCompact: formatPerformanceMoney(performanceSummary.end_value, false, true),
        tone: performanceSummary.end_value > 0 ? 'is-positive' : performanceSummary.end_value < 0 ? 'is-negative' : '',
      },
      {
        id: 'performance-return',
        label: 'Portfolio Return',
        value: formatPerformancePercent(performanceSummary.return_pct),
        tone: performanceSummary.return_pct > 0 ? 'is-positive' : performanceSummary.return_pct < 0 ? 'is-negative' : '',
      },
      {
        id: 'performance-change',
        label: 'Adjusted Gain/Loss',
        value: formatPerformanceMoney(performanceSummary.adjusted_change, true),
        valueCompact: formatPerformanceMoney(performanceSummary.adjusted_change, true, true),
        tone: performanceSummary.adjusted_change > 0 ? 'is-positive' : performanceSummary.adjusted_change < 0 ? 'is-negative' : '',
      },
    ];
  }, [
    convertToPrimary,
    isPerformanceSummaryPending,
    maskMoney,
    performanceData?.currency,
    performanceSummary.adjusted_change,
    performanceSummary.end_value,
    performanceSummary.return_pct,
    primaryCurrency,
  ]);
  const activeTopSummaryMetrics = activeTab === 'performance'
    ? performanceTopSummaryMetrics
    : activeTab === 'income'
      ? incomeTopSummaryMetrics
      : activeTab === 'overview'
        ? topSummaryMetrics
        : [];
  const showTopSummaryMetrics = activeTopSummaryMetrics.length > 0;
  const topSummaryLabel = activeTab === 'income'
    ? 'Income summary'
    : activeTab === 'performance'
      ? 'Performance summary'
      : 'Investments summary';

  const handleToggleInstitution = (institution) => {
    const accountIds = institution.accountIds;

    if (accountIds.length === 0) {
      return;
    }

    setLocalScopeInstitutions((previous) => previous.map((inst) => {
      if (inst.id !== institution.id) {
        return inst;
      }

      const accountsForInstitution = inst.accounts || [];
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

  const handleToggleAllInstitutions = () => {
    const shouldHideAll = allScopeSourcesSelected;
    setLocalScopeInstitutions((previous) => previous.map((inst) => ({
      ...inst,
      hidden: shouldHideAll,
      accounts: (inst.accounts || []).map((account) => ({
        ...account,
        hidden: shouldHideAll,
      })),
    })));
  };

  const handleApplyInstitutionSelection = async () => {
    setScopeSaveError('');
    setScopeSaving(true);

    try {
      await persistVisibilityScope({
        apiBase: API,
        institutions: localScopeInstitutions,
        previousInstitutions: holdingsScopeDraftRef.current,
      });
      holdingsScopeDraftRef.current = null;
      setIsInstitutionMenuOpen(false);
      if (onDataChange) Promise.resolve(onDataChange()).catch(() => {});
      if (fetchAllScopeInstitutions) Promise.resolve(fetchAllScopeInstitutions()).catch(() => {});
    } catch (err) {
      console.error('Failed to save investments visibility:', err);
      setScopeSaveError(err?.message || 'Could not save the selected scope.');
      const authoritativeInstitutions = await reconcileVisibilityScope({
        onDataChange,
        fetchAllScopeInstitutions,
      });
      if (authoritativeInstitutions) {
        const investmentEligible = getInvestmentScopeInstitutions(authoritativeInstitutions);
        setLocalScopeInstitutions(cloneScopeInstitutions(investmentEligible));
        holdingsScopeDraftRef.current = cloneScopeInstitutions(investmentEligible);
      }
    } finally {
      setScopeSaving(false);
    }
  };

  const handleToggleInstitutionAccount = (accountId) => {
    const isCurrentlySelected = selectedScopeAccountIdSet.has(accountId);

    setLocalScopeInstitutions((previous) => previous.map((inst) => {
      const accountsForInstitution = inst.accounts || [];
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

  const handleSortColumn = (columnId, tabId) => {
    const column = HOLDINGS_TABLE_COLUMNS.find((candidate) => candidate.id === columnId);

    if (!column || !column.sortable) {
      return;
    }

    const setter = tabId === 'options'
      ? setOptionsTableSort
      : tabId === 'crypto'
        ? setCryptoTableSort
        : setHoldingsTableSort;

    setter((previous) => (
      getNextSortConfig(previous, columnId, HOLDINGS_TABLE_SORT_DEFAULT_DIRECTIONS)
    ));
  };

  const closePositionDetailDrawer = useCallback(() => {
    setSelectedPositionDetail(null);
  }, []);

  // Clicks on any holdings-summary-row switch or toggle the detail, rather
  // than dismissing from the outside-click layer.
  const handlePositionDetailDismiss = useCallback((event) => {
    if (event.target?.closest?.('.holdings-summary-row, .holdings-metrics-scroll-controller')) {
      return;
    }
    closePositionDetailDrawer();
  }, [closePositionDetailDrawer]);

  useDismissibleLayer({
    open: isPositionDetailDrawerVisible,
    ref: positionDetailTrayShellRef,
    ignoreAppChrome: true,
    onDismiss: handlePositionDetailDismiss,
  });

  // Position-detail tray no longer needs anchor measurement —
  // position:fixed at a stable viewport top means the open position
  // doesn't depend on the clicked row's geometry.

  const institutionFilterControl = (
    <div className="investments-filter-popover" ref={institutionMenuRef}>
      <ScopeSelectorTrigger
        isOpen={isInstitutionMenuOpen}
        summary={institutionFilterSummary}
        controls="investments-institution-filter-panel"
        className={hasHiddenInstitutions ? 'has-hidden-sources' : ''}
        onClick={() => (isInstitutionMenuOpen ? closeInstitutionMenu() : openInstitutionMenu())}
      />

      <div
        id="investments-institution-filter-panel"
        role="dialog"
        aria-label="Scope filters"
        className={`investments-filter-panel scope-selector-panel ${isInstitutionMenuOpen ? 'is-open' : ''}`.trim()}
        aria-hidden={!isInstitutionMenuOpen}
      >
        <InstitutionAccountSelector
          key={isInstitutionMenuOpen ? 'open' : 'closed'}
          institutions={institutionMenuInstitutions}
          selectedAccountIds={draftSelectedAccountIdSet}
          onToggleInstitution={handleToggleInstitution}
          onToggleAccount={handleToggleInstitutionAccount}
          headerActions={(
            <>
              <button
                type="button"
                className="institution-filter-toggle-all app-control-root"
                onClick={handleToggleAllInstitutions}
              >
                <span className="app-control-label">{allDraftAccountsSelected ? 'All Off' : 'All On'}</span>
              </button>
              <button
                type="button"
                className="btn-primary scope-filter-apply-btn app-control-root"
                onClick={handleApplyInstitutionSelection}
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

  // Per-sub-tab export: each tab exports only its own table (Performance has
  // nothing to export). Every export honors the active account/institution filter
  // (and Income also the timeline) so the file matches the on-screen table.
  // Holdings tab includes the in-account cash table alongside spot positions.
  const exportAccountScope = appliedAccountIds.length
    ? { account_ids: appliedAccountIds.join(',') }
    : {};
  const holdingsExportConfig = {
    overview: { label: 'Export all holdings', category: null },
    holdings: { label: 'Export holdings', category: 'spot,cash' },
    options: { label: 'Export options', category: 'option' },
    crypto: { label: 'Export crypto', category: 'crypto' },
  }[activeTab];
  const investmentsExportButton = activeTab === 'income'
    ? (
      <CsvExportButton
        dataset="income"
        label={incomeSubTab === 'interest' ? 'Export interest' : 'Export dividends'}
        portal={false}
        getParams={() => ({
          kind: incomeSubTab,
          ...exportAccountScope,
          ...(incomeTimelineBounds.startDate ? { start_date: incomeTimelineBounds.startDate } : {}),
          ...(incomeTimelineBounds.endDate ? { end_date: incomeTimelineBounds.endDate } : {}),
        })}
      />
    )
    : holdingsExportConfig
      ? (
        <CsvExportButton
          dataset="holdings"
          label={holdingsExportConfig.label}
          portal={false}
          getParams={() => ({
            ...(holdingsExportConfig.category ? { category: holdingsExportConfig.category } : {}),
            ...exportAccountScope,
          })}
        />
      )
      : null;
  const activeTimelineFilterControl = activeTab === 'income'
    ? incomeTimelineFilterControl
    : activeTab === 'performance'
      ? performanceTimelineFilterControl
      : null;
  const incomeModeToggleElement = (
    <div className="income-mode-toggle" role="group" aria-label="Income type">
      {INCOME_SUBTAB_OPTIONS.map((option) => {
        const isSelected = incomeSubTab === option.key;
        return (
          <button
            key={option.key}
            type="button"
            aria-pressed={isSelected}
            className={`income-mode-option app-control-root ${isSelected ? 'is-active' : ''}`.trim()}
            onClick={() => setIncomeSubTab(option.key)}
          >
            <span className="income-mode-option-label app-control-label">{option.label}</span>
          </button>
        );
      })}
    </div>
  );
  const investmentsFilterControls = (
    <div className="app-page-controls investments-page-controls investments-toolbar-controls" aria-label="Investments view controls">
      <div className="app-toolbar-visibility">
        <BalancesToggleButton className="page-top-balance-toggle" />
      </div>
      <div className="app-toolbar-export">
        <div className="app-toolbar-export-slot" aria-label="Export">
          {investmentsExportButton}
        </div>
      </div>
      <AppViewFiltersMenu
        id="investments-view-filters"
        ariaLabel="Investments view and filters"
        rows={[
          { key: 'accounts', label: 'Accounts', control: institutionFilterControl },
          { key: 'period', label: 'Period', control: activeTimelineFilterControl, hidden: !activeTimelineFilterControl },
          {
            key: 'currency',
            label: 'Currency',
            control: (
              <CurrencyViewPicker
                value={primaryCurrency}
                options={SELECTABLE_CURRENCIES}
                onChange={setPrimaryCurrency}
                panelId="investments-view-currency-panel"
              />
            ),
          },
        ]}
      />
    </div>
  );
  const usesShellToolbar = Boolean(showPageTitle && investmentsFiltersSlot);
  // Sub-tab nav is decoupled from the filters slot: sub-tabs stay in
  // the shell toolbar whenever its target DOM node exists.
  const usesShellNavSlot = Boolean(showPageTitle && investmentsNavSlot);
  const investmentsPrimaryNav = (
    <div className="investments-subnav app-section-nav" role="tablist" aria-label="Investments sections">
      {visibleInvestmentsTabs.map((tab) => (
        <React.Fragment key={tab.id}>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-disabled={!tab.isEnabled}
            aria-controls={activeTab === tab.id ? `investments-panel-${tab.id}` : undefined}
            tabIndex={activeTab === tab.id ? 0 : -1}
            disabled={!tab.isEnabled}
            className={`investments-subnav-tab app-section-nav-tab ${activeTab === tab.id ? 'is-active' : ''} ${tab.isEnabled ? 'is-interactive' : 'is-placeholder'}`.trim()}
            onClick={() => {
              if (tab.isEnabled) {
                setActiveTab(tab.id);
              }
            }}
          >
            <span className="investments-subnav-tab-label">{tab.label}</span>
          </button>
          {tab.id === 'income' && activeTab === 'income' ? (
            <div className="investments-subnav-child-nav" role="presentation">
              {incomeModeToggleElement}
            </div>
          ) : null}
        </React.Fragment>
      ))}
    </div>
  );
  const tableMoney = (value, currency) => {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
    return (
      <FitMoney
        full={maskMoney(formatTableMoney(value, currency))}
        compact={maskMoney(formatCompactMoney(value, currency))}
        className="holdings-cell-money"
      />
    );
  };

  const renderPositionMoney = (nativeValue, cadValue, currency) => {
    if (currency === 'Mixed') {
      return Number.isFinite(Number(cadValue)) ? tableMoney(fxToPrimary(Number(cadValue)), primaryCurrency) : '—';
    }
    return tableMoney(nativeValue, currency);
  };

  const getPositionToneClass = (value) => {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
      return '';
    }

    return Number(value) >= 0 ? 'gain' : 'loss';
  };

  const renderPositionDetailDrawerContent = (detail) => {
    if (!detail?.holding) {
      return null;
    }

    const { holding, childPositions } = detail;
    const hasMixedCurrency = holding.displayCurrency === 'Mixed';
    const summaryItems = [
      {
        label: 'Cost Basis',
        value: renderPositionMoney(holding.totalCostBasis, holding.totalCostBasisCad, holding.displayCurrency),
      },
      {
        label: 'Current Value',
        value: renderPositionMoney(holding.marketValue, holding.marketValueCad, holding.displayCurrency),
      },
      {
        label: 'Unrlzd P&L %',
        value: holding.gainLossPct === null ? '—' : formatPercent(holding.gainLossPct),
        tone: getPositionToneClass(holding.gainLossPct),
      },
      {
        label: 'Unrlzd P&L',
        value: renderPositionMoney(holding.gainLoss, holding.gainLossCad, holding.displayCurrency),
        tone: getPositionToneClass(holding.gainLoss ?? holding.gainLossCad),
      },
      {
        label: 'Average Cost',
        value: holding.averageCostPerUnit === null || hasMixedCurrency
          ? '—'
          : maskMoney(formatTableMoney(holding.averageCostPerUnit, holding.displayCurrency)),
      },
      {
        label: 'Last Price',
        value: holding.lastPrice === null || hasMixedCurrency
          ? '—'
          : maskMoney(formatTableMoney(holding.lastPrice, holding.displayCurrency)),
      },
    ];

    return (
      <div className="position-detail-drawer-content">
        <div className="position-detail-summary-grid">
          {summaryItems.map((item) => (
            <div key={item.label} className="position-detail-summary-item">
              <span className="position-detail-summary-label">{item.label}</span>
              <strong className={`position-detail-summary-value ${item.tone ? `is-${item.tone}` : ''}`.trim()}>
                {item.value}
              </strong>
            </div>
          ))}
        </div>

        <section className="position-detail-section">
          <h4 className="position-detail-section-title">Accounts</h4>
          <div className="position-detail-account-list">
            {childPositions.map((position) => {
              const positionGainLossToneClass = getPositionToneClass(position.gainLoss);
              const positionGainLossPctToneClass = getPositionToneClass(position.gainLossPct);

              return (
                <div key={position.id} className="position-detail-account-row">
                  <div className="position-detail-account-heading">
                    <div className="position-detail-account-name">
                      <InstitutionLogo name={position.institutionName} size={16} />
                      <span>{position.accountName}</span>
                    </div>
                    <AccountTypeBadge
                      accountType={position.accountType}
                      label={position.accountTypeLabel}
                      className="holdings-account-type-badge"
                    />
                  </div>
                  <div className="position-detail-account-metrics">
                    <span>
                      <span className="position-detail-metric-label">Cost Basis</span>
                      <strong>{tableMoney(position.totalCostBasis, position.currency)}</strong>
                    </span>
                    <span>
                      <span className="position-detail-metric-label">Current Value</span>
                      <strong>{tableMoney(position.marketValue, position.currency)}</strong>
                    </span>
                    <span>
                      <span className="position-detail-metric-label">Unrlzd P&L %</span>
                      <strong className={positionGainLossPctToneClass ? `is-${positionGainLossPctToneClass}` : ''}>
                        {position.gainLossPct === null ? '—' : formatPercent(position.gainLossPct)}
                      </strong>
                    </span>
                    <span>
                      <span className="position-detail-metric-label">Unrlzd P&L</span>
                      <strong className={positionGainLossToneClass ? `is-${positionGainLossToneClass}` : ''}>
                        {tableMoney(position.gainLoss, position.currency)}
                      </strong>
                    </span>
                    <span>
                      <span className="position-detail-metric-label">Average Cost</span>
                      <strong>{tableMoney(position.averageCostPerUnit, position.currency)}</strong>
                    </span>
                    <span>
                      <span className="position-detail-metric-label">Qty</span>
                      <strong>{formatQuantity(position.quantity)}</strong>
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      </div>
    );
  };

  const renderCombinedTable = ({
    rows,
    childRowsBySymbol,
    emptyMessage,
    rowControlPrefix,
    tabId,
    tableLabel,
  }) => {
    if (filteredAccounts.length === 0) {
      return <p className="no-data">No investment accounts are included in this view.</p>;
    }

    if (!holdingsReady) {
      return <p className="no-data">Loading holdings...</p>;
    }

    if (rows.length === 0) {
      return <p className="no-data">{emptyMessage}</p>;
    }

    const activeTableSort = tabId === 'options'
      ? optionsTableSort
      : tabId === 'crypto'
        ? cryptoTableSort
        : holdingsTableSort;

    const totalMarketValueCad = rows.reduce((sum, holding) => (
      sum + (Number.isFinite(Number(holding.equityValueCad)) ? Number(holding.equityValueCad) : 0)
    ), 0);
    const totalMarketValue = fxToPrimary(totalMarketValueCad);
    const renderHeaderContent = (column) => {
      if (!column.sortable) {
        return <span className={`holdings-static-header is-${column.align}`.trim()}>{column.label}</span>;
      }

      return (
        <SortableTableHeader
          label={column.label}
          sortKey={column.id}
          sortConfig={activeTableSort}
          onSort={() => handleSortColumn(column.id, tabId)}
          defaultDirections={HOLDINGS_TABLE_SORT_DEFAULT_DIRECTIONS}
          align={column.align}
        />
      );
    };
    const renderHeaderCell = (column, columnIndex) => (
      <div
        key={column.id}
        className={`holdings-table-header-cell is-${column.align}`.trim()}
        role="columnheader"
        aria-colindex={columnIndex + 1}
      >
        {renderHeaderContent(column)}
      </div>
    );
    const renderSecurityCellContent = (holding) => (
      <div className="holdings-security-cell">
        <div className="holdings-name-cell">
          <span className="holdings-name-primary">{holding.displaySymbol || holding.symbol}</span>
          {holding.displayName && (
            <span className="holdings-name-secondary">{holding.displayName}</span>
          )}
        </div>
      </div>
    );
    const getInteractiveRowClassName = (rowDetailKey, isSelected) => (
      `holdings-parent-row holdings-summary-row ${isSelected ? 'is-selected' : ''}`.trim()
    );
    const getSharedRowHandlers = (openPositionDetail, handlePositionRowKeyDown) => ({
      tabIndex: 0,
      onMouseEnter: (event) => {
        updateCombinedTableHoverPointer(event);
        syncCombinedTableRowHighlight(event.currentTarget, 'hover');
      },
      onMouseMove: (event) => {
        updateCombinedTableHoverPointer(event);
      },
      onMouseLeave: (event) => {
        clearCombinedTableHoverPointer();
        clearCombinedTableHoverHighlight(event.currentTarget.closest('.holdings-combined-table-frame'));
      },
      onFocus: (event) => {
        syncCombinedTableRowHighlight(event.currentTarget, 'hover');
      },
      onBlur: (event) => {
        clearCombinedTableHoverHighlight(event.currentTarget.closest('.holdings-combined-table-frame'));
      },
      onClick: (event) => {
        openPositionDetail(event);
        clearCombinedTableHoverHighlight(event.currentTarget.closest('.holdings-combined-table-frame'));
      },
      onKeyDown: (event) => {
        const isActivation = event.key === 'Enter' || event.key === ' ';
        handlePositionRowKeyDown(event);
        if (isActivation) {
          clearCombinedTableHoverHighlight(event.currentTarget.closest('.holdings-combined-table-frame'));
        }
      },
    });
    const tableRows = rows.map((holding) => {
      const childPositions = childRowsBySymbol.get(holding.symbolKey) || [];
      const rowDetailKey = `${rowControlPrefix}-${holding.symbolKey}`;
      const isSelected = selectedPositionDetail?.key === rowDetailKey;
      const weightPct = totalMarketValueCad > 0 && Number.isFinite(Number(holding.equityValueCad))
        ? (Number(holding.equityValueCad) / totalMarketValueCad) * 100
        : null;
      const openPositionDetail = () => {
        setSelectedPositionDetail((current) => (
          current?.key === rowDetailKey && current?.tabId === tabId
            ? null
            : {
              key: rowDetailKey,
              tabId,
              tableLabel,
              holding,
              childPositions,
              weightPct,
            }
        ));
      };
      const handlePositionRowKeyDown = (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') {
          return;
        }

        event.preventDefault();
        openPositionDetail(event);
      };

      return {
        holding,
        rowDetailKey,
        rowClassName: getInteractiveRowClassName(rowDetailKey, isSelected),
        rowAriaLabel: `View ${holding.displaySymbol || holding.symbol} details`,
        rowHandlers: getSharedRowHandlers(openPositionDetail, handlePositionRowKeyDown),
        weightPct,
        returnToneClass: getPositionToneClass(holding.gainLossPct),
      };
    });

    return (
      <div
        className="holdings-combined-table-frame"
        ref={(element) => setCombinedTableFrameRef(tabId, element)}
      >
        <div
          className="holdings-table is-combined"
          role="table"
          aria-label={tableLabel}
          aria-colcount={HOLDINGS_TABLE_COLUMNS.length}
          aria-rowcount={tableRows.length + 2}
        >
          <div className="holdings-table-header-group" role="rowgroup">
            <div className="holdings-table-grid-row holdings-table-header-row" role="row" aria-rowindex={1}>
              <div
                className="holdings-table-header-cell holdings-table-security-cell is-left"
                role="columnheader"
                aria-colindex={1}
              >
                {renderHeaderContent(HOLDINGS_TABLE_COLUMNS[0])}
              </div>
              <div className="holdings-metrics-clip">
                <div className="holdings-metrics-track holdings-table-header-metrics">
                  {HOLDINGS_TABLE_COLUMNS.slice(1).map((column, index) => renderHeaderCell(column, index + 1))}
                </div>
              </div>
            </div>
          </div>
          <HoldingsMetricsScrollController placement="top" enableFrameWheel />
          <div className="holdings-table-body" role="rowgroup">
            {tableRows.map(({ holding, rowDetailKey, rowClassName, rowAriaLabel, rowHandlers, weightPct, returnToneClass }, rowIndex) => (
              <div
                key={rowDetailKey}
                className={`holdings-table-grid-row ${rowClassName}`.trim()}
                data-holdings-row-detail-key={rowDetailKey}
                role="row"
                aria-label={rowAriaLabel}
                aria-rowindex={rowIndex + 2}
                {...rowHandlers}
              >
                <div className="holdings-table-body-cell holdings-table-security-cell" role="rowheader" aria-colindex={1}>
                  {renderSecurityCellContent(holding)}
                </div>
                <div className="holdings-metrics-clip">
                  <div className="holdings-metrics-track">
                    <div className="holdings-table-body-cell" role="cell" aria-colindex={2}>
                      {weightPct === null ? '—' : formatPercent(weightPct)}
                    </div>
                    <div className="holdings-table-body-cell" role="cell" aria-colindex={3}>
                      {holding.displayCurrency === 'Mixed' ? '—' : tableMoney(holding.lastPrice, holding.displayCurrency)}
                    </div>
                    <div className="holdings-table-body-cell" role="cell" aria-colindex={4}>
                      {holding.gainLossPct === null ? (
                        '—'
                      ) : (
                        <span className={`holdings-return-pill ${returnToneClass ? `is-${returnToneClass}` : ''}`.trim()}>
                          {returnToneClass === 'gain' ? (
                            <MdArrowUpward className="holdings-return-pill-arrow" size={12} aria-hidden="true" />
                          ) : returnToneClass === 'loss' ? (
                            <MdArrowDownward className="holdings-return-pill-arrow" size={12} aria-hidden="true" />
                          ) : null}
                          {formatPercent(holding.gainLossPct)}
                        </span>
                      )}
                    </div>
                    <div className="holdings-table-body-cell" role="cell" aria-colindex={5}>
                      {renderPositionMoney(holding.totalCostBasis, holding.totalCostBasisCad, holding.displayCurrency)}
                    </div>
                    <div className="holdings-table-body-cell" role="cell" aria-colindex={6}>
                      {renderPositionMoney(holding.marketValue, holding.marketValueCad, holding.displayCurrency)}
                    </div>
                    <div className="holdings-table-body-cell" role="cell" aria-colindex={7}>
                      {formatQuantity(holding.quantity)}
                    </div>
                    <div className="holdings-table-body-cell" role="cell" aria-colindex={8}>
                      {holding.displayCurrency === 'Mixed' || !holding.displayCurrency ? '—' : holding.displayCurrency}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
          <div className="holdings-table-footer-group" role="rowgroup">
            <div
              className="holdings-table-grid-row holdings-table-footer-row"
              role="row"
              aria-rowindex={tableRows.length + 2}
            >
              <div className="holdings-table-footer-cell holdings-table-security-cell" role="cell" aria-colindex={1} />
              <div className="holdings-metrics-clip">
                <div className="holdings-metrics-track holdings-table-footer-metrics">
                  <div
                    className="holdings-table-footer-cell holdings-table-total-label-cell"
                    role="cell"
                    aria-colindex={4}
                    aria-colspan={2}
                  >
                    <span className="holdings-table-total-label">
                      {tabId === 'options'
                        ? 'Total options value'
                        : tabId === 'crypto'
                          ? 'Total crypto value'
                          : 'Total holdings value'}
                    </span>
                  </div>
                  <div className="holdings-table-footer-cell holdings-table-total-value-cell" role="cell" aria-colindex={6}>
                    <span className={`holdings-table-total-value ${totalMarketValue > 0 ? 'is-positive' : totalMarketValue < 0 ? 'is-negative' : ''}`.trim()}>
                      {tableMoney(totalMarketValue, primaryCurrency)}
                    </span>
                  </div>
                  <div className="holdings-table-footer-cell holdings-table-total-currency-cell" role="cell" aria-colindex={8}>
                    <span className="holdings-table-total-currency">{primaryCurrency}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <HoldingsMetricsScrollController placement="bottom" />
        </div>
      </div>
    );
  };

  const renderGainersLosersChart = ({
    title,
    rows,
    isLossChart = false,
  }) => {
    const axisConfig = getGainersLosersAxisConfig(rows, isLossChart, showGainersLosersByValue);

    return (
      <div className="holdings-pnl-chart-panel">
        <div className="holdings-pnl-chart-heading">
          <h3 className="holdings-pnl-chart-title">{title}</h3>
        </div>
        {rows.length > 0 ? (
          <div className={`holdings-pnl-chart ${isLossChart ? 'is-loss-chart' : ''}`.trim()}>
            <GainersLosersEChart
              rows={rows}
              showByValue={showGainersLosersByValue}
              axisConfig={axisConfig}
              primaryCurrency={primaryCurrency}
              height="100%"
            />
          </div>
        ) : (
          <div className="holdings-analysis-empty holdings-pnl-empty">
            <span>{gainersLosersEmptyMessage}</span>
          </div>
        )}
      </div>
    );
  };


  return (
    <>
      {usesShellNavSlot ? createPortal(investmentsPrimaryNav, investmentsNavSlot) : null}
      {usesShellToolbar ? createPortal(investmentsFilterControls, investmentsFiltersSlot) : null}
      <div className="page-frame investments-hub-shell">
        <AppStatusNotice
          title="Scope update failed"
          message={scopeSaveError}
          onDismiss={() => setScopeSaveError('')}
        />
        <AppStatusNotice
          title="Holdings could not be loaded"
          message={holdingsLoadError}
          actionLabel="Retry"
          onAction={() => {
            failedHoldingAccountIdsRef.current.clear();
            setHoldingsLoadError('');
            setHoldingsRetryVersion((value) => value + 1);
          }}
        />
        <AppStatusNotice
          title="Income could not be loaded"
          message={activeTab === 'income' ? incomeError : ''}
          actionLabel="Retry"
          onAction={retryIncomeTransactions}
          actionDisabled={incomeLoading}
        />
        {!usesShellToolbar && (
          <div className="investments-page-header">
            {showPageTitle ? (
              <div className="investments-page-header-main">
                <h1 className="page-top-title investments-page-title">Investments</h1>
              </div>
            ) : (
              <div className="investments-page-header-main" />
            )}
            {investmentsFilterControls}
          </div>
        )}

        {!usesShellNavSlot ? (
          <div className="investments-navigation-stack">
            {investmentsPrimaryNav}
          </div>
        ) : null}

        {showTopSummaryMetrics ? (
          <div
            className={`holdings-summary-grid investments-top-metrics has-${activeTopSummaryMetrics.length}-metrics is-${activeTab}-summary`.trim()}
            aria-label={topSummaryLabel}
          >
            {activeTopSummaryMetrics.map((metric) => (
              <div key={metric.id} className="panel-shell holdings-summary-card">
                <span className="holdings-summary-label">{metric.label}</span>
                <strong className={`holdings-summary-value ${metric.tone || ''}`.trim()}>
                  {metric.valueCompact
                    ? <FitMoney full={metric.value} compact={metric.valueCompact} className="holdings-summary-money" />
                    : metric.value}
                </strong>
              </div>
            ))}
          </div>
        ) : null}

        <div
          id={`investments-panel-${activeTab}`}
          className="investments-hub-content"
          ref={investmentsHubContentRef}
          role="tabpanel"
          aria-label={INVESTMENTS_TABS.find((tab) => tab.id === activeTab)?.label || 'Investments'}
        >
          {activeTab === 'overview' && (
            <>
              <section className="panel-shell holdings-chart-section">
                <button
                  type="button"
                  className={`holdings-section-toggle ${!isAllocationSectionCollapsed ? 'is-expanded' : ''}`.trim()}
                  aria-expanded={!isAllocationSectionCollapsed}
                  onClick={() => setIsAllocationSectionCollapsed((previous) => !previous)}
                >
                  <span className="holdings-section-toggle-chevron" aria-hidden="true">
                    <TriangleIcon direction={isAllocationSectionCollapsed ? 'right' : 'down'} />
                  </span>
                  <span className="holdings-section-toggle-copy">
                    <span className="holdings-section-title">Allocation</span>
                  </span>
                </button>
                {!isAllocationSectionCollapsed && (
                  <div className="holdings-chart-section-body">
                    {topHoldingsBarData.length > 0 ? (
                      <>
                        <div className="holdings-top-chart">
                          <TopHoldingEChart
                            rows={topHoldingsBarData}
                            primaryCurrency={primaryCurrency}
                            balancesHidden={balancesHidden}
                            height="100%"
                          />
                        </div>
                        <div className="holdings-chart-divider" aria-hidden="true" />
                        <div className="holdings-pie-grid">
                          <div className="holdings-pie-panel">
                            <div className="holdings-pie-panel-header">
                              <div className="holdings-pie-panel-copy">
                                <span className="holdings-pie-title">Positions Allocation</span>
                              </div>
                            </div>
                            <div className="holdings-allocation-pie-wrap">
                              <div className="holdings-allocation-pie">
                                <HoldingsPieEChart
                                  data={allocationChartData}
                                  labelKey="symbol"
                                  primaryCurrency={primaryCurrency}
                                  height="100%"
                                />
                              </div>
                            </div>
                          </div>

                          <div className="holdings-pie-panel">
                            <div className="holdings-pie-panel-header">
                              <div className="holdings-pie-panel-copy">
                                <span className="holdings-pie-title">Composition</span>
                              </div>
                            </div>
                            {compositionChart.slices.length > 0 ? (
                              <div className="holdings-allocation-pie-wrap">
                                <div className="holdings-allocation-pie">
                                  <HoldingsPieEChart
                                    data={compositionChart.slices}
                                    labelKey="shortLabel"
                                    primaryCurrency={primaryCurrency}
                                    height="100%"
                                  />
                                </div>
                              </div>
                            ) : (
                              <div className="holdings-analysis-empty">
                                <span>No composition data is available for the selected positions.</span>
                              </div>
                            )}
                            <div className="holdings-composition-controls" role="group" aria-label="Composition grouping">
                              <div className="chart-view-stepper holdings-composition-stepper">
                                <button
                                  type="button"
                                  className="chart-view-stepper-btn chart-view-stepper-prev-btn app-control-root"
                                  onClick={() => cycleCompositionGroupMode(-1)}
                                  aria-label={`Show ${previousCompositionGroupMode.label}`}
                                  data-tooltip={previousCompositionGroupMode.label}
                                >
                                  <span className="app-control-icon" aria-hidden="true"><TriangleIcon direction="left" /></span>
                                </button>
                                <span className="chart-view-stepper-label holdings-composition-stepper-label">
                                  {currentCompositionGroupMode.label}
                                </span>
                                <button
                                  type="button"
                                  className="chart-view-stepper-btn chart-view-stepper-next-btn app-control-root"
                                  onClick={() => cycleCompositionGroupMode(1)}
                                  aria-label={`Show ${nextCompositionGroupMode.label}`}
                                  data-tooltip={nextCompositionGroupMode.label}
                                >
                                  <span className="app-control-icon" aria-hidden="true"><TriangleIcon direction="right" /></span>
                                </button>
                              </div>
                            </div>
                          </div>
                        </div>
                      </>
                    ) : (
                      <div className="holdings-analysis-empty">
                        <span>{positionsChartsEmptyMessage}</span>
                      </div>
                    )}
                  </div>
                )}
              </section>

              <section className="panel-shell holdings-chart-section">
                <button
                  type="button"
                  className={`holdings-section-toggle ${!isGainersLosersSectionCollapsed ? 'is-expanded' : ''}`.trim()}
                  aria-expanded={!isGainersLosersSectionCollapsed}
                  onClick={() => setIsGainersLosersSectionCollapsed((previous) => !previous)}
                >
                  <span className="holdings-section-toggle-chevron" aria-hidden="true">
                    <TriangleIcon direction={isGainersLosersSectionCollapsed ? 'right' : 'down'} />
                  </span>
                  <span className="holdings-section-toggle-copy">
                    <span className="holdings-section-title">Gainers / Losers</span>
                  </span>
                </button>
                {!isGainersLosersSectionCollapsed && (
                  <div className="holdings-chart-section-body">
                    <div className="holdings-pnl-toolbar">
                      <button
                        type="button"
                        role="switch"
                        aria-checked={showGainersLosersByValue}
                        className={`holdings-pnl-toggle ${showGainersLosersByValue ? 'is-active' : ''}`.trim()}
                        onClick={() => setShowGainersLosersByValue((previous) => !previous)}
                      >
                        <span className="holdings-pnl-toggle-switch" aria-hidden="true">
                          <span />
                        </span>
                        <span>{`Show By P&L Value (${primaryCurrencySymbol})`}</span>
                      </button>
                    </div>
                    <div className="holdings-pnl-chart-stack">
                      {renderGainersLosersChart({
                        title: 'Top Gainers',
                        rows: topUnrealizedGainersData,
                      })}
                      {renderGainersLosersChart({
                        title: 'Top Losers',
                        rows: topUnrealizedLosersData,
                        isLossChart: true,
                      })}
                    </div>
                  </div>
                )}
              </section>
            </>
          )}

          {activeTab === 'holdings' && (
            <>
              <section className="panel-shell holdings-table-section holdings-combined-table-section">
                <button
                  type="button"
                  className={`holdings-section-toggle ${!isPositionsTableCollapsed ? 'is-expanded' : ''}`.trim()}
                  aria-expanded={!isPositionsTableCollapsed}
                  onClick={() => setIsPositionsTableCollapsed((previous) => !previous)}
                >
                  <span className="holdings-section-toggle-chevron" aria-hidden="true">
                    <TriangleIcon direction={isPositionsTableCollapsed ? 'right' : 'down'} />
                  </span>
                  <span className="holdings-section-toggle-copy">
                    <span className="holdings-section-title">Holdings</span>
                    <span className="holdings-section-meta">{combinedPositionsMeta}</span>
                  </span>
                </button>
                {!isPositionsTableCollapsed && renderCombinedTable({
                  rows: sortedCombinedTableDataset,
                  childRowsBySymbol: childPositionsBySymbol,
                  emptyMessage: 'No eligible invested positions are available for the selected institutions.',
                  rowControlPrefix: 'positions',
                  tabId: 'holdings',
                  tableLabel: 'Holdings',
                })}
              </section>

              <section className="panel-shell holdings-table-section holdings-cash-section">
                <button
                  type="button"
                  className={`holdings-section-toggle ${!isCashSectionCollapsed ? 'is-expanded' : ''}`.trim()}
                  aria-expanded={!isCashSectionCollapsed}
                  onClick={() => setIsCashSectionCollapsed((previous) => !previous)}
                >
                  <span className="holdings-section-toggle-chevron" aria-hidden="true">
                    <TriangleIcon direction={isCashSectionCollapsed ? 'right' : 'down'} />
                  </span>
                  <span className="holdings-section-toggle-copy">
                    <span className="holdings-section-title">Cash Balance</span>
                  </span>
                </button>
                {!isCashSectionCollapsed && (
                  filteredAccounts.length === 0 ? (
                    <p className="no-data">No investment accounts are included in this view.</p>
                  ) : !holdingsReady ? (
                    <p className="no-data">Loading holdings...</p>
                  ) : cashAccountRows.length === 0 ? (
                    <p className="no-data">No cash balances are available for the selected institutions.</p>
                  ) : (
                    <div className="holdings-cash-panel">
                      <CashBalanceTopScrollController />
                      <div className="holdings-cash-scroll-surface">
                        <table className="holdings-cash-compact-table">
                          <thead>
                            <tr>
                              <th>Account</th>
                              <th>Account Type</th>
                              <th className="is-right">Cash Positions</th>
                              <th>Currency</th>
                            </tr>
                          </thead>
                          <tbody>
                            {cashAccountRows.map((accountRow) => (
                              <tr key={accountRow.key}>
                                <td>
                                  <div className="holdings-account-cell holdings-cash-account-cell" title={accountRow.institutionName}>
                                    <div className="holdings-inst-logo-wrap holdings-account-logo-wrap">
                                      <InstitutionLogo name={accountRow.institutionName} size={16} />
                                    </div>
                                    <span className="holdings-account-primary">{accountRow.accountName}</span>
                                  </div>
                                </td>
                                <td>
                                  <div className="holdings-account-type-cell">
                                    {accountRow.accountTypeLabel ? (
                                      <AccountTypeBadge
                                        accountType={accountRow.accountType}
                                        label={accountRow.accountTypeLabel}
                                        className="holdings-account-type-badge"
                                      />
                                    ) : null}
                                  </div>
                                </td>
                                <td className="is-right">
                                  <div className="holdings-cash-balance-stack">
                                    {accountRow.balances.map((balanceRow) => (
                                      <div key={balanceRow.key} className="holdings-cash-value-line">
                                        <span className={`holdings-cash-value ${balanceRow.nativeAmount > 0 ? 'is-positive' : balanceRow.nativeAmount < 0 ? 'is-negative' : ''}`.trim()}>
                                          {maskMoney(formatTableMoney(balanceRow.nativeAmount, balanceRow.currency))}
                                        </span>
                                      </div>
                                    ))}
                                  </div>
                                </td>
                                <td>
                                  <div className="holdings-cash-currency-stack">
                                    {accountRow.balances.map((balanceRow) => (
                                      <div key={balanceRow.key} className="holdings-cash-currency-line">
                                        <span className="holdings-cash-currency-code">{balanceRow.currency}</span>
                                      </div>
                                    ))}
                                  </div>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                          <tfoot>
                            <tr>
                              <td colSpan={2} className="is-right">
                                <span className="holdings-cash-total-label">Total Cash Balance</span>
                              </td>
                              <td className="is-right">
                                <span className={`holdings-cash-total-value ${cashValue > 0 ? 'is-positive' : cashValue < 0 ? 'is-negative' : ''}`.trim()}>
                                  {maskMoney(formatTableMoney(fxToPrimary(cashValue || 0), primaryCurrency))}
                                </span>
                              </td>
                              <td>
                                <span className="holdings-cash-currency-code">{primaryCurrency}</span>
                              </td>
                            </tr>
                          </tfoot>
                        </table>
                      </div>
                    </div>
                  )
                )}
              </section>
            </>
          )}

          {activeTab === 'options' && (
            <>
              <section className="panel-shell holdings-table-section options-calendar-section">
                <div className="options-calendar-section-header">
                  <button
                    type="button"
                    className={`holdings-section-toggle options-calendar-section-toggle ${!isOptionsCalendarCollapsed ? 'is-expanded' : ''}`.trim()}
                    aria-expanded={!isOptionsCalendarCollapsed}
                    onClick={() => setIsOptionsCalendarCollapsed((previous) => !previous)}
                  >
                    <span className="holdings-section-toggle-chevron" aria-hidden="true">
                      <TriangleIcon direction={isOptionsCalendarCollapsed ? 'right' : 'down'} />
                    </span>
                    <span className="holdings-section-toggle-copy">
                      <span className="holdings-section-title">Expiration Calendar</span>
                    </span>
                  </button>
                  {!isOptionsCalendarCollapsed && (
                    <>
                      <div className="options-calendar-period-control cf-timeline-popover" aria-label="Options calendar month">
                        <button
                          type="button"
                          className="cf-timeline-step-btn cf-timeline-prev-btn app-control-root"
                          onClick={() => handleOptionsCalendarStep(-1)}
                          aria-label="Previous month"
                          data-tooltip="Previous month"
                        >
                          <span className="app-control-icon" aria-hidden="true"><TriangleIcon direction="left" /></span>
                        </button>
                        <output
                          className="chart-view-stepper-label options-calendar-period-label"
                          aria-label={`Selected month: ${optionsCalendarMonthLabel}`}
                        >
                          {optionsCalendarMonthLabel}
                        </output>
                        <button
                          type="button"
                          className="cf-timeline-step-btn cf-timeline-next-btn app-control-root"
                          onClick={() => handleOptionsCalendarStep(1)}
                          aria-label="Next month"
                          data-tooltip="Next month"
                        >
                          <span className="app-control-icon" aria-hidden="true"><TriangleIcon direction="right" /></span>
                        </button>
                        <button
                          type="button"
                          className="cf-timeline-step-btn cf-timeline-today-btn app-control-root"
                          disabled={isOptionsCalendarCurrentMonth}
                          onClick={handleOptionsCalendarToday}
                          aria-label="Jump to current month"
                          data-tooltip="Jump to current month"
                        >
                          <span className="app-control-icon" aria-hidden="true"><CurrentPeriodIcon /></span>
                        </button>
                      </div>
                      <div className="options-calendar-legend">
                        <span className="options-calendar-legend-item">
                          <span className="options-calendar-legend-dot is-long" aria-hidden="true" />
                          <span>Long</span>
                        </span>
                        <span className="options-calendar-legend-item">
                          <span className="options-calendar-legend-dot is-short" aria-hidden="true" />
                          <span>Short</span>
                        </span>
                      </div>
                    </>
                  )}
                </div>
                {!isOptionsCalendarCollapsed && (
                  <div className="options-calendar">
                    <div className="options-calendar-grid">
                      {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((dayName) => (
                        <div key={dayName} className="options-calendar-weekday">{dayName}</div>
                      ))}
                      {optionsCalendarGrid.map((cell) => (
                        <div
                          key={cell.dayKey}
                          className={[
                            'options-calendar-day',
                            cell.isOutsideMonth ? 'is-outside' : '',
                            cell.isToday ? 'is-today' : '',
                            cell.entries.length > 0 ? 'has-entries' : '',
                          ].filter(Boolean).join(' ')}
                        >
                          <span className="options-calendar-day-number">{cell.day}</span>
                          {cell.entries.length > 0 && (
                            <div className="options-calendar-pills">
                              {cell.entries.map((entry) => {
                                const sideLabel = entry.side === 'C' ? 'CALL' : 'PUT';
                                const optionLabel = `${entry.ticker} ${entry.strike} ×${entry.absQuantity} ${sideLabel}`;
                                return (
                                  <span
                                    key={entry.key}
                                    className={`options-calendar-pill ${entry.isShort ? 'is-short' : 'is-long'}`}
                                  >
                                    <OverflowTooltipText className="options-calendar-pill-label" text={optionLabel} />
                                  </span>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </section>
              <section className="panel-shell holdings-table-section holdings-combined-table-section">
                <button
                  type="button"
                  className={`holdings-section-toggle ${!isOptionsTableCollapsed ? 'is-expanded' : ''}`.trim()}
                  aria-expanded={!isOptionsTableCollapsed}
                  onClick={() => setIsOptionsTableCollapsed((previous) => !previous)}
                >
                  <span className="holdings-section-toggle-chevron" aria-hidden="true">
                    <TriangleIcon direction={isOptionsTableCollapsed ? 'right' : 'down'} />
                  </span>
                  <span className="holdings-section-toggle-copy">
                    <span className="holdings-section-title">Options</span>
                    <span className="holdings-section-meta">{combinedOptionsMeta}</span>
                  </span>
                </button>
                {!isOptionsTableCollapsed && renderCombinedTable({
                  rows: sortedCombinedOptionDataset,
                  childRowsBySymbol: childOptionPositionsBySymbol,
                  emptyMessage: 'No option positions are available for the selected institutions.',
                  rowControlPrefix: 'options',
                  tabId: 'options',
                  tableLabel: 'Options',
                })}
              </section>
            </>
          )}

          {activeTab === 'crypto' && (
            <section className="panel-shell holdings-table-section holdings-combined-table-section">
              <button
                type="button"
                className={`holdings-section-toggle ${!isCryptoTableCollapsed ? 'is-expanded' : ''}`.trim()}
                aria-expanded={!isCryptoTableCollapsed}
                onClick={() => setIsCryptoTableCollapsed((previous) => !previous)}
              >
                <span className="holdings-section-toggle-chevron" aria-hidden="true">
                  <TriangleIcon direction={isCryptoTableCollapsed ? 'right' : 'down'} />
                </span>
                <span className="holdings-section-toggle-copy">
                  <span className="holdings-section-title">Crypto</span>
                  <span className="holdings-section-meta">{combinedCryptoMeta}</span>
                </span>
              </button>
              {!isCryptoTableCollapsed && renderCombinedTable({
                rows: sortedCombinedCryptoDataset,
                childRowsBySymbol: childCryptoPositionsBySymbol,
                emptyMessage: 'No crypto positions are available for the selected institutions.',
                rowControlPrefix: 'crypto',
                tabId: 'crypto',
                tableLabel: 'Crypto',
              })}
            </section>
          )}

          {activeTab === 'performance' && (
            <>
              {selectedAccountIdsForView.length === 0 ? (
                <div className="income-empty">
                  <span className="income-empty-text">No investment accounts selected.</span>
                  <span className="income-empty-hint">No accounts in scope.</span>
                </div>
              ) : performanceLoading && !performanceData ? (
                <div className="income-loading">
                  <div className="income-loading-spinner" />
                  <span>Loading performance data...</span>
                </div>
              ) : performanceError ? (
                <div className="income-empty">
                  <span className="income-empty-text">Performance data unavailable.</span>
                  <span className="income-empty-hint">{performanceError}</span>
                </div>
              ) : performanceChartData.length === 0 ? (
                <div className="income-empty">
                  <span className="income-empty-text">No performance history recorded yet.</span>
                  <span className="income-empty-hint">No investment balance snapshots match this selection.</span>
                </div>
              ) : (
                <>
                  <section className="panel-shell holdings-chart-section performance-chart-section">
                    <div className="holdings-section-header performance-section-header">
                      <div className="holdings-section-toggle-copy">
                        <span className="holdings-section-title">Return vs Benchmarks</span>
                      </div>
                      {performanceBenchmarkOptions.length > 0 ? (
                        <div className="performance-benchmark-toggles" aria-label="Benchmark comparisons">
                          {performanceBenchmarkOptions.map((benchmark) => {
                            const benchmarkTone = benchmark.return_pct > 0
                              ? 'is-positive'
                              : benchmark.return_pct < 0 ? 'is-negative' : '';
                            return (
                              <label key={benchmark.id} className="performance-benchmark-toggle">
                                <input
                                  type="checkbox"
                                  checked={benchmark.isVisible}
                                  onChange={() => togglePerformanceBenchmark(benchmark.id)}
                                />
                                <span className="performance-benchmark-swatch" style={{ backgroundColor: benchmark.color }} />
                                <span className="performance-benchmark-name">
                                  {benchmark.name}
                                </span>
                                <span className={`performance-benchmark-return ${benchmarkTone}`.trim()}>
                                  {formatSignedPercent(benchmark.return_pct)}
                                </span>
                              </label>
                            );
                          })}
                        </div>
                      ) : null}
                    </div>

                    <div className="holdings-chart-section-body">
                      <div className="performance-line-chart">
                        <PerformanceLineEChart
                          data={performanceChartData}
                          benchmarks={performanceBenchmarkOptions}
                          portfolioColor={performanceLineColors.portfolio}
                          height="100%"
                        />
                      </div>
                      {performanceBenchmarkDataNote ? (
                        <div className="performance-benchmark-data-note">
                          {performanceBenchmarkDataNote}
                        </div>
                      ) : null}
                    </div>
                  </section>
                </>
              )}
            </>
          )}

          {activeTab === 'income' && (
            <>
              {incomeLoading ? (
                <div className="income-loading">
                  <div className="income-loading-spinner" />
                  <span>Loading income data...</span>
                </div>
              ) : incomeError && !Array.isArray(incomeTransactions) ? (
                <div className="income-empty" role="alert">
                  <span className="income-empty-text">Income data is unavailable.</span>
                  <span className="income-empty-hint">Use Retry above to load it again.</span>
                </div>
              ) : (
                <>
                  {incomeSubTab === 'dividends' && (
                    dividendTransactions.length === 0 && withholdingTransactions.length === 0 ? (
                      <div className="income-empty">
                        <span className="income-empty-text">No dividend income recorded yet.</span>
                        <span className="income-empty-hint">No dividend or withholding tax data is included for the selected accounts and timeline.</span>
                      </div>
                    ) : (
                      <>
                  {/* Income History bar chart */}
                  <section className="panel-shell holdings-chart-section income-chart-section" ref={dividendHistorySectionRef}>
                    <button
                      type="button"
                      className={`holdings-section-toggle ${!isIncomeChartCollapsed ? 'is-expanded' : ''}`.trim()}
	                      aria-expanded={!isIncomeChartCollapsed}
	                      onClick={() => setIsIncomeChartCollapsed((prev) => !prev)}
	                    >
	                      <span className="holdings-section-toggle-chevron" aria-hidden="true">
	                        <TriangleIcon direction={isIncomeChartCollapsed ? 'right' : 'down'} />
	                      </span>
	                      <span className="holdings-section-toggle-copy">
	                        <span className="holdings-section-title">Dividend Income History</span>
	                        <span className="holdings-section-meta income-history-meta">
                          <span>
                            {combinedDividendHistoryData.months.length} {dividendHistoryBucketLabel}
	                          </span>
                          {selectedDividendPositionSymbol ? (
                            <span className="income-history-filter-chip">{selectedDividendPositionSymbol}</span>
                          ) : null}
	                        </span>
	                      </span>
	                    </button>
                    {!isIncomeChartCollapsed && (
                      <div className="holdings-chart-section-body">
                        {combinedDividendHistoryData.months.length > 0 ? (
                          <div className="income-history-chart">
                            <IncomeHistoryEChart
                              data={combinedDividendHistoryData.months}
                              primaryCurrency={primaryCurrency}
                              height={400}
                              selectedBucketKey={activeIncomeSelectedBucketKey}
                              onSelectBucket={(entry) => handleIncomeHistoryBucketSelect(activeIncomeDetailChartId, entry)}
                            />
                          </div>
                        ) : (
                          <div className="holdings-analysis-empty">
                            <span>
                              {selectedDividendPositionSymbol
                                ? `No dividend or withholding tax history available for ${selectedDividendPositionSymbol} in the selected timeline.`
                                : 'No dividend or withholding tax history available for the selected timeline.'}
                            </span>
                          </div>
                        )}
                      </div>
                    )}
                  </section>

                  {/* Income by Position */}
                  <section className="panel-shell holdings-chart-section income-chart-section income-position-chart-section" ref={dividendPositionSectionRef}>
                    <button
                      type="button"
                      className={`holdings-section-toggle ${!isIncomByPositionCollapsed ? 'is-expanded' : ''}`.trim()}
	                      aria-expanded={!isIncomByPositionCollapsed}
	                      onClick={() => setIsIncomeByPositionCollapsed((prev) => !prev)}
	                    >
	                      <span className="holdings-section-toggle-chevron" aria-hidden="true">
	                        <TriangleIcon direction={isIncomByPositionCollapsed ? 'right' : 'down'} />
	                      </span>
	                      <span className="holdings-section-toggle-copy">
	                        <span className="holdings-section-title">Dividends by Position</span>
	                        <span className="holdings-section-meta">
	                          {dividendsByPosition.length} {dividendsByPosition.length === 1 ? 'symbol' : 'symbols'}
	                        </span>
	                      </span>
	                    </button>
                    {!isIncomByPositionCollapsed && (
                      <div className="holdings-chart-section-body">
                        {dividendsByPosition.length > 0 ? (
                          <div className="income-by-position-chart">
                            <DividendsByPositionEChart
                              data={dividendsByPosition}
                              selectedSymbol={selectedDividendPositionSymbol}
                              onSelect={handleDividendPositionSelect}
                              primaryCurrency={primaryCurrency}
                              balancesHidden={balancesHidden}
                            />
                          </div>
                        ) : (
                          <div className="holdings-analysis-empty">
                            <span>No dividend data by position available for the selected timeline.</span>
                          </div>
                        )}
                      </div>
                    )}
                  </section>

                  {/* Recent income transactions by active mode */}
                  <section className="panel-shell holdings-table-section income-table-section">
                    <button
                      type="button"
                      className={`holdings-section-toggle ${!isIncomeTableCollapsed ? 'is-expanded' : ''}`.trim()}
	                      aria-expanded={!isIncomeTableCollapsed}
	                      onClick={() => setIsIncomeTableCollapsed((prev) => !prev)}
	                    >
	                      <span className="holdings-section-toggle-chevron" aria-hidden="true">
	                        <TriangleIcon direction={isIncomeTableCollapsed ? 'right' : 'down'} />
	                      </span>
	                      <span className="holdings-section-toggle-copy">
	                        <span className="holdings-section-title">Recent Dividend Income Activity</span>
	                        <span className="holdings-section-meta">Last {recentDividendIncomeTransactions.length} transactions</span>
	                      </span>
	                    </button>
	                    {!isIncomeTableCollapsed && (
	                      <div className="holdings-table-wrap income-activity-table-wrap">
	                        {recentDividendIncomeTransactions.length > 0 ? (
	                          <div className="income-activity-scroll-surface">
	                            <table className="income-dividends-table income-dividends-header-table">
	                              <thead>
	                                <tr>
	                                  <th>Date</th>
                                  <th>Account</th>
                                  <th>Activity</th>
                                  <th>Amount</th>
	                                  <th>Currency</th>
	                                </tr>
	                              </thead>
	                            </table>
	                            <IncomeActivityTopScrollController />
	                            <table className="income-dividends-table income-dividends-body-table">
	                              <tbody>
	                                {recentDividendIncomeTransactions.map((tx) => (
	                                  <tr key={tx.id}>
                                    <td>{formatShortDateLabel(tx.date)}</td>
                                    <td className="income-table-account">
                                      <span className="income-table-account-cell">
                                        <InstitutionLogo name={tx.institution_name} size={16} />
                                        <span>{tx.account_name}</span>
                                      </span>
                                    </td>
                                    <td className="income-table-symbol">
                                      {tx.type === 'withholding_tax' ? 'Withholding Tax' : 'Dividend'} · {tx.symbol || tx.description || '—'}
                                    </td>
                                    <td className={tx.type === 'withholding_tax' ? 'is-withholding' : (tx.amount >= 0 ? 'is-positive' : 'is-negative')}>
                                      {formatTableMoney(Math.abs(tx.amount), tx.currency)}
                                    </td>
                                    <td>{tx.currency}</td>
                                  </tr>
	                                ))}
	                              </tbody>
	                            </table>
	                          </div>
	                        ) : (
                          <div className="holdings-analysis-empty">
                            <span>No recent dividend income or withholding transactions.</span>
                          </div>
                        )}
                      </div>
                    )}
                  </section>
                      </>
                    )
                  )}

                  {incomeSubTab === 'interest' && (
                    interestTransactions.length === 0 ? (
                      <div className="income-empty">
                        <span className="income-empty-text">No interest transactions recorded yet.</span>
                        <span className="income-empty-hint">No interest data is included for the selected accounts and timeline.</span>
                      </div>
                    ) : (
                      <>
                        {/* Interest monthly chart */}
                        <section className="panel-shell holdings-chart-section income-chart-section" ref={interestHistorySectionRef}>
                          <button
                            type="button"
                            className={`holdings-section-toggle ${!isInterestChartCollapsed ? 'is-expanded' : ''}`.trim()}
	                            aria-expanded={!isInterestChartCollapsed}
	                            onClick={() => setIsInterestChartCollapsed((prev) => !prev)}
	                          >
	                            <span className="holdings-section-toggle-chevron" aria-hidden="true">
	                              <TriangleIcon direction={isInterestChartCollapsed ? 'right' : 'down'} />
	                            </span>
	                            <span className="holdings-section-toggle-copy">
	                              <span className="holdings-section-title">Interest History</span>
	                              <span className="holdings-section-meta income-history-meta">
                                <span>
                                  {interestMonthlyChartData.months.length} {interestHistoryBucketLabel}
	                                </span>
	                              </span>
	                            </span>
	                          </button>
                          {!isInterestChartCollapsed && (
                            <div className="holdings-chart-section-body">
                              {interestMonthlyChartData.months.length > 0 ? (
                                <div className="income-history-chart">
                                  <IncomeHistoryEChart
                                    data={interestMonthlyChartData.months}
                                    primaryCurrency={primaryCurrency}
                                    height={400}
                                    selectedBucketKey={activeIncomeSelectedBucketKey}
                                    onSelectBucket={(entry) => handleIncomeHistoryBucketSelect('interest', entry)}
                                    positiveKey="Earned"
                                    negativeKey="Paid"
                                    positiveName="Earned"
                                    negativeName="Paid"
                                    positiveColor={interestReceivedBarColor}
                                    negativeColor={interestPaidBarColor}
                                  />
                                </div>
                              ) : (
                                <div className="holdings-analysis-empty">
                                  <span>No interest history available.</span>
                                </div>
                              )}
                            </div>
                          )}
                        </section>

                        {/* Recent Interest Transactions */}
                        <section className="panel-shell holdings-table-section income-table-section">
                          <button
                            type="button"
                            className={`holdings-section-toggle ${!isInterestTableCollapsed ? 'is-expanded' : ''}`.trim()}
	                            aria-expanded={!isInterestTableCollapsed}
	                            onClick={() => setIsInterestTableCollapsed((prev) => !prev)}
	                          >
	                            <span className="holdings-section-toggle-chevron" aria-hidden="true">
	                              <TriangleIcon direction={isInterestTableCollapsed ? 'right' : 'down'} />
	                            </span>
	                            <span className="holdings-section-toggle-copy">
	                              <span className="holdings-section-title">Recent Interest Transactions</span>
	                              <span className="holdings-section-meta">Last {recentInterest.length} transactions</span>
	                            </span>
	                          </button>
	                          {!isInterestTableCollapsed && (
	                            <div className="holdings-table-wrap income-activity-table-wrap">
	                              {recentInterest.length > 0 ? (
	                                <div className="income-activity-scroll-surface">
	                                  <table className="income-dividends-table income-dividends-header-table">
	                                    <thead>
	                                      <tr>
	                                        <th>Date</th>
                                        <th>Account</th>
                                        <th>Description</th>
                                        <th>Amount</th>
	                                        <th>Currency</th>
	                                      </tr>
	                                    </thead>
	                                  </table>
	                                  <IncomeActivityTopScrollController />
	                                  <table className="income-dividends-table income-dividends-body-table">
	                                    <tbody>
	                                      {recentInterest.map((tx) => (
	                                        <tr key={tx.id}>
                                          <td>{formatShortDateLabel(tx.date)}</td>
                                          <td className="income-table-account">
                                            <span className="income-table-account-cell">
                                              <InstitutionLogo name={tx.institution_name} size={16} />
                                              <span>{tx.account_name}</span>
                                            </span>
                                          </td>
                                          <td className="income-table-symbol">{tx.description || tx.symbol || '—'}</td>
                                          <td className={tx.amount >= 0 ? 'is-positive' : 'is-negative'}>
                                            {formatTableMoney(tx.amount, tx.currency)}
                                          </td>
                                          <td>{tx.currency}</td>
                                        </tr>
	                                      ))}
	                                    </tbody>
	                                  </table>
	                                </div>
	                              ) : (
                                <div className="holdings-analysis-empty">
                                  <span>No recent interest transactions.</span>
                                </div>
                              )}
                            </div>
                          )}
                        </section>
                      </>
                    )
                  )}
                </>
              )}
            </>
          )}

          {isPositionDetailTrayPresent && document.querySelector('.app-main') ? createPortal((
            // Portaled into .app-main + position:fixed (see App.css —
            // scoped fixed rule for .position-detail-tray). The tray
            // opens at a stable viewport top regardless of which row
            // was clicked; the clicked row's highlight + the tray
            // title bar provide the link.
            <div
              className={`app-edge-tray is-pinned position-detail-tray ${positionDetailTray.animatedOpen ? 'is-open' : ''}`.trim()}
              aria-hidden={!isPositionDetailDrawerVisible}
            >
              <div className="app-edge-tray-backdrop" aria-hidden="true" />
              <div
                className="app-edge-tray-shell"
                ref={positionDetailTrayShellRef}
              >
                <SideDetailDrawerPanel
                  meta=""
                  title={visiblePositionDetail.holding.displaySymbol || visiblePositionDetail.holding.symbol}
                  subtitle={visiblePositionDetail.holding.displayName || ''}
                  onClose={closePositionDetailDrawer}
                  className="panel-shell position-detail-panel"
                >
                  {renderPositionDetailDrawerContent(visiblePositionDetail)}
                </SideDetailDrawerPanel>
              </div>
            </div>
          ), document.querySelector('.app-main')) : null}

          {isIncomeDetailTrayVisible && document.querySelector('.app-main') ? createPortal((
            // Rendered through a portal into .app-main for ownership, while the
            // shell itself uses fixed viewport placement (see App.css). Its
            // inline `top` is a viewport coordinate measured from the source
            // chart panel so anchored and pinned trays share horizontal gap math.
            <div
              className={`app-edge-tray is-anchored income-detail-tray ${incomeDetailTray.animatedOpen ? 'is-open' : ''}`.trim()}
              aria-hidden={!isIncomeDetailDrawerOpen}
            >
              <div className="app-edge-tray-backdrop" aria-hidden="true" />
              <div
                className="app-edge-tray-shell"
                ref={incomeDetailTrayShellRef}
                style={incomeDetailTrayTopY !== null
                  ? { '--app-tray-anchored-top': `${incomeDetailTrayTopY}px` }
                  : { visibility: 'hidden' }}
              >
                <SideDetailDrawerPanel
                  meta=""
                  title={visibleIncomeDrawerDetail.entry.month}
                  subtitle=""
                  onClose={closeIncomeDetailDrawer}
                  className="panel-shell income-detail-panel"
                >
                  <IncomeHistoryDetailDrawerContent
                    detail={visibleIncomeDrawerDetail.detail}
                    primaryCurrency={primaryCurrency}
                    expandedGroupKeys={incomeDetailExpandedGroupKeys}
                    onToggleGroup={handleIncomeDetailGroupToggle}
                  />
                </SideDetailDrawerPanel>
              </div>
            </div>
          ), document.querySelector('.app-main')) : null}
        </div>
      </div>
    </>
  );
}

export default Holdings;
