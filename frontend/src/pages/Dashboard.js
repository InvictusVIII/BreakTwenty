import React, { useState, useMemo, useEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import NetWorthChart from '../components/NetWorthChart';
import CsvExportButton from '../components/CsvExportButton';
import AppStatusNotice from '../components/AppStatusNotice';
import AllocationChart, {
  OTHER_INSTITUTIONS_COLOR_KEY,
  OTHER_INSTITUTIONS_LABEL,
} from '../components/AllocationChart';
import InstitutionAccountSelector, { ScopeSelectorTrigger, formatScopeSelectionSummary } from '../components/InstitutionAccountSelector';
import { ChartColorPopover, ChartColorRow } from '../components/ChartColorControls';
import InstitutionLogo from '../components/InstitutionLogo';
import ProviderSyncStatus from '../components/ProviderSyncStatus';
import CategoryPill from '../components/CategoryPill';
import { TOUR_DEMO_CASH_FLOW_ANCHOR_DATE, TOUR_DEMO_NOW_ISO } from '../components/tourDemoData';
import SortableTableHeader, { getNextSortConfig, usePersistentSortConfig } from '../components/SortableTableHeader';
import useDismissibleLayer, { APP_NON_DISMISS_INTERACTION_SELECTOR } from '../hooks/useDismissibleLayer';
import {
  MdAccountBalance,
  MdAdd,
  MdCheck,
  MdCreditCard,
  MdDragIndicator,
  MdInfoOutline,
  MdRestartAlt,
} from 'react-icons/md';
import { LuSlidersHorizontal } from 'react-icons/lu';
import TriangleIcon from '../components/TriangleIcon';
import { API } from '../config';
import { getAppClockOverride, getAppNow, getAppNowMs } from '../utils/appClock';
import {
  cloneScopeInstitutions,
  EMPTY_PORTFOLIO_CHART_COLORS,
  PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
  PORTFOLIO_TIMEFRAMES,
  DEFAULT_PORTFOLIO_TIMEFRAME,
  formatOverviewChangeValue,
  formatChangePercentFull,
  formatChangePercentCompact,
  formatOverviewMoney,
  getAggregateHistoryMetricChange,
  formatPortfolioAccountTypeLabel,
  getAllocationInstitutionColorKeys,
  getPortfolioPaletteColor,
  loadPortfolioChartColors,
  normalizeChartColor,
  parseSyncedAt,
  persistPortfolioChartColors,
  resolvePortfolioNetWorthChartColor,
  timeAgo,
} from '../utils/portfolioViewUtils';
import { formatCompactMoney, formatSignedCompactMoney } from '../utils/format';
import FitMoney, { FitMetricValue } from '../components/FitMoney';
import Money from '../components/Money';
import StablePieTooltipEChart from '../components/charts/StablePieTooltipEChart';
import { CASH_FLOW_DONUT_HEIGHT, buildCfDonutOption, rollupByParent } from '../utils/cashFlowChart';
import CashFlowTimelineControl, {
  computeRange as computeCashFlowRange,
  computeLabel as computeCashFlowLabel,
  getInitialAnchorForPreset as getCashFlowInitialAnchor,
} from '../components/CashFlowTimelineControl';
import { buildFxHistoryIndex, cadPerUnitAtDate } from '../utils/currencyView';
import { useCurrency, useTheme } from '../appState';
import useBalancesHidden from '../hooks/useBalancesHidden';
import { persistVisibilityScope, reconcileVisibilityScope } from '../utils/visibilityScope';
import { readCashFlowResponse, readTransactionCollectionResponse } from '../utils/apiResponse';
import { getActiveSyncBatchConnectionStates } from '../utils/syncBatch';
import { resolveCategoryAccentColor } from '../utils/categoryColors';
import { resolveProviderSyncDisplay } from '../utils/syncDisplayState';
import { persistentStorage } from '../utils/persistentStorage';
import {
  filterDashboardNetWorthHistory,
  getDashboardCashFlowDisplayCurrency,
  getMarketStripTilePresentation,
} from '../utils/dashboardViewUtils';
import RecentTransactionsTable from '../components/RecentTransactionsTable';
import HorizontalScrollProxy from '../components/HorizontalScrollProxy';
import './Dashboard.css';

const MARKET_STRIP_REFRESH_MS = 15 * 60 * 1000;
const MARKET_NEWS_REFRESH_MS = 30 * 60 * 1000;
const MARKET_NEWS_EMPTY_RETRY_MS = 45 * 1000;
const MARKET_STRIP_CACHE_STORAGE_KEY = 'breaktwenty_market_strip_payload_v1';
const MARKET_NEWS_CACHE_STORAGE_KEY = 'breaktwenty_market_news_payload_v1';
const MARKET_STRIP_MAX_TILES = 7;
const MARKET_NEWS_MAX_ARTICLES = 3;
const ACTIVE_INSTITUTION_ACTIVITY_STATUSES = new Set(['queued', 'running']);
const AUTO_SYNC_OPTIMISTIC_TTL_MS = 2 * 60 * 1000;
const INSTITUTION_STATUS_SORT_DEFAULT_DIRECTIONS = {
  institution: 'asc',
  status: 'asc',
  balance: 'desc',
  updated: 'desc',
};
const INSTITUTION_STATUS_HORIZONTAL_SCROLL_PROXY_OPTIONS = {
  className: 'institution-status-horizontal-scrollbar app-horizontal-scroll-proxy',
  innerClassName: 'institution-status-horizontal-scrollbar-inner app-horizontal-scroll-proxy-inner',
  contentWidthProperty: '--app-horizontal-scroll-content-width',
  targetViewportProperty: '--app-horizontal-scroll-viewport-width',
  resetOnTargetWidthChange: true,
  clampTolerance: 0,
  resolveTarget: (controller) => controller.closest('.institution-status-table'),
  getContentElements: ({ target }) => [
    target.querySelector('.institution-status-scroll-surface'),
    target.querySelector('.institution-status-head'),
    target.querySelector('.institution-status-rows'),
  ],
  getObservedElements: ({ target, contentElements }) => [target, ...contentElements],
};

function InstitutionStatusHorizontalScrollProxy() {
  return <HorizontalScrollProxy options={INSTITUTION_STATUS_HORIZONTAL_SCROLL_PROXY_OPTIONS} />;
}

function usePortalElementById(elementId) {
  const [element, setElement] = useState(null);

  useEffect(() => {
    if (typeof document === 'undefined' || typeof window === 'undefined') {
      return undefined;
    }

    let cancelled = false;
    const frameId = window.requestAnimationFrame(() => {
      if (!cancelled) {
        setElement(document.getElementById(elementId));
      }
    });

    return () => {
      cancelled = true;
      window.cancelAnimationFrame(frameId);
    };
  }, [elementId]);

  return element;
}
const INSTITUTION_STATUS_SORT_STORAGE_KEY = 'breaktwenty_dashboard_institution_status_sort_v1';
const EMPTY_MARKET_STRIP_TILES = [];
// v3: default layout moves Assets & Liabilities to the right main column above
// Accounts Overview. Bump the key so existing v2 local layouts get this new
// default instead of leaving the widget appended at the end.
const DASHBOARD_LAYOUT_STORAGE_KEY = 'breaktwenty_dashboard_layout_v3';
const RECENT_TRANSACTIONS_LIMIT = 10;
const DASHBOARD_WIDGET_DEFINITIONS = [
  { id: 'market-strip', label: 'Market Strip', kind: 'wide', zone: 'top' },
  { id: 'net-worth', label: 'Net Worth', kind: 'metric', zone: 'metrics' },
  { id: 'assets', label: 'Total Assets', kind: 'metric', zone: 'metrics' },
  { id: 'liabilities', label: 'Total Liabilities', kind: 'metric', zone: 'metrics' },
  { id: 'net-worth-history', label: 'Net Worth History', kind: 'wide', zone: 'net-worth-history' },
  { id: 'market-news', label: 'Market Updates', kind: 'wide', zone: 'market-news' },
  { id: 'cash-flow', label: 'Cash Flow', kind: 'half', zone: 'main', column: 'left' },
  { id: 'recent-transactions', label: 'Recent Transactions', kind: 'half', zone: 'main', column: 'left' },
  { id: 'asset-allocation', label: 'Assets & Liabilities', kind: 'half', zone: 'main', column: 'right' },
  { id: 'institution-status', label: 'Accounts Overview', kind: 'half', zone: 'main', column: 'right' },
];
const CASH_FLOW_PANEL_PERIOD_KEY = 'month';
const CASH_FLOW_PANEL_TOP_CATEGORIES = 5;
const DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT = 7;
const CASH_FLOW_PANEL_VIEWS = [
  { key: 'cashflow', label: 'Net Cash Flow' },
  { key: 'income', label: 'Income' },
  { key: 'expense', label: 'Expense' },
];
const NETWORTH_HISTORY_METHODOLOGY = 'The graph reconstructs the full history of each account — banks from their transactions, brokerages from daily NAV, and manual assets from the values you record. Change values over a specific period track this graph exactly — All Time runs from your earliest recorded value to today — so the figures always match the line\'s rise and fall, and the percentage is that change relative to the period\'s starting value.';

const DASHBOARD_TOP_BAND_IDS = ['top', 'market-news', 'metrics', 'net-worth-history'];

const DASHBOARD_TOP_BAND_LABELS = {
  top: 'Market Strip',
  'market-news': 'Market Updates',
  metrics: 'Metrics',
  'net-worth-history': 'Net Worth History',
};
const DASHBOARD_WIDGET_IDS = DASHBOARD_WIDGET_DEFINITIONS.map((widget) => widget.id);
const DASHBOARD_WIDGET_BY_ID = new Map(DASHBOARD_WIDGET_DEFINITIONS.map((widget) => [widget.id, widget]));
const DASHBOARD_DEFAULT_VISIBILITY = Object.fromEntries(DASHBOARD_WIDGET_IDS.map((id) => [id, true]));
const DASHBOARD_MAIN_CUSTOMIZE_GROUPS = [
  { id: 'main:left', label: 'Left Column' },
  { id: 'main:right', label: 'Right Column' },
];

function getMostRecentSyncedAt(accounts) {
  return accounts.reduce((latest, account) => {
    const parsed = parseSyncedAt(account.last_synced);
    if (!parsed) return latest;
    if (!latest.parsed || parsed > latest.parsed) {
      return { parsed, value: account.last_synced };
    }
    return latest;
  }, { parsed: null, value: null }).value;
}

function getMostRecentDateString(primaryDateStr, fallbackDateStr) {
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

function formatInstitutionNetBalance(amount, currency = 'CAD') {
  const numeric = Number(amount);
  if (!Number.isFinite(numeric)) return '--';
  return new Intl.NumberFormat('en-CA', {
    style: 'currency',
    currency,
    currencyDisplay: 'narrowSymbol',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(numeric);
}

function getActiveInstitutionTargets(syncActivity) {
  const providers = new Set();
  const institutionIds = new Set();
  const activities = Array.isArray(syncActivity?.active) ? syncActivity.active : [];
  activities.forEach((activity) => {
    const provider = String(activity?.provider || '').trim();
    const status = String(activity?.status || '').trim().toLowerCase();
    if (provider && ACTIVE_INSTITUTION_ACTIVITY_STATUSES.has(status)) {
      providers.add(provider);
      const institutionId = Number(activity?.institution_id || 0);
      if (institutionId > 0) institutionIds.add(institutionId);
    }
  });
  return { providers, institutionIds };
}

function compareOptionalNumber(leftValue, rightValue) {
  const leftNumber = Number(leftValue);
  const rightNumber = Number(rightValue);
  const leftMissing = !Number.isFinite(leftNumber);
  const rightMissing = !Number.isFinite(rightNumber);
  if (leftMissing && rightMissing) return 0;
  if (leftMissing) return -1;
  if (rightMissing) return 1;
  return leftNumber - rightNumber;
}

function compareInstitutionStatusDefault(left, right) {
  if (left.status.priority !== right.status.priority) {
    return left.status.priority - right.status.priority;
  }
  const balanceSort = Math.abs(right.netBalance) - Math.abs(left.netBalance);
  if (balanceSort !== 0) return balanceSort;
  return left.name.localeCompare(right.name);
}

function compareInstitutionStatusRows(left, right, sortConfig) {
  let result = 0;
  if (sortConfig?.key === 'institution') {
    result = left.name.localeCompare(right.name);
  } else if (sortConfig?.key === 'status') {
    result = compareOptionalNumber(left.status.priority, right.status.priority)
      || left.status.label.localeCompare(right.status.label);
  } else if (sortConfig?.key === 'balance') {
    result = compareOptionalNumber(left.netBalance, right.netBalance);
  } else if (sortConfig?.key === 'updated') {
    result = compareOptionalNumber(left.updatedAt, right.updatedAt);
  }

  if (result !== 0) {
    return sortConfig.direction === 'desc' ? -result : result;
  }
  return compareInstitutionStatusDefault(left, right);
}

function getDashboardWidgetLayoutGroup(widgetId, columns = {}) {
  const widget = DASHBOARD_WIDGET_BY_ID.get(widgetId);
  if (!widget) return 'main:left';
  if (widget.zone === 'main') {
    const column = columns[widgetId] === 'right' || columns[widgetId] === 'left'
      ? columns[widgetId]
      : widget.column || 'left';
    return `main:${column}`;
  }
  return widget.zone || 'main:left';
}

function isDashboardMainGroup(layoutGroup) {
  return layoutGroup === 'main:left' || layoutGroup === 'main:right';
}

function getDashboardColumnFromGroup(layoutGroup) {
  return layoutGroup === 'main:right' ? 'right' : 'left';
}

function normalizeDashboardLayout(layout) {
  const rawOrder = Array.isArray(layout?.order) ? layout.order : [];
  const seenIds = new Set();
  const order = rawOrder.filter((id) => {
    if (!DASHBOARD_WIDGET_BY_ID.has(id) || seenIds.has(id)) return false;
    seenIds.add(id);
    return true;
  });
  DASHBOARD_WIDGET_IDS.forEach((id) => {
    if (!seenIds.has(id)) order.push(id);
  });

  const sourceVisibility = layout?.visible && typeof layout.visible === 'object'
    ? layout.visible
    : DASHBOARD_DEFAULT_VISIBILITY;
  const visible = Object.fromEntries(
    DASHBOARD_WIDGET_IDS.map((id) => [id, sourceVisibility[id] !== false])
  );

  const sourceColumns = layout?.columns && typeof layout.columns === 'object'
    ? layout.columns
    : {};
  const columns = Object.fromEntries(
    DASHBOARD_WIDGET_DEFINITIONS
      .filter((widget) => widget.zone === 'main')
      .map((widget) => {
        const column = sourceColumns[widget.id] === 'right' || sourceColumns[widget.id] === 'left'
          ? sourceColumns[widget.id]
          : widget.column || 'left';
        return [widget.id, column];
      })
  );

  const rawBandOrder = Array.isArray(layout?.bandOrder) ? layout.bandOrder : [];
  const seenBands = new Set();
  const bandOrder = rawBandOrder.filter((id) => {
    if (!DASHBOARD_TOP_BAND_IDS.includes(id) || seenBands.has(id)) return false;
    seenBands.add(id);
    return true;
  });
  DASHBOARD_TOP_BAND_IDS.forEach((id, canonicalIndex) => {
    if (seenBands.has(id)) return;
    seenBands.add(id);
    let insertAt = bandOrder.length;
    for (let i = canonicalIndex - 1; i >= 0; i -= 1) {
      const precedingIndex = bandOrder.indexOf(DASHBOARD_TOP_BAND_IDS[i]);
      if (precedingIndex >= 0) {
        insertAt = precedingIndex + 1;
        break;
      }
    }
    bandOrder.splice(insertAt, 0, id);
  });

  return { order, visible, columns, bandOrder };
}

function moveDashboardWidgetWithinGroup(layout, widgetId, offset) {
  const normalizedLayout = normalizeDashboardLayout(layout);
  const group = getDashboardWidgetLayoutGroup(widgetId, normalizedLayout.columns);
  const groupIds = normalizedLayout.order.filter(
    (id) => getDashboardWidgetLayoutGroup(id, normalizedLayout.columns) === group
  );
  const currentIndex = groupIds.indexOf(widgetId);
  const nextIndex = currentIndex + offset;
  if (currentIndex < 0 || nextIndex < 0 || nextIndex >= groupIds.length) return normalizedLayout.order;
  return moveDashboardWidgetRelative(
    normalizedLayout.order,
    widgetId,
    groupIds[nextIndex],
    offset > 0 ? 'after' : 'before',
  );
}

function loadDashboardLayout() {
  try {
    return normalizeDashboardLayout(JSON.parse(persistentStorage.getItem(DASHBOARD_LAYOUT_STORAGE_KEY) || 'null'));
  } catch (_) {
    return normalizeDashboardLayout(null);
  }
}

function persistDashboardLayout(layout) {
  try {
    persistentStorage.setItem(DASHBOARD_LAYOUT_STORAGE_KEY, JSON.stringify(normalizeDashboardLayout(layout)));
  } catch (_) {
  }
}

function moveDashboardWidgetRelative(order, activeId, targetId, position = 'before') {
  if (!activeId || !targetId || activeId === targetId) return order;
  const normalizedOrder = normalizeDashboardLayout({ order }).order;
  if (!normalizedOrder.includes(activeId) || !normalizedOrder.includes(targetId)) return normalizedOrder;
  const withoutActive = normalizedOrder.filter((id) => id !== activeId);
  const targetIndex = withoutActive.indexOf(targetId);
  if (targetIndex < 0) return normalizedOrder;
  const insertIndex = position === 'after' ? targetIndex + 1 : targetIndex;
  return [
    ...withoutActive.slice(0, insertIndex),
    activeId,
    ...withoutActive.slice(insertIndex),
  ];
}

function getDashboardWidgetDropPosition(event) {
  const rect = event.currentTarget.getBoundingClientRect();
  return event.clientY >= rect.top + (rect.height / 2) ? 'after' : 'before';
}

function getDashboardWidgetContainerDropIntent(container, clientY) {
  const widgetElements = Array.from(container.querySelectorAll('[data-dashboard-widget-id]'));
  if (widgetElements.length === 0) return null;

  for (const widgetElement of widgetElements) {
    const rect = widgetElement.getBoundingClientRect();
    if (clientY < rect.top + (rect.height / 2)) {
      return { targetId: widgetElement.dataset.dashboardWidgetId, position: 'before' };
    }
  }

  const lastWidget = widgetElements[widgetElements.length - 1];
  return { targetId: lastWidget.dataset.dashboardWidgetId, position: 'after' };
}

function getDashboardWidgetElementFromEvent(event, container) {
  const target = event.target;
  if (!(target instanceof Element)) return null;
  const widgetElement = target.closest('[data-dashboard-widget-id]');
  return widgetElement && container.contains(widgetElement) ? widgetElement : null;
}

function formatMarketStripValue(value, fractionDigits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const digits = Math.max(0, Math.min(6, Number(fractionDigits) || 2));
  return new Intl.NumberFormat('en-CA', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

function MarketStripSparkline({ tile, tone }) {
  const rawValues = (Array.isArray(tile?.points) ? tile.points : [])
    .map((point) => Number(point))
    .filter((point) => Number.isFinite(point));
  const latestValue = Number(tile?.value);
  const change = Number(tile?.change);
  const explicitReferenceValue = Number(tile?.reference_value);
  const referenceValue = Number.isFinite(explicitReferenceValue)
    ? explicitReferenceValue
    : Number.isFinite(latestValue) && Number.isFinite(change)
      ? latestValue - change
      : null;
  const referencePair = Number.isFinite(referenceValue) && Number.isFinite(latestValue)
    ? [referenceValue, latestValue]
    : rawValues;
  const hasCurrentDayPathPoints = tile?.point_mode === 'intraday' || tile?.point_mode === 'snapshot';
  const values = tile?.point_mode === 'daily'
    ? rawValues
    : hasCurrentDayPathPoints
    ? rawValues.length >= 2 ? rawValues : referencePair
    : rawValues;
  const width = 74;
  const height = 34;
  const padding = 3;

  if (values.length < 2) {
    return <span className="market-strip-sparkline-empty" aria-hidden="true" />;
  }

  const domainValues = Number.isFinite(referenceValue) ? [...values, referenceValue] : values;
  const min = Math.min(...domainValues);
  const max = Math.max(...domainValues);
  const span = max - min || Math.max(Math.abs(max), 1);
  const yForValue = (value) => height - padding - ((value - min) / span) * (height - padding * 2);
  const step = (width - padding * 2) / Math.max(values.length - 1, 1);
  const path = values.map((value, index) => {
    const x = padding + index * step;
    const y = yForValue(value);
    return `${index === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(' ');
  const referenceY = Number.isFinite(referenceValue) ? yForValue(referenceValue) : null;

  return (
    <svg className={`market-strip-sparkline is-${tone}`.trim()} viewBox={`0 0 ${width} ${height}`} aria-hidden="true" focusable="false">
      {Number.isFinite(referenceY) ? (
        <line
          className="market-strip-sparkline-baseline"
          x1={padding}
          x2={width - padding}
          y1={referenceY.toFixed(2)}
          y2={referenceY.toFixed(2)}
        />
      ) : null}
      <path className="market-strip-sparkline-fill" d={`${path} L${width - padding} ${height - padding} L${padding} ${height - padding} Z`} />
      <path className="market-strip-sparkline-line" d={path} />
    </svg>
  );
}

function normalizeMarketStripSearchSymbol(value) {
  return String(value || '').trim().toUpperCase().slice(0, 32);
}

function normalizeCachedMarketStripPayload(payload, status = 'cached') {
  if (!payload || !Array.isArray(payload.tiles) || payload.tiles.length === 0) return null;
  return {
    status,
    updated_at: payload.updated_at || null,
    refresh_seconds: payload.refresh_seconds || null,
    market_data: payload.market_data || null,
    tiles: payload.tiles.slice(0, MARKET_STRIP_MAX_TILES),
    watchlist: Array.isArray(payload.watchlist) ? payload.watchlist.slice(0, MARKET_STRIP_MAX_TILES) : [],
    available_tiles: Array.isArray(payload.available_tiles) ? payload.available_tiles : [],
  };
}

function loadCachedMarketStripPayload() {
  if (getAppClockOverride()) return null;
  try {
    const cached = JSON.parse(persistentStorage.getItem(MARKET_STRIP_CACHE_STORAGE_KEY) || 'null');
    return normalizeCachedMarketStripPayload(cached);
  } catch (_) {
    return null;
  }
}

function persistMarketStripPayload(payload) {
  if (getAppClockOverride()) return;
  const cacheablePayload = normalizeCachedMarketStripPayload(payload, 'ok');
  if (!cacheablePayload) return;
  try {
    persistentStorage.setItem(MARKET_STRIP_CACHE_STORAGE_KEY, JSON.stringify(cacheablePayload));
  } catch (_) {
  }
}

function normalizeCachedMarketNewsPayload(payload) {
  if (!payload || !Array.isArray(payload.articles) || payload.articles.length === 0) return null;
  return {
    status: 'ok',
    articles: payload.articles.slice(0, MARKET_NEWS_MAX_ARTICLES),
  };
}

function loadCachedMarketNewsPayload() {
  if (getAppClockOverride()) return null;
  try {
    const cached = JSON.parse(persistentStorage.getItem(MARKET_NEWS_CACHE_STORAGE_KEY) || 'null');
    return normalizeCachedMarketNewsPayload(cached);
  } catch (_) {
    return null;
  }
}

function persistMarketNewsPayload(payload) {
  if (getAppClockOverride()) return;
  const cacheablePayload = normalizeCachedMarketNewsPayload(payload);
  if (!cacheablePayload) return;
  try {
    persistentStorage.setItem(MARKET_NEWS_CACHE_STORAGE_KEY, JSON.stringify(cacheablePayload));
  } catch (_) {
  }
}

function getReorderedMarketStripIds(order, activeId, insertIndex) {
  const currentOrder = Array.isArray(order) ? order.filter(Boolean) : [];
  const withoutActive = currentOrder.filter((id) => id !== activeId);
  const nextIndex = Math.max(0, Math.min(withoutActive.length, insertIndex));
  return [
    ...withoutActive.slice(0, nextIndex),
    activeId,
    ...withoutActive.slice(nextIndex),
  ];
}

function marketStripOrdersMatch(firstOrder, secondOrder) {
  if (!Array.isArray(firstOrder) || !Array.isArray(secondOrder)) return false;
  if (firstOrder.length !== secondOrder.length) return false;
  return firstOrder.every((id, index) => id === secondOrder[index]);
}

function getMarketStripInsertIndex(clientX, order, activeId, rectsById) {
  const comparisonIds = order.filter((id) => id !== activeId);
  const insertIndex = comparisonIds.findIndex((id) => {
    const rect = rectsById.get(id);
    return rect && clientX < rect.left + rect.width / 2;
  });
  return insertIndex >= 0 ? insertIndex : comparisonIds.length;
}

function getMarketStripDropRect(order, activeId, targetIndex, originalIndex, rectsById, fallbackRect) {
  if (targetIndex === originalIndex) return fallbackRect;
  const comparisonIds = order.filter((id) => id !== activeId);
  const targetId = targetIndex < originalIndex
    ? comparisonIds[targetIndex]
    : comparisonIds[targetIndex - 1];
  return rectsById.get(targetId) || fallbackRect;
}

function MarketStripTileContents({ tile, presentation }) {
  return (
    <>
      <div className="market-strip-copy">
        <div className="market-strip-label">{tile.label}</div>
        <div className="market-strip-value">{formatMarketStripValue(tile.value, presentation.precision)}</div>
        <div className="market-strip-change">{presentation.changeCopy}</div>
        {presentation.asOfCopy ? <div className="market-strip-as-of">{presentation.asOfCopy}</div> : null}
      </div>
      <MarketStripSparkline tile={tile} tone={presentation.tone} />
    </>
  );
}

function MarketStrip({ payload, onWatchlistChange }) {
  const rawTiles = Array.isArray(payload?.tiles) ? payload.tiles : EMPTY_MARKET_STRIP_TILES;
  const tiles = rawTiles.length > MARKET_STRIP_MAX_TILES ? rawTiles.slice(0, MARKET_STRIP_MAX_TILES) : rawTiles;
  const isKeylessMarketData = payload?.market_data?.mode === 'keyless';
  const canSearchMarketStrip = !isKeylessMarketData && Boolean(payload?.market_data?.has_key);
  const [isConfigOpen, setIsConfigOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const configRef = useRef(null);
  const stripRef = useRef(null);
  const tileRefs = useRef(new Map());
  const dragPreviewRef = useRef(null);
  const dragDataRef = useRef(null);
  const dragAnimationFrameRef = useRef(null);
  const dropCommitTimeoutRef = useRef(null);
  const [dragState, setDragState] = useState(null);
  const [optimisticTileOrder, setOptimisticTileOrder] = useState(null);
  const watchlist = useMemo(
    () => (Array.isArray(payload?.watchlist) ? payload.watchlist : []),
    [payload]
  );
  const tileOrder = useMemo(
    () => tiles.map((tile) => tile?.id).filter(Boolean),
    [tiles]
  );
  const tileById = useMemo(
    () => new Map(tiles.filter(Boolean).map((tile) => [tile.id, tile])),
    [tiles]
  );
  const baseTileOrder = optimisticTileOrder || tileOrder;
  const visibleTiles = useMemo(() => {
    const previewOrder = optimisticTileOrder;
    if (!Array.isArray(previewOrder) || !previewOrder.length) return tiles;
    const orderedIds = new Set();
    const orderedTiles = previewOrder
      .map((id) => {
        orderedIds.add(id);
        return tileById.get(id);
      })
      .filter(Boolean);
    tiles.forEach((tile) => {
      if (!orderedIds.has(tile.id)) {
        orderedTiles.push(tile);
      }
    });
    return orderedTiles;
  }, [optimisticTileOrder, tileById, tiles]);
  const draggedTile = useMemo(
    () => tiles.find((tile) => tile?.id === dragState?.tileId) || null,
    [dragState?.tileId, tiles]
  );
  const draggedTilePresentation = draggedTile
    ? getMarketStripTilePresentation(draggedTile, isKeylessMarketData)
    : null;
  const selectedIds = useMemo(
    () => new Set(watchlist.map((item) => item?.id).filter(Boolean)),
    [watchlist]
  );
  const hasReachedMarketStripLimit = selectedIds.size >= MARKET_STRIP_MAX_TILES;
  const watchlistIndexById = useMemo(() => {
    const indexById = new Map();
    watchlist.forEach((item, index) => {
      if (item?.id) {
        indexById.set(item.id, index);
      }
    });
    return indexById;
  }, [watchlist]);

  useEffect(() => {
    let cancelled = false;
    const query = search.trim();
    if (!isConfigOpen || !canSearchMarketStrip || !query) {
      Promise.resolve().then(() => {
        if (!cancelled) {
          setSearchResults([]);
        }
      });
      return () => {
        cancelled = true;
      };
    }

    const controller = new AbortController();
    const timeoutId = window.setTimeout(async () => {
      try {
        const response = await fetch(`${API}/market-data/search?q=${encodeURIComponent(query)}`, {
          signal: controller.signal,
        });
        const payload = await response.json();
        if (!cancelled) {
          setSearchResults(Array.isArray(payload?.results) ? payload.results : []);
        }
      } catch (error) {
        if (!cancelled && error?.name !== 'AbortError') {
          setSearchResults([]);
        }
      }
    }, 150);

    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
      controller.abort();
    };
  }, [canSearchMarketStrip, isConfigOpen, search]);

  const availableOptions = useMemo(() => {
    const byId = new Map();
    const mergeOption = (item) => {
      if (item?.id && !byId.has(item.id)) byId.set(item.id, item);
    };
    (Array.isArray(payload?.available_tiles) ? payload.available_tiles : []).forEach(mergeOption);
    if (canSearchMarketStrip && search.trim()) {
      searchResults.forEach(mergeOption);
    }
    watchlist.forEach((item) => {
      if (!item?.id || byId.has(item.id)) return;
      if (!canSearchMarketStrip) return;
      const tile = tileById.get(item.id);
      const fallbackLabel = item.id.replace(/^custom:/, '');
      byId.set(item.id, {
        id: item.id,
        label: item.label || tile?.label || item.symbol || tile?.symbol || fallbackLabel,
        symbol: item.symbol || tile?.symbol || fallbackLabel,
        custom: Boolean(item.custom || item.id.startsWith('custom:')),
      });
    });
    return Array.from(byId.values());
  }, [canSearchMarketStrip, payload, search, searchResults, tileById, watchlist]);
  const filteredOptions = useMemo(() => {
    const query = canSearchMarketStrip ? search.trim().toUpperCase() : '';
    const filtered = query
      ? availableOptions.filter((item) => (
        `${item.label || ''} ${item.symbol || ''} ${item.fred_symbol || ''}`.toUpperCase().includes(query)
      ))
      : [...availableOptions];
    const customSymbol = normalizeMarketStripSearchSymbol(search);
    const customId = customSymbol ? `custom:${customSymbol}` : '';
    if (canSearchMarketStrip && customSymbol && !filtered.some((item) => item.id === customId)) {
      filtered.push({
        id: customId,
        label: customSymbol,
        symbol: customSymbol,
        custom: true,
      });
    }
    return filtered
      .sort((a, b) => {
        const aIndex = watchlistIndexById.has(a.id) ? watchlistIndexById.get(a.id) : null;
        const bIndex = watchlistIndexById.has(b.id) ? watchlistIndexById.get(b.id) : null;
        if (aIndex !== null && bIndex !== null) return aIndex - bIndex;
        if (aIndex !== null) return -1;
        if (bIndex !== null) return 1;
        return 0;
      })
      .slice(0, 16);
  }, [availableOptions, canSearchMarketStrip, search, watchlistIndexById]);

  useEffect(() => {
    if (!isConfigOpen) {
      return undefined;
    }

    const handlePointerDown = (event) => {
      if (event.target?.closest?.(APP_NON_DISMISS_INTERACTION_SELECTOR)) {
        return;
      }
      if (!configRef.current?.contains(event.target)) {
        setIsConfigOpen(false);
      }
    };
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        setIsConfigOpen(false);
      }
    };

    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isConfigOpen]);

  useEffect(() => () => {
    if (dropCommitTimeoutRef.current) {
      window.clearTimeout(dropCommitTimeoutRef.current);
    }
    if (dragAnimationFrameRef.current) {
      window.cancelAnimationFrame(dragAnimationFrameRef.current);
    }
  }, []);

  useEffect(() => {
    if (optimisticTileOrder && marketStripOrdersMatch(tileOrder, optimisticTileOrder)) {
      Promise.resolve().then(() => setOptimisticTileOrder(null));
    }
  }, [optimisticTileOrder, tileOrder]);

  const toggleOption = (option) => {
    if (!option?.id || typeof onWatchlistChange !== 'function') return;
    if (!selectedIds.has(option.id) && hasReachedMarketStripLimit) return;
    setOptimisticTileOrder(null);
    const nextItems = selectedIds.has(option.id)
      ? watchlist.filter((item) => item.id !== option.id)
      : [
        ...watchlist,
        {
          id: option.id,
          ...(option.custom ? { symbol: option.symbol, label: option.label } : {}),
        },
      ];
    onWatchlistChange(nextItems);
  };

  const isPointInsideStrip = (clientX, clientY) => {
    const rect = dragDataRef.current?.stripRect || stripRef.current?.getBoundingClientRect();
    return Boolean(
      rect
      && clientX >= rect.left
      && clientX <= rect.right
      && clientY >= rect.top
      && clientY <= rect.bottom
    );
  };

  const moveDragPreview = (clientX, clientY, scale = 1.03) => {
    const data = dragDataRef.current;
    if (!data) return;
    data.currentX = clientX;
    data.currentY = clientY;
    if (dragAnimationFrameRef.current) return;
    dragAnimationFrameRef.current = window.requestAnimationFrame(() => {
      dragAnimationFrameRef.current = null;
      const preview = dragPreviewRef.current;
      const currentData = dragDataRef.current;
      if (!preview || !currentData) return;
      const left = currentData.currentX - currentData.pointerOffsetX;
      const top = currentData.currentY - currentData.pointerOffsetY;
      preview.style.transition = 'none';
      preview.style.transform = `translate3d(${left}px, ${top}px, 0) scale(${scale})`;
    });
  };

  const commitMarketStripOrder = (order) => {
    if (!Array.isArray(order) || !order.length || typeof onWatchlistChange !== 'function') return;
    const orderedIds = new Set(order);
    const watchlistById = new Map(watchlist.map((item) => [item?.id, item]));
    const nextItems = [
      ...order.map((id) => watchlistById.get(id)).filter(Boolean),
      ...watchlist.filter((item) => !orderedIds.has(item?.id)),
    ];
    onWatchlistChange(nextItems);
  };

  const beginMarketStripDrag = (event, tileId) => {
    if (event.button !== 0 || baseTileOrder.length < 2 || typeof onWatchlistChange !== 'function') return;
    if (dropCommitTimeoutRef.current) {
      window.clearTimeout(dropCommitTimeoutRef.current);
      dropCommitTimeoutRef.current = null;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const stripRect = stripRef.current?.getBoundingClientRect();
    const rectsById = new Map();
    baseTileOrder.forEach((id) => {
      const tileNode = tileRefs.current.get(id);
      if (!tileNode) return;
      const tileRect = tileNode.getBoundingClientRect();
      rectsById.set(id, {
        left: tileRect.left,
        top: tileRect.top,
        width: tileRect.width,
        height: tileRect.height,
      });
    });
    const originalIndex = baseTileOrder.indexOf(tileId);
    const dragData = {
      tileId,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      currentX: event.clientX,
      currentY: event.clientY,
      pointerOffsetX: event.clientX - rect.left,
      pointerOffsetY: event.clientY - rect.top,
      width: rect.width,
      height: rect.height,
      originalIndex,
      targetIndex: originalIndex,
      originalOrder: [...baseTileOrder],
      rectsById,
      activeRect: {
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
      },
      stripRect,
      isDragging: false,
      isOutside: false,
    };
    dragDataRef.current = dragData;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setDragState({
      tileId,
      pointerId: event.pointerId,
      currentX: event.clientX,
      currentY: event.clientY,
      pointerOffsetX: event.clientX - rect.left,
      pointerOffsetY: event.clientY - rect.top,
      width: rect.width,
      height: rect.height,
      isDragging: false,
      isDropping: false,
      isOutside: false,
      originalIndex,
      targetIndex: originalIndex,
    });
  };

  const updateMarketStripDrag = (event, tileId) => {
    const data = dragDataRef.current;
    if (!data || data.tileId !== tileId || data.pointerId !== event.pointerId || dragState?.isDropping) return;
    const deltaX = event.clientX - data.startX;
    const deltaY = event.clientY - data.startY;
    const isDragging = data.isDragging || Math.hypot(deltaX, deltaY) > 5;
    if (!isDragging) return;
    event.preventDefault();
    data.isDragging = true;
    moveDragPreview(event.clientX, event.clientY);
    const isInside = isPointInsideStrip(event.clientX, event.clientY);
    const targetIndex = isInside
      ? getMarketStripInsertIndex(event.clientX, data.originalOrder, tileId, data.rectsById)
      : data.targetIndex;
    if (
      dragState?.isDragging !== true
      || dragState?.targetIndex !== targetIndex
      || dragState?.isOutside !== !isInside
    ) {
      data.targetIndex = targetIndex;
      data.isOutside = !isInside;
      setDragState((previous) => ({
        ...(previous || {}),
        tileId,
        pointerId: event.pointerId,
        currentX: event.clientX,
        currentY: event.clientY,
        pointerOffsetX: data.pointerOffsetX,
        pointerOffsetY: data.pointerOffsetY,
        width: data.width,
        height: data.height,
        isDragging: true,
        isDropping: false,
        isOutside: !isInside,
        originalIndex: data.originalIndex,
        targetIndex,
      }));
    }
  };

  const finishMarketStripDrag = (event, tileId) => {
    const data = dragDataRef.current;
    if (!data || data.tileId !== tileId || data.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (!data.isDragging) {
      dragDataRef.current = null;
      setDragState(null);
      return;
    }

    moveDragPreview(event.clientX, event.clientY);
    const shouldCommit = isPointInsideStrip(event.clientX, event.clientY);
    const targetIndex = shouldCommit
      ? getMarketStripInsertIndex(event.clientX, data.originalOrder, tileId, data.rectsById)
      : data.originalIndex;
    const finalOrder = getReorderedMarketStripIds(data.originalOrder, tileId, targetIndex);
    const targetRect = getMarketStripDropRect(
      data.originalOrder,
      tileId,
      targetIndex,
      data.originalIndex,
      data.rectsById,
      data.activeRect,
    );
    data.targetIndex = targetIndex;
    data.isOutside = false;
    setDragState((previous) => ({
      ...(previous || {}),
      tileId,
      pointerId: event.pointerId,
      pointerOffsetX: data.pointerOffsetX,
      pointerOffsetY: data.pointerOffsetY,
      width: data.width,
      height: data.height,
      isDragging: true,
      isDropping: true,
      isOutside: false,
      originalIndex: data.originalIndex,
      targetIndex,
    }));

    const preview = dragPreviewRef.current;
    if (preview && targetRect) {
      if (dragAnimationFrameRef.current) {
        window.cancelAnimationFrame(dragAnimationFrameRef.current);
        dragAnimationFrameRef.current = null;
      }
      window.requestAnimationFrame(() => {
        preview.style.transition = 'transform 170ms cubic-bezier(0.2, 0.8, 0.2, 1)';
        preview.style.transform = `translate3d(${targetRect.left}px, ${targetRect.top}px, 0) scale(1)`;
      });
    }

    if (dropCommitTimeoutRef.current) {
      window.clearTimeout(dropCommitTimeoutRef.current);
    }
    dropCommitTimeoutRef.current = window.setTimeout(() => {
      dropCommitTimeoutRef.current = null;
      if (shouldCommit) {
        setOptimisticTileOrder(finalOrder);
      }
      dragDataRef.current = null;
      setDragState(null);
      if (shouldCommit && !marketStripOrdersMatch(data.originalOrder, finalOrder)) {
        commitMarketStripOrder(finalOrder);
      }
    }, 180);
  };

  const cancelMarketStripDrag = (event, tileId) => {
    const data = dragDataRef.current;
    if (!data || data.tileId !== tileId || data.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (dropCommitTimeoutRef.current) {
      window.clearTimeout(dropCommitTimeoutRef.current);
      dropCommitTimeoutRef.current = null;
    }
    if (dragAnimationFrameRef.current) {
      window.cancelAnimationFrame(dragAnimationFrameRef.current);
      dragAnimationFrameRef.current = null;
    }
    dragDataRef.current = null;
    setDragState(null);
  };

  if (!tiles.length) {
    return null;
  }

  return (
    <section
      className={`panel-shell market-strip ${dragState?.isDragging ? 'is-dragging' : ''} ${dragState?.isOutside ? 'is-drag-outside' : ''}`.trim()}
      aria-label="Market data"
      ref={stripRef}
    >
      <div className="market-strip-header" ref={configRef}>
        <button
          type="button"
          className={`market-strip-config-trigger app-control-root ${isConfigOpen ? 'is-open' : ''}`.trim()}
          aria-label="Customize market strip"
          aria-haspopup="dialog"
          aria-expanded={isConfigOpen}
          onClick={() => setIsConfigOpen((previous) => !previous)}
        >
          <span className="app-control-icon" aria-hidden="true"><LuSlidersHorizontal /></span>
        </button>
        <div
          className={`market-strip-config-panel ${isConfigOpen ? 'is-open' : ''}`.trim()}
          role="dialog"
          aria-label="Customize market strip"
          aria-hidden={!isConfigOpen}
        >
          {isConfigOpen && (
            <>
              {canSearchMarketStrip ? (
                <div className="market-strip-config-row">
                  <input
                    type="search"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Search ticker, crypto, ETF, forex, or FRED:SERIES"
                  />
                </div>
              ) : null}
              <div className="market-strip-option-list transactions-toolbar-menu-list" role="menu" aria-label="Market data tiles">
                {filteredOptions.map((option) => {
                  const isSelected = selectedIds.has(option.id);
                  const optionName = option.label || option.symbol || option.id;
                  const isDisabled = !isSelected && hasReachedMarketStripLimit;
                  return (
                    <button
                      key={option.id}
                      type="button"
                      role="menuitemcheckbox"
                      aria-checked={isSelected}
                      aria-disabled={isDisabled}
                      disabled={isDisabled}
                      className={`market-strip-option transactions-toolbar-menu-item ${isSelected ? 'is-selected' : ''} ${isDisabled ? 'is-disabled' : ''}`.trim()}
                      onClick={() => toggleOption(option)}
                    >
                      <span className="market-strip-option-copy transactions-toolbar-menu-item-copy">
                        <span className="market-strip-option-label">{optionName}</span>
                        <span className="market-strip-option-symbol">{option.symbol || option.fred_symbol || option.id}</span>
                      </span>
                      <span className={`transactions-toolbar-checkbox ${isSelected ? 'is-selected' : ''}`.trim()} aria-hidden="true">
                        {isSelected ? <MdCheck size={14} /> : option.custom ? <MdAdd size={14} /> : null}
                      </span>
                    </button>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </div>
      <div className="market-strip-tape">
        {visibleTiles.map((tile) => {
            const presentation = getMarketStripTilePresentation(tile, isKeylessMarketData);
            const isActiveDragTile = dragState?.tileId === tile.id && dragState?.isDragging;
            const tileIndex = baseTileOrder.indexOf(tile.id);
            let shift = 0;
            if (dragState?.isDragging && !isActiveDragTile && tileIndex >= 0) {
              if (dragState.targetIndex > dragState.originalIndex && tileIndex > dragState.originalIndex && tileIndex <= dragState.targetIndex) {
                shift = -dragState.width;
              } else if (dragState.targetIndex < dragState.originalIndex && tileIndex >= dragState.targetIndex && tileIndex < dragState.originalIndex) {
                shift = dragState.width;
              }
            }

            return (
              <article
                key={tile.id}
                ref={(node) => {
                  if (node) {
                    tileRefs.current.set(tile.id, node);
                  } else {
                    tileRefs.current.delete(tile.id);
                  }
                }}
                data-market-strip-tile-id={tile.id}
                className={`market-strip-tile is-${presentation.tone} ${isActiveDragTile ? 'is-drag-placeholder' : ''}`.trim()}
                style={{ '--market-strip-shift': `${shift}px` }}
                onPointerDown={(event) => beginMarketStripDrag(event, tile.id)}
                onPointerMove={(event) => updateMarketStripDrag(event, tile.id)}
                onPointerUp={(event) => finishMarketStripDrag(event, tile.id)}
                onPointerCancel={(event) => cancelMarketStripDrag(event, tile.id)}
              >
                <MarketStripTileContents tile={tile} presentation={presentation} />
              </article>
            );
          })}
      </div>
      {dragState?.isDragging && draggedTile && draggedTilePresentation ? (
        <div
          ref={dragPreviewRef}
          className={`market-strip-drag-preview market-strip-tile is-${draggedTilePresentation.tone} ${dragState.isDropping ? 'is-dropping' : ''} ${dragState.isOutside ? 'is-outside' : ''}`.trim()}
          style={{
            width: `${dragState.width}px`,
            height: `${dragState.height}px`,
            transform: `translate3d(${dragState.currentX - dragState.pointerOffsetX}px, ${dragState.currentY - dragState.pointerOffsetY}px, 0) scale(1.03)`,
          }}
          aria-hidden="true"
        >
          <MarketStripTileContents tile={draggedTile} presentation={draggedTilePresentation} />
        </div>
      ) : null}
    </section>
  );
}

function formatMarketNewsTime(value) {
  if (!value) return '';
  const publishedAt = new Date(value);
  const publishedTime = publishedAt.getTime();
  if (!Number.isFinite(publishedTime)) return '';

  const diffMs = Math.max(0, getAppNowMs() - publishedTime);
  const minuteMs = 60 * 1000;
  const hourMs = 60 * minuteMs;
  const dayMs = 24 * hourMs;

  if (diffMs < minuteMs) return 'Just now';
  if (diffMs < hourMs) {
    const minutes = Math.max(1, Math.round(diffMs / minuteMs));
    return `${minutes} min${minutes === 1 ? '' : 's'} ago`;
  }
  if (diffMs < dayMs) {
    const hours = Math.max(1, Math.round(diffMs / hourMs));
    return `${hours} hr${hours === 1 ? '' : 's'} ago`;
  }

  return publishedAt.toLocaleDateString('en-CA', {
    month: 'short',
    day: 'numeric',
  });
}

function MarketNewsPanel({ payload }) {
  const articles = Array.isArray(payload?.articles)
    ? payload.articles.slice(0, MARKET_NEWS_MAX_ARTICLES)
    : [];
  const isLoading = payload?.status === 'loading';
  const isError = payload?.status === 'error';

  return (
    <section className="panel-shell chart-card market-news-card" aria-label="Market and economic updates">
      <div className="market-news-header">
        <div className="market-news-heading">
          <h2>Market & Economic Updates</h2>
          <span className="market-news-source-notice">
            Official public sources: Bank of Canada, Federal Reserve, BLS, BEA, EIA, SEC.
          </span>
        </div>
      </div>
      {isLoading ? (
        <div className="market-news-empty">Loading market updates...</div>
      ) : articles.length > 0 ? (
        <div className="market-news-list">
          {articles.map((article) => {
            const publishedCopy = formatMarketNewsTime(article.published_at);
            return (
              <a
                key={article.id || article.url}
                className="market-news-item"
                href={article.url}
                target="_blank"
                rel="noreferrer"
              >
                <span className="market-news-item-copy">
                  <span className="market-news-title">{article.title}</span>
                  <span className="market-news-meta">
                    <span>{article.source || 'Public source'}</span>
                    {publishedCopy ? <span>{publishedCopy}</span> : null}
                  </span>
                </span>
              </a>
            );
          })}
        </div>
      ) : (
        <div className="market-news-empty">
          {isError ? 'Market updates are unavailable.' : 'No market updates available.'}
        </div>
      )}
    </section>
  );
}

// Headline figure + an optional trailing "change" cluster on one line, hugging left.
// Both the figure and the change amount collapse to their compact forms (with the full
// value on hover) as the available width shrinks — narrower card, zoom, resolution.
//
// Why this isn't two <FitMoney>s: FitMoney decides compaction from its OWN box width vs
// its content. To hug, both boxes would have to be content-sized, and a content-sized
// FitMoney can compact but can never see that space reopened (it shrank to its compact
// content) — so it sticks compact. Here one always-full-width box tracks the AVAILABLE
// width while a hidden full-width clone measures the natural combined width; when that
// clone overflows the box we compact both, and we expand back the moment it fits again.
// Thin dashboard wrapper over the reusable FitMetricValue: the value and the change fold
// independently, each with its OWN tooltip (value → full figure; change → full amount + %), shown
// only on the element that shrank. Styling stays in Dashboard.css via the passed class names.
function OverviewMetricValue({ numberFull, numberCompact, change }) {
  return (
    <FitMetricValue
      valueFull={numberFull}
      valueCompact={numberCompact}
      change={change ? {
        amountFull: change.amountFull,
        amountCompact: change.amountCompact,
        pctFull: change.pctFull,
        pctCompact: change.pctCompact,
        suffix: change.suffix,
        tooltip: change.tooltip,
        className: `overview-inline-change is-${change.tone}`.trim(),
      } : null}
      classes={{ box: 'overview-metric-value-box', line: 'overview-metric-value-line', value: 'overview-metric-number' }}
    />
  );
}

function OverviewMetricCard({ label, value, tone = 'neutral', metaTone = tone, meta, icon: Icon, action, className = '' }) {
  return (
    <section className={`panel-shell overview-metric-card is-${tone} ${className}`.trim()} aria-label={label}>
      <div className="overview-metric-card-copy">
        <div className="overview-metric-label-row">
          <span className="overview-metric-label">{label}</span>
          {action}
          {Icon ? (
            <span className="overview-metric-icon" aria-hidden="true">
              <Icon />
            </span>
          ) : null}
        </div>
        <div className="overview-metric-value">{value}</div>
        {meta ? <div className={`overview-metric-meta is-${metaTone}`.trim()}>{meta}</div> : null}
      </div>
    </section>
  );
}

function RecentTransactionsPanel({ transactions, status = 'idle', balancesHidden = false }) {
  return (
    <section className="panel-shell chart-card recent-transactions-card" aria-label="Recent transactions">
      <div className="recent-transactions-header">
        <div className="recent-transactions-heading">
          <h2>Recent Transactions</h2>
        </div>
        <Link className="dashboard-panel-link recent-transactions-view-all" to="/transactions">
          <span>Go to Transactions</span>
          <span aria-hidden="true">→</span>
        </Link>
      </div>

      <RecentTransactionsTable
        transactions={transactions}
        status={status}
        balancesHidden={balancesHidden}
      />
    </section>
  );
}

function InstitutionStatusPanel({
  institutions,
  accounts,
  syncActivity,
  transactionImportStatus,
  activeSyncBatches = [],
  autoSyncStates = null,
  autoSyncInProgress = false,
  balancesHidden = false,
  currency = 'CAD',
}) {
  const [sortConfig, setSortConfig] = usePersistentSortConfig(
    INSTITUTION_STATUS_SORT_STORAGE_KEY,
    null,
    INSTITUTION_STATUS_SORT_DEFAULT_DIRECTIONS
  );
  const handleSort = useCallback((key) => {
    setSortConfig((currentSort) => getNextSortConfig(currentSort, key, INSTITUTION_STATUS_SORT_DEFAULT_DIRECTIONS));
  }, [setSortConfig]);

  const rows = useMemo(() => {
    const activeBatchStates = getActiveSyncBatchConnectionStates(activeSyncBatches);
    return (institutions || []).map((institution) => {
      const institutionAccounts = (accounts || []).filter((account) => account.institution_id === institution.id);
      const netBalance = institutionAccounts.reduce((total, account) => {
        const balance = Number(account.balance);
        if (!Number.isFinite(balance)) return total;
        return total + (account.is_liability ? -Math.abs(balance) : balance);
      }, 0);
      const institutionId = Number(institution.id || 0);
      const autoSyncState = autoSyncStates?.[String(institutionId)] || null;
      const activeBatchState = activeBatchStates.get(institutionId) || null;
      const optimisticLastSynced = getMostRecentDateString(
        getMostRecentSyncedAt(activeBatchState?.accounts || []),
        autoSyncState?.institutionId === institutionId
          ? getActiveOptimisticSyncedAt(autoSyncState.optimisticLastSyncedAt)
          : null,
      );
      const lastSynced = getMostRecentDateString(getMostRecentSyncedAt(institutionAccounts), optimisticLastSynced);
      const updatedAt = parseSyncedAt(lastSynced);
      const status = resolveProviderSyncDisplay({
        institution,
        lastSynced,
        lastSyncText: timeAgo(lastSynced) || 'Never',
        batchState: activeBatchState,
        autoSyncState,
        autoSyncInProgress,
        syncActivity,
        transactionImportStatus,
      });

      return {
        id: institution.id,
        name: institution.name,
        provider: institution.provider,
        has_logo: institution.has_logo,
        netBalance,
        updatedAt: updatedAt ? updatedAt.getTime() : null,
        updated: status.tone === 'manual' ? 'N/A' : (timeAgo(lastSynced) || 'Never'),
        status,
      };
    }).sort((left, right) => compareInstitutionStatusRows(left, right, sortConfig));
  }, [accounts, activeSyncBatches, autoSyncInProgress, autoSyncStates, institutions, sortConfig, syncActivity, transactionImportStatus]);

  return (
    <section className="panel-shell chart-card institution-status-card" aria-label="Accounts overview">
      <div className="institution-status-header">
        <div className="institution-status-heading">
          <h2>Accounts Overview</h2>
        </div>
        <Link className="dashboard-panel-link" to="/accounts">
          <span>Go to Accounts</span>
          <span aria-hidden="true">→</span>
        </Link>
      </div>

      <div className="institution-status-table" role="table" aria-label="Institution sync status">
        <div className="institution-status-scroll-surface">
          <div className="institution-status-head" role="row">
            <SortableTableHeader
              label="Institution"
              sortKey="institution"
              sortConfig={sortConfig}
              onSort={() => handleSort('institution')}
              defaultDirections={INSTITUTION_STATUS_SORT_DEFAULT_DIRECTIONS}
              className="institution-status-head-cell"
              align="left"
              ariaLabelPrefix="Sort Institution Status by"
              resetAriaLabel="Reset Institution Status ordering"
            />
            <SortableTableHeader
              label="Status"
              sortKey="status"
              sortConfig={sortConfig}
              onSort={() => handleSort('status')}
              defaultDirections={INSTITUTION_STATUS_SORT_DEFAULT_DIRECTIONS}
              className="institution-status-head-cell"
              align="left"
              ariaLabelPrefix="Sort Institution Status by"
              resetAriaLabel="Reset Institution Status ordering"
            />
            <SortableTableHeader
              label="Current Balance"
              sortKey="balance"
              sortConfig={sortConfig}
              onSort={() => handleSort('balance')}
              defaultDirections={INSTITUTION_STATUS_SORT_DEFAULT_DIRECTIONS}
              className="institution-status-head-cell"
              align="center"
              ariaLabelPrefix="Sort Institution Status by"
              resetAriaLabel="Reset Institution Status ordering"
            />
            <SortableTableHeader
              label="Updated"
              sortKey="updated"
              sortConfig={sortConfig}
              onSort={() => handleSort('updated')}
              defaultDirections={INSTITUTION_STATUS_SORT_DEFAULT_DIRECTIONS}
              className="institution-status-head-cell"
              align="center"
              ariaLabelPrefix="Sort Institution Status by"
              resetAriaLabel="Reset Institution Status ordering"
            />
          </div>
          <InstitutionStatusHorizontalScrollProxy />
          {rows.length > 0 ? (
            <div className="institution-status-rows">
              {rows.map((row) => {
                const balanceTone = row.netBalance < 0 ? 'negative' : row.netBalance > 0 ? 'positive' : 'neutral';
                return (
                  <div className="institution-status-row" role="row" key={row.id}>
                    <span className="institution-status-main">
                      <span className="institution-status-logo">
                        <InstitutionLogo name={row.name} provider={row.provider} logoUrl={row.has_logo ? `${API}/institutions/${row.id}/logo` : undefined} size={28} boxed />
                      </span>
                      <span className="institution-status-name">{row.name}</span>
                    </span>
                    <ProviderSyncStatus
                      model={row.status}
                      institutionName={row.name}
                      showIcon={false}
                      textClassName="institution-status-badge"
                    />
                    <span className={`institution-status-balance is-${balanceTone}`.trim()}>
                      <FitMoney full={balancesHidden ? '******' : formatInstitutionNetBalance(row.netBalance, currency)} compact={balancesHidden ? '******' : formatCompactMoney(row.netBalance, currency)} className="institution-status-money" />
                    </span>
                    <span className="institution-status-updated">{row.updated}</span>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="institution-status-state">
              <MdAccountBalance size={22} aria-hidden="true" />
              <span>No active institutions yet.</span>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

// Compact cash-flow widget: one donut at a time (Cash Flow / Income / Expense),
// for one month at a time, both swapped with arrow steppers; the per-category
// legend sits to the right of the donut so the chart can be larger. Reuses the
// Cash Flow page's donut builder + period helpers so it stays in lockstep with
// the full page. The backend scopes /cash-flow to the user's VISIBLE (in-scope)
// accounts (Account.hidden=False / Institution.hidden=False), so the panel
// already follows the dashboard's institution/account scope — a scope Apply
// persists those flags and bumps dataRefreshId, which refetches this panel.
// Amounts come back already converted to the primary currency.
function getTourCashFlowAnchor() {
  const [year, month, day] = TOUR_DEMO_CASH_FLOW_ANCHOR_DATE.split('-').map(Number);
  return new Date(year, month - 1, day);
}

function DashboardCashFlowPanel({
  currency = 'CAD',
  balancesHidden = false,
  refreshId = 0,
  tourDemoActive = false,
  themeColors,
  chartThemeColors,
}) {
  const { mode: categoryThemeMode } = useTheme();
  const [viewIndex, setViewIndex] = useState(0);
  // Same period model as the Cash Flow page (preset + calendar anchor + custom
  // range), driven by the shared CashFlowTimelineControl.
  const [period, setPeriod] = useState(() => ({
    periodKey: CASH_FLOW_PANEL_PERIOD_KEY,
    anchor: getCashFlowInitialAnchor(CASH_FLOW_PANEL_PERIOD_KEY),
    customRange: { start: '', end: '' },
  }));
  const [payload, setPayload] = useState(null);
  const [status, setStatus] = useState('loading');
  const hasLoadedRef = useRef(false);

  useEffect(() => {
    if (!tourDemoActive) return;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (cancelled) return;
      setPeriod((current) => {
        const tourAnchor = getTourCashFlowAnchor();
        const currentAnchor = current.anchor instanceof Date ? current.anchor : new Date(current.anchor || getAppNowMs());
        if (
          current.periodKey === CASH_FLOW_PANEL_PERIOD_KEY
          && currentAnchor.getFullYear() === tourAnchor.getFullYear()
          && currentAnchor.getMonth() === tourAnchor.getMonth()
        ) {
          return current;
        }
        return {
          periodKey: CASH_FLOW_PANEL_PERIOD_KEY,
          anchor: tourAnchor,
          customRange: { start: '', end: '' },
        };
      });
    });
    return () => {
      cancelled = true;
    };
  }, [tourDemoActive]);

  const view = CASH_FLOW_PANEL_VIEWS[viewIndex];
  const range = useMemo(
    () => computeCashFlowRange(period.periodKey, period.anchor, period.customRange),
    [period],
  );
  const periodLabel = useMemo(
    () => computeCashFlowLabel(period.periodKey, period.anchor, period.customRange),
    [period],
  );

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams();
    if (range.start) params.set('start_date', range.start);
    if (range.end) params.set('end_date', range.end);
    (async () => {
      try {
        // Only flash the loading state on the very first fetch; month/refresh
        // changes swap the donut in place once the new data lands.
        if (!hasLoadedRef.current) setStatus('loading');
        const response = await fetch(`${API}/cash-flow?${params.toString()}`, { signal: controller.signal });
        setPayload(await readCashFlowResponse(response, { label: 'Dashboard cash flow' }));
        setStatus('idle');
        hasLoadedRef.current = true;
      } catch (error) {
        if (error?.name === 'AbortError') return;
        if (!hasLoadedRef.current) {
          setPayload(null);
          setStatus('error');
        }
      }
    })();
    return () => controller.abort();
  }, [range.start, range.end, refreshId, currency]);

  // The response amounts are already denominated in `period.currency`. During
  // a primary-currency switch, keep rendering an existing response in its own
  // unit until the currency-dependent fetch above replaces it. Using the new
  // toolbar currency against the old numbers would temporarily label BTC as CAD
  // (the exact $0.11 stale-payload bug this guards against).
  const payloadCurrency = getDashboardCashFlowDisplayCurrency(payload, currency);

  const { segments, centerLabel, centerValue, centerSigned, legendRows, donutTotal, hasChartData } = useMemo(() => {
    const totals = payload ? payload.totals : { income: 0, expense: 0, net: 0 };
    const placeholderColor = chartThemeColors.other;
    if (view.key === 'income') {
      const rows = payload ? payload.income_breakdown : [];
      const total = rows.reduce((sum, row) => sum + row.amount, 0);
      const hasRows = total > 0;
      return {
        segments: hasRows
          ? rows.slice(0, 10).map((row) => ({
            name: row.name,
            value: row.amount,
            color: resolveCategoryAccentColor(row, categoryThemeMode, chartThemeColors.other),
          }))
          : [{ name: 'No income', value: 1, color: placeholderColor }],
        centerLabel: 'Income',
        centerValue: total,
        centerSigned: false,
        legendRows: hasRows
          ? rows.slice(0, CASH_FLOW_PANEL_TOP_CATEGORIES).map((row) => ({ ...row, percent: row.percent_of_total }))
          : [],
        donutTotal: total,
        hasChartData: hasRows,
      };
    }
    if (view.key === 'expense') {
      const rows = rollupByParent(payload ? payload.expense_breakdown : []);
      const total = rows.reduce((sum, row) => sum + row.amount, 0);
      const hasRows = total > 0;
      return {
        segments: hasRows
          ? rows.slice(0, 10).map((row) => ({
            name: row.name,
            value: row.amount,
            color: resolveCategoryAccentColor(row, categoryThemeMode, chartThemeColors.other),
          }))
          : [{ name: 'No spending', value: 1, color: placeholderColor }],
        centerLabel: 'Expense',
        centerValue: total,
        centerSigned: false,
        legendRows: hasRows
          ? rows.slice(0, CASH_FLOW_PANEL_TOP_CATEGORIES).map((row) => ({ ...row, percent: row.percent_of_total }))
          : [],
        donutTotal: total,
        hasChartData: hasRows,
      };
    }
    const netTotal = Math.max(totals.income, 0) + Math.max(totals.expense, 0);
    const hasNetData = netTotal > 0;
    return {
      segments: hasNetData
        ? [
          { name: 'Income', value: Math.max(totals.income, 0.01), color: themeColors.positive },
          { name: 'Expense', value: Math.max(totals.expense, 0.01), color: themeColors.negative },
        ]
        : [{ name: 'No cash flow', value: 1, color: placeholderColor }],
      centerLabel: 'Net Cash Flow',
      centerValue: totals.net,
      centerSigned: true,
      legendRows: hasNetData
        ? [
          { category_id: 'cf-income', name: 'Income', color: themeColors.positive, amount: totals.income, percent: (Math.max(totals.income, 0) / netTotal) * 100, plain: true },
          { category_id: 'cf-expense', name: 'Expense', color: themeColors.negative, amount: totals.expense, percent: (Math.max(totals.expense, 0) / netTotal) * 100, plain: true },
        ]
        : [],
      donutTotal: netTotal,
      hasChartData: hasNetData,
    };
  }, [view.key, payload, themeColors, chartThemeColors, categoryThemeMode]);

  const donutOption = useMemo(
    () => buildCfDonutOption(segments, { total: donutTotal, currency: payloadCurrency, balancesHidden, chartColors: chartThemeColors, tooltipDisabled: !hasChartData }),
    [segments, donutTotal, payloadCurrency, balancesHidden, chartThemeColors, hasChartData],
  );

  const cycleView = (direction) => {
    setViewIndex((index) => (index + direction + CASH_FLOW_PANEL_VIEWS.length) % CASH_FLOW_PANEL_VIEWS.length);
  };
  // The arrow tooltips name the chart they'll switch to (Net / Income / Expense).
  const prevView = CASH_FLOW_PANEL_VIEWS[(viewIndex - 1 + CASH_FLOW_PANEL_VIEWS.length) % CASH_FLOW_PANEL_VIEWS.length];
  const nextView = CASH_FLOW_PANEL_VIEWS[(viewIndex + 1) % CASH_FLOW_PANEL_VIEWS.length];

  const isInitialLoading = status === 'loading' && !payload;
  const centerTone = centerSigned && centerValue > 0 ? 'is-positive' : centerSigned && centerValue < 0 ? 'is-negative' : '';
  const emptyMessage = view.key === 'income'
    ? `No income in ${periodLabel}.`
    : view.key === 'expense'
      ? `No spending in ${periodLabel}.`
      : `No cash flow in ${periodLabel}.`;

  return (
    <section className="panel-shell chart-card dashboard-cashflow-card" aria-label="Cash flow">
      <div className="dashboard-cashflow-header-scroll">
        <div className="dashboard-cashflow-header">
          <h2>Cash Flow</h2>
          <CashFlowTimelineControl
            periodKey={period.periodKey}
            anchor={period.anchor}
            customRange={period.customRange}
            onChange={setPeriod}
          />
          <Link className="dashboard-panel-link" to="/cash-flow" state={{ cashFlowPeriod: period }}>
            <span>Go to Cash Flow</span>
            <span aria-hidden="true">→</span>
          </Link>
        </div>
      </div>

      {status === 'error' ? (
        <div className="dashboard-cashflow-state is-error">Cash flow is unavailable.</div>
      ) : isInitialLoading ? (
        <div className="dashboard-cashflow-state">Loading cash flow…</div>
      ) : (
        <div className="dashboard-cashflow-body">
          <div className="dashboard-cashflow-chart">
            <div className="dashboard-cashflow-donut">
              <StablePieTooltipEChart option={donutOption} height={CASH_FLOW_DONUT_HEIGHT} />
              <div className="dashboard-cashflow-donut-center">
                <div className="dashboard-cashflow-donut-label">{centerLabel}</div>
                <div className={`dashboard-cashflow-donut-value ${centerTone}`.trim()}>
                  <Money amount={centerValue} currency={payloadCurrency} hidden={balancesHidden} signed={centerSigned} />
                </div>
              </div>
            </div>
            <div className="chart-view-stepper" role="group" aria-label="Chart view">
              <button
                type="button"
                className="chart-view-stepper-btn chart-view-stepper-prev-btn app-control-root"
                onClick={() => cycleView(-1)}
                aria-label={`Show ${prevView.label}`}
                data-tooltip={prevView.label}
              >
                <span className="app-control-icon" aria-hidden="true">
                  <TriangleIcon direction="left" />
                </span>
              </button>
              <span className="chart-view-stepper-label">{view.label}</span>
              <button
                type="button"
                className="chart-view-stepper-btn chart-view-stepper-next-btn app-control-root"
                onClick={() => cycleView(1)}
                aria-label={`Show ${nextView.label}`}
                data-tooltip={nextView.label}
              >
                <span className="app-control-icon" aria-hidden="true">
                  <TriangleIcon direction="right" />
                </span>
              </button>
            </div>
          </div>

          <div className={`dashboard-cashflow-legend ${view.key === 'cashflow' ? 'is-net' : ''} ${hasChartData ? '' : 'is-empty'}`.trim()}>
            {hasChartData ? legendRows.map((row) => (
              <div className="dashboard-cashflow-legend-row" key={row.category_id}>
                <div className="dashboard-cashflow-legend-label">
                  {row.plain ? (
                    <span className="dashboard-cashflow-legend-chip">
                      <span className="dashboard-cashflow-legend-dot" style={{ background: row.color }} aria-hidden="true" />
                      <span className="dashboard-cashflow-legend-name">{row.name}</span>
                    </span>
                  ) : (
                    <CategoryPill
                      category={{
                        id: row.category_id,
                        name: row.name,
                        icon: row.icon,
                        icon_set: row.icon_set,
                        color_dark: row.color_dark,
                        color_light: row.color_light,
                      }}
                      size="sm"
                    />
                  )}
                </div>
                <div className="dashboard-cashflow-legend-track">
                  <div
                    className="dashboard-cashflow-legend-fill"
                    style={{
                      width: `${Math.min(100, Math.max(0, row.percent || 0))}%`,
                      background: row.plain
                        ? row.color
                        : resolveCategoryAccentColor(row, categoryThemeMode, chartThemeColors.other),
                    }}
                  />
                </div>
                <div className="dashboard-cashflow-legend-amount">
                  <Money amount={row.amount} currency={payloadCurrency} hidden={balancesHidden} className="dashboard-cashflow-legend-money" />
                  <span className="dashboard-cashflow-legend-percent">{(row.percent || 0).toFixed(1)}%</span>
                </div>
              </div>
            )) : (
              <div className="dashboard-cashflow-legend-empty">{emptyMessage}</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function Dashboard({
  data,
  allScopeInstitutions = [],
  fetchAllScopeInstitutions,
  onDataChange,
  dataRefreshId = 0,
  timeframe = DEFAULT_PORTFOLIO_TIMEFRAME,
  customDateRange = { start: '', end: '' },
  syncActivity = null,
  activeSyncBatches = [],
  autoSyncStates = null,
  autoSyncInProgress = false,
  tourDemoActive = false,
}) {
  const [balancesHidden] = useBalancesHidden();
  const { colors: themeColors, chartColors: chartThemeColors } = useTheme();
  const [chartColors, setChartColorsRaw] = useState(loadPortfolioChartColors);
  // Core data is owned by App-level state; render straight from the `data` prop.
  const { accounts = [], networth = null } = data;
  const [allocationTab, setAllocationTab] = useState('assets');
  const [allocationLiabView, setAllocationLiabView] = useState('institution');
  const [balanceHistory, setBalanceHistory] = useState({});
  const [marketStrip, setMarketStrip] = useState(() => loadCachedMarketStripPayload() || { status: 'loading', tiles: [] });
  const [marketNews, setMarketNews] = useState(() => loadCachedMarketNewsPayload() || { status: 'loading', articles: [] });
  const [recentTransactions, setRecentTransactions] = useState([]);
  const [recentTransactionsStatus, setRecentTransactionsStatus] = useState('loading');
  const [showVisDropdown, setShowVisDropdown] = useState(false);
  const [scopeSaveError, setScopeSaveError] = useState('');
  const [scopeSaving, setScopeSaving] = useState(false);
  // Transient draft for the scope popover's optimistic toggles; null when closed
  // (then the central allScopeInstitutions prop is read — the single source of truth).
  const [scopeDraft, setScopeDraft] = useState(null);
  const scopeInstitutions = scopeDraft ?? allScopeInstitutions;
  const dashboardToolbarScopeSlot = usePortalElementById('dashboard-toolbar-scope-slot');
  const dashboardToolbarCustomizeSlot = usePortalElementById('dashboard-toolbar-customize-slot');
  const { primaryCurrency, fxRates } = useCurrency();
  const [fxHistory, setFxHistory] = useState({});
  const [dashboardLayout, setDashboardLayoutRaw] = useState(loadDashboardLayout);
  const [isCustomizeOpen, setIsCustomizeOpen] = useState(false);
  const [isArrangeMode, setIsArrangeMode] = useState(false);
  const [draggedDashboardWidgetId, setDraggedDashboardWidgetId] = useState(null);
  const [dashboardDropTargetId, setDashboardDropTargetId] = useState(null);
  const [draggedDashboardBandId, setDraggedDashboardBandId] = useState(null);
  const [dashboardBandDropTarget, setDashboardBandDropTarget] = useState(null);
  const dashboardScopePopoverRef = useRef(null);
  const dashboardCustomizeTriggerRef = useRef(null);
  const dashboardCustomizeTrayRef = useRef(null);

  const setChartColors = useCallback((updater) => {
    setChartColorsRaw((previous) => {
      const next = typeof updater === 'function' ? updater(previous) : updater;
      if (next !== previous) {
        persistPortfolioChartColors(next);
      }
      return next;
    });
  }, []);

  const setDashboardLayout = useCallback((updater) => {
    setDashboardLayoutRaw((previous) => {
      const nextLayout = normalizeDashboardLayout(typeof updater === 'function' ? updater(previous) : updater);
      persistDashboardLayout(nextLayout);
      return nextLayout;
    });
  }, []);

  const closeCustomizeDrawer = useCallback(() => {
    setIsCustomizeOpen(false);
    setIsArrangeMode(false);
    setDraggedDashboardWidgetId(null);
    setDashboardDropTargetId(null);
  }, []);

  const toggleCustomizeDrawer = useCallback(() => {
    setIsCustomizeOpen((previous) => {
      const next = !previous;
      if (!next) {
        setIsArrangeMode(false);
        setDraggedDashboardWidgetId(null);
        setDashboardDropTargetId(null);
      }
      return next;
    });
  }, []);

  const handleToggleDashboardWidget = useCallback((widgetId) => {
    setDashboardLayout((previous) => ({
      ...previous,
      visible: {
        ...previous.visible,
        [widgetId]: previous.visible[widgetId] === false,
      },
    }));
  }, [setDashboardLayout]);

  const handleResetDashboardLayout = useCallback(() => {
    setDashboardLayout({
      order: DASHBOARD_WIDGET_IDS,
      visible: DASHBOARD_DEFAULT_VISIBILITY,
    });
  }, [setDashboardLayout]);

  const handleMoveDashboardWidget = useCallback((widgetId, offset) => {
    setDashboardLayout((previous) => ({
      ...previous,
      order: moveDashboardWidgetWithinGroup(previous, widgetId, offset),
    }));
  }, [setDashboardLayout]);

  const handleMoveDashboardBand = useCallback((bandId, offset) => {
    setDashboardLayout((previous) => {
      const normalized = normalizeDashboardLayout(previous);
      const currentIndex = normalized.bandOrder.indexOf(bandId);
      const nextIndex = currentIndex + offset;
      if (currentIndex < 0 || nextIndex < 0 || nextIndex >= normalized.bandOrder.length) {
        return normalized;
      }
      const nextBandOrder = [...normalized.bandOrder];
      const [moved] = nextBandOrder.splice(currentIndex, 1);
      nextBandOrder.splice(nextIndex, 0, moved);
      return normalizeDashboardLayout({ ...normalized, bandOrder: nextBandOrder });
    });
  }, [setDashboardLayout]);

  const handlePlaceDashboardWidget = useCallback((activeId, layoutGroup, targetId = null, position = 'after') => {
    setDashboardLayout((previous) => {
      const normalized = normalizeDashboardLayout(previous);
      const activeGroup = getDashboardWidgetLayoutGroup(activeId, normalized.columns);
      const targetGroup = targetId
        ? getDashboardWidgetLayoutGroup(targetId, normalized.columns)
        : layoutGroup;
      const canMoveAcrossMainColumns = isDashboardMainGroup(activeGroup) && isDashboardMainGroup(targetGroup);
      if (activeGroup !== targetGroup && !canMoveAcrossMainColumns) {
        return normalized;
      }

      const nextColumns = { ...normalized.columns };
      if (canMoveAcrossMainColumns) {
        nextColumns[activeId] = getDashboardColumnFromGroup(targetGroup);
      }

      let nextOrder = normalized.order;
      if (targetId && activeId !== targetId) {
        nextOrder = moveDashboardWidgetRelative(normalized.order, activeId, targetId, position);
      } else if (!targetId && targetGroup) {
        const targetGroupIds = normalized.order.filter(
          (id) => id !== activeId && getDashboardWidgetLayoutGroup(id, nextColumns) === targetGroup
        );
        if (targetGroupIds.length > 0) {
          nextOrder = moveDashboardWidgetRelative(normalized.order, activeId, targetGroupIds[targetGroupIds.length - 1], 'after');
        }
      }

      return normalizeDashboardLayout({ ...normalized, order: nextOrder, columns: nextColumns });
    });
  }, [setDashboardLayout]);

  useEffect(() => {
    fetch(`${API}/accounts/balance-history`)
      .then((r) => r.json())
      .then((d) => setBalanceHistory(d))
      .catch(() => {});
  }, [data.accounts, dataRefreshId]);

  const hasLoadedRecentTransactionsRef = useRef(false);
  const loadRecentTransactions = useCallback(async ({ signal } = {}) => {
    const params = new URLSearchParams({
      limit: String(RECENT_TRANSACTIONS_LIMIT),
      offset: '0',
    });

    try {
      // Only show the loading state on the first fetch; later refreshes (e.g. when an
      // autosync settles) update the table in place without flashing a spinner.
      if (!hasLoadedRecentTransactionsRef.current) {
        setRecentTransactionsStatus('loading');
      }
      const response = await fetch(`${API}/transactions?${params.toString()}`, { signal });
      const payload = await readTransactionCollectionResponse(response, {
        label: 'Recent transactions',
      });
      setRecentTransactions(payload.transactions.slice(0, RECENT_TRANSACTIONS_LIMIT));
      setRecentTransactionsStatus('idle');
      hasLoadedRecentTransactionsRef.current = true;
    } catch (error) {
      if (error?.name === 'AbortError') {
        return;
      }
      // Keep the already-rendered rows on a transient refresh failure.
      if (!hasLoadedRecentTransactionsRef.current) {
        setRecentTransactions([]);
        setRecentTransactionsStatus('error');
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) {
        loadRecentTransactions({ signal: controller.signal });
      }
    });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [dataRefreshId, loadRecentTransactions]);

  // Refresh recent transactions whenever a provider's sync activity settles, mirroring
  // how the Institution Status panel reacts to the same live syncActivity stream.
  const activeSyncActivityProvidersRef = useRef(new Set());
  useEffect(() => {
    const { providers: nextProviders } = getActiveInstitutionTargets(syncActivity);
    const previousProviders = activeSyncActivityProvidersRef.current;
    const settled = [...previousProviders].some((provider) => !nextProviders.has(provider));
    activeSyncActivityProvidersRef.current = nextProviders;
    if (settled) {
      loadRecentTransactions();
    }
  }, [syncActivity, loadRecentTransactions]);

  const loadMarketStrip = useCallback(async ({ cancelled = () => false } = {}) => {
    try {
      const response = await fetch(`${API}/market-data/strip`);
      const payload = await response.json();
      if (!response.ok || payload.status !== 'ok') {
        throw new Error(payload.message || 'Market strip request failed');
      }
      if (!cancelled()) {
        setMarketStrip(payload);
        persistMarketStripPayload(payload);
      }
    } catch (_) {
      if (!cancelled()) {
        setMarketStrip((previous) => ({ ...previous, status: 'error' }));
      }
    }
  }, []);

  const handleMarketStripWatchlistChange = useCallback(async (items) => {
    try {
      const response = await fetch(`${API}/market-data/strip/watchlist`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items }),
      });
      const payload = await response.json();
      if (response.ok && payload.status === 'ok') {
        setMarketStrip((previous) => ({ ...previous, watchlist: payload.watchlist || items }));
        await loadMarketStrip();
      }
    } catch (_) {
      setMarketStrip((previous) => ({ ...previous, status: 'error' }));
    }
  }, [loadMarketStrip]);

  useEffect(() => {
    let isCancelled = false;
    const isEffectCancelled = () => isCancelled;
    Promise.resolve().then(() => {
      if (!isCancelled) {
        loadMarketStrip({ cancelled: isEffectCancelled });
      }
    });
    const intervalId = window.setInterval(() => loadMarketStrip({ cancelled: isEffectCancelled }), MARKET_STRIP_REFRESH_MS);
    return () => {
      isCancelled = true;
      window.clearInterval(intervalId);
    };
  }, [loadMarketStrip]);

  const loadMarketNews = useCallback(async ({ cancelled = () => false } = {}) => {
    try {
      const response = await fetch(`${API}/market-data/news`);
      const payload = await response.json();
      if (!cancelled() && response.ok && payload.status === 'ok') {
        const articleCount = Array.isArray(payload.articles) ? payload.articles.length : 0;
        if (articleCount > 0) {
          setMarketNews(payload);
          persistMarketNewsPayload(payload);
        }
        return articleCount;
      } else if (!cancelled()) {
        setMarketNews((previous) => ({ ...previous, status: 'error' }));
      }
    } catch (_) {
      if (!cancelled()) {
        setMarketNews((previous) => ({ ...previous, status: 'error' }));
      }
    }
    return 0;
  }, []);

  useEffect(() => {
    let isCancelled = false;
    const isEffectCancelled = () => isCancelled;
    let emptyRetryTimeoutId = null;
    const loadAndRetryEmptyNews = async () => {
      const articleCount = await loadMarketNews({ cancelled: isEffectCancelled });
      if (!isCancelled && articleCount === 0) {
        emptyRetryTimeoutId = window.setTimeout(loadAndRetryEmptyNews, MARKET_NEWS_EMPTY_RETRY_MS);
      }
    };
    loadAndRetryEmptyNews();
    const intervalId = window.setInterval(() => loadMarketNews({ cancelled: isEffectCancelled }), MARKET_NEWS_REFRESH_MS);
    return () => {
      isCancelled = true;
      if (emptyRetryTimeoutId) {
        window.clearTimeout(emptyRetryTimeoutId);
      }
      window.clearInterval(intervalId);
    };
  }, [loadMarketNews]);

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
      if (onDataChange) Promise.resolve(onDataChange()).catch(() => {});
      Promise.resolve(loadRecentTransactions()).catch(() => {});
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

  useEffect(() => {
    fetch(`${API}/fx-rates/history`)
      .then((r) => r.json())
      .then((d) => { if (d.rates) setFxHistory(d.rates); })
      .catch(() => {});
  }, []);

  const convertToPrimary = useCallback((amount, fromCurrency) => {
    const numeric = Number(amount);
    if (!Number.isFinite(numeric)) return 0;
    const from = String(fromCurrency || primaryCurrency).trim().toUpperCase();
    if (!from || from === primaryCurrency) return numeric;
    const rate = fxRates[from];
    if (!rate || rate === 0) return numeric;
    return numeric / rate;
  }, [primaryCurrency, fxRates]);

  // Sorted historical rate map for nearest-prior lookup by date.
  const fxHistorySorted = useMemo(() => buildFxHistoryIndex(fxHistory), [fxHistory]);

  // Convert a past balance into the primary currency at its own date (used for
  // the net-worth history series); falls back to the current rate.
  const convertToPrimaryAtDate = useCallback((value, fromCurrency, date) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    const from = String(fromCurrency || primaryCurrency).trim().toUpperCase();
    if (!from || from === primaryCurrency) return numeric;
    const cadFrom = cadPerUnitAtDate(fxHistorySorted, from, date, fxRates);
    const cadTo = cadPerUnitAtDate(fxHistorySorted, primaryCurrency, date, fxRates);
    if (!cadTo) return numeric;
    return (numeric * cadFrom) / cadTo;
  }, [fxHistorySorted, fxRates, primaryCurrency]);

  useEffect(() => {
    if (!isCustomizeOpen) {
      return undefined;
    }

    const handlePointerDown = (event) => {
      const target = event.target;
      if (
        dashboardCustomizeTrayRef.current?.contains(target)
        || dashboardCustomizeTriggerRef.current?.contains(target)
        || target?.closest?.(APP_NON_DISMISS_INTERACTION_SELECTOR)
      ) {
        return;
      }

      closeCustomizeDrawer();
    };

    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        closeCustomizeDrawer();
      }
    };

    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [closeCustomizeDrawer, isCustomizeOpen]);

  const closeScopeDropdownWithoutSave = useCallback(() => {
    setScopeDraft(null);
    setShowVisDropdown(false);
  }, []);

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
  const hasHiddenInstitutions = scopeInstitutions.some((inst) => inst.hidden || inst.accounts.some((account) => account.hidden));
  const scopeTotalAccountCount = scopeInstitutions.reduce((total, inst) => total + inst.accounts.length, 0);
  const scopeSummary = formatScopeSelectionSummary({
    totalInstitutions: scopeInstitutions.length,
    selectedInstitutions: selectedScopeInstitutionCount,
    selectedAccounts: selectedScopeAccountIds.length,
  });
  const allScopeSourcesSelected = scopeTotalAccountCount > 0 && selectedScopeAccountIds.length === scopeTotalAccountCount;

  const handleToggleScopeDropdown = () => {
    if (showVisDropdown) {
      closeScopeDropdownWithoutSave();
      return;
    }

    setScopeDraft(cloneScopeInstitutions(allScopeInstitutions));
    setShowVisDropdown(true);
  };

  useDismissibleLayer({
    open: showVisDropdown,
    ref: dashboardScopePopoverRef,
    onDismiss: closeScopeDropdownWithoutSave,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

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

  const handleDashboardWidgetDragStart = useCallback((event, widgetId) => {
    if (!isArrangeMode) {
      event.preventDefault();
      return;
    }

    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', widgetId);
    setDraggedDashboardWidgetId(widgetId);
  }, [isArrangeMode]);

  const handleDashboardWidgetDragOver = useCallback((event, widgetId) => {
    const activeId = draggedDashboardWidgetId || event.dataTransfer.getData('text/plain');
    if (!isArrangeMode || !activeId || activeId === widgetId) {
      return;
    }
    const activeGroup = getDashboardWidgetLayoutGroup(activeId, dashboardLayout.columns);
    const targetGroup = getDashboardWidgetLayoutGroup(widgetId, dashboardLayout.columns);
    if (activeGroup !== targetGroup && !(isDashboardMainGroup(activeGroup) && isDashboardMainGroup(targetGroup))) {
      return;
    }

    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    setDashboardDropTargetId(widgetId);
  }, [dashboardLayout.columns, draggedDashboardWidgetId, isArrangeMode]);

  const handleDashboardWidgetDrop = useCallback((event, widgetId) => {
    if (!isArrangeMode) {
      return;
    }

    event.preventDefault();
    const activeId = event.dataTransfer.getData('text/plain') || draggedDashboardWidgetId;
    if (activeId && activeId !== widgetId) {
      handlePlaceDashboardWidget(
        activeId,
        getDashboardWidgetLayoutGroup(widgetId, dashboardLayout.columns),
        widgetId,
        getDashboardWidgetDropPosition(event),
      );
    }
    setDraggedDashboardWidgetId(null);
    setDashboardDropTargetId(null);
  }, [dashboardLayout.columns, draggedDashboardWidgetId, handlePlaceDashboardWidget, isArrangeMode]);

  const handleDashboardWidgetContainerDragOver = useCallback((event, layoutGroup = null) => {
    const activeId = draggedDashboardWidgetId || event.dataTransfer.getData('text/plain');
    if (!isArrangeMode || !activeId || getDashboardWidgetElementFromEvent(event, event.currentTarget)) {
      return;
    }
    const activeGroup = getDashboardWidgetLayoutGroup(activeId, dashboardLayout.columns);
    const canMoveToGroup = layoutGroup && (
      activeGroup === layoutGroup || (isDashboardMainGroup(activeGroup) && isDashboardMainGroup(layoutGroup))
    );
    if (layoutGroup && !canMoveToGroup) {
      return;
    }

    const dropIntent = getDashboardWidgetContainerDropIntent(event.currentTarget, event.clientY);
    if (dropIntent?.targetId === activeId) {
      return;
    }

    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    setDashboardDropTargetId(dropIntent?.targetId || null);
  }, [dashboardLayout.columns, draggedDashboardWidgetId, isArrangeMode]);

  const handleDashboardWidgetContainerDrop = useCallback((event, layoutGroup = null) => {
    if (!isArrangeMode || getDashboardWidgetElementFromEvent(event, event.currentTarget)) {
      return;
    }

    const dropIntent = getDashboardWidgetContainerDropIntent(event.currentTarget, event.clientY);
    const activeId = event.dataTransfer.getData('text/plain') || draggedDashboardWidgetId;
    if (
      activeId
      && layoutGroup
      && (!dropIntent || activeId !== dropIntent.targetId)
    ) {
      event.preventDefault();
      handlePlaceDashboardWidget(activeId, layoutGroup, dropIntent?.targetId || null, dropIntent?.position || 'after');
    }
    setDraggedDashboardWidgetId(null);
    setDashboardDropTargetId(null);
  }, [draggedDashboardWidgetId, handlePlaceDashboardWidget, isArrangeMode]);

  const clearDashboardWidgetDrag = useCallback(() => {
    setDraggedDashboardWidgetId(null);
    setDashboardDropTargetId(null);
  }, []);

  const clearDashboardBandDrag = useCallback(() => {
    setDraggedDashboardBandId(null);
    setDashboardBandDropTarget(null);
  }, []);

  const handleDashboardBandDragStart = useCallback((event, bandId) => {
    if (!isArrangeMode) {
      event.preventDefault();
      return;
    }
    event.stopPropagation();
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('application/x-dashboard-band', bandId);
    setDraggedDashboardBandId(bandId);
  }, [isArrangeMode]);

  const handleDashboardBandDragOver = useCallback((event, bandId) => {
    if (!isArrangeMode) return;
    const activeBandId = draggedDashboardBandId || event.dataTransfer.getData('application/x-dashboard-band');
    if (!activeBandId || activeBandId === bandId) return;
    if (!DASHBOARD_TOP_BAND_IDS.includes(bandId)) return;

    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'move';
    setDashboardBandDropTarget({ bandId });
  }, [draggedDashboardBandId, isArrangeMode]);

  const handleDashboardBandDrop = useCallback((event, bandId) => {
    if (!isArrangeMode) return;
    const activeBandId = event.dataTransfer.getData('application/x-dashboard-band') || draggedDashboardBandId;
    if (!activeBandId || activeBandId === bandId) {
      clearDashboardBandDrag();
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    setDashboardLayout((previous) => {
      const normalized = normalizeDashboardLayout(previous);
      const activeIndex = normalized.bandOrder.indexOf(activeBandId);
      const targetIndex = normalized.bandOrder.indexOf(bandId);
      if (activeIndex < 0 || targetIndex < 0) return normalized;
      const nextBandOrder = [...normalized.bandOrder];
      [nextBandOrder[activeIndex], nextBandOrder[targetIndex]] = [nextBandOrder[targetIndex], nextBandOrder[activeIndex]];
      return normalizeDashboardLayout({ ...normalized, bandOrder: nextBandOrder });
    });
    clearDashboardBandDrag();
  }, [clearDashboardBandDrag, draggedDashboardBandId, isArrangeMode, setDashboardLayout]);

  const accountCurrencyById = useMemo(() => {
    const map = new Map();
    (accounts || []).forEach((account) => {
      if (account && account.id != null) {
        map.set(String(account.id), account.currency || primaryCurrency);
      }
    });
    return map;
  }, [accounts, primaryCurrency]);

  const accountIsLiabilityById = useMemo(() => {
    const map = new Map();
    (accounts || []).forEach((account) => {
      if (account && account.id != null) {
        map.set(String(account.id), Boolean(account.is_liability));
      }
    });
    return map;
  }, [accounts]);

  const convertedAccounts = useMemo(() => (accounts || []).map((account) => ({
    ...account,
    balance: convertToPrimary(account.balance, account.currency),
  })), [accounts, convertToPrimary]);

  const convertedBalanceHistory = useMemo(() => {
    const result = {};
    Object.entries(balanceHistory || {}).forEach(([accountId, entries]) => {
      const fromCurrency = accountCurrencyById.get(String(accountId));
      const converted = {};
      Object.entries(entries || {}).forEach(([date, value]) => {
        converted[date] = convertToPrimaryAtDate(value, fromCurrency, date);
      });
      result[accountId] = converted;
    });
    return result;
  }, [accountCurrencyById, balanceHistory, convertToPrimaryAtDate]);

  const currentTotals = useMemo(() => {
    let assets = 0;
    let liabilities = 0;
    convertedAccounts.forEach((account) => {
      const balance = Number(account.balance) || 0;
      if (account.is_liability) {
        liabilities += balance;
      } else {
        assets += balance;
      }
    });
    return {
      total_assets: assets,
      total_liabilities: liabilities,
      net_worth: assets - Math.abs(liabilities),
    };
  }, [convertedAccounts]);

  const assetInstitutionChartColors = chartColors.assetInstitutions || EMPTY_PORTFOLIO_CHART_COLORS;
  const liabilityInstitutionChartColors = chartColors.liabilityInstitutions || EMPTY_PORTFOLIO_CHART_COLORS;
  const liabilityTypeChartColors = chartColors.liabilityTypes || EMPTY_PORTFOLIO_CHART_COLORS;
  const allocationColorData = useMemo(() => {
    const assetMap = new Map();
    const liabilityInstitutionMap = new Map();
    const liabilityTypeMap = new Map();

    convertedAccounts.forEach((account) => {
      const balance = Number(account.balance || 0);
      const institutionKey = account.institution_id !== undefined && account.institution_id !== null
        ? String(account.institution_id)
        : account.institution;

      if (!account.is_liability && balance > 0 && institutionKey) {
        const existing = assetMap.get(institutionKey) || {
          key: institutionKey,
          label: account.institution || 'Unknown',
          value: 0,
        };
        existing.value += balance;
        assetMap.set(institutionKey, existing);
      }

      if (account.is_liability && balance) {
        const liabilityValue = Math.abs(balance);
        if (institutionKey) {
          const existing = liabilityInstitutionMap.get(institutionKey) || {
            key: institutionKey,
            label: account.institution || 'Unknown',
            value: 0,
          };
          existing.value += liabilityValue;
          liabilityInstitutionMap.set(institutionKey, existing);
        }

        const typeKey = account.account_type || 'other';
        const existingType = liabilityTypeMap.get(typeKey) || {
          key: typeKey,
          label: formatPortfolioAccountTypeLabel(typeKey),
          value: 0,
        };
        existingType.value += liabilityValue;
        liabilityTypeMap.set(typeKey, existingType);
      }
    });

    const assetItems = Array.from(assetMap.values())
      .filter((item) => item.value > 0)
      .sort((left, right) => right.value - left.value);

    return {
      assetItems,
      liabilityInstitutionItems: Array.from(liabilityInstitutionMap.values())
        .filter((item) => item.value > 0)
        .sort((left, right) => right.value - left.value),
      liabilityTypeItems: Array.from(liabilityTypeMap.values())
        .filter((item) => item.value > 0)
        .sort((left, right) => right.value - left.value),
    };
  }, [convertedAccounts]);

  const liabilityPrimaryInstitutionKey = allocationColorData.liabilityInstitutionItems[0]?.key || null;

  const assetInstitutionDefaultColors = useMemo(() => {
    const defaults = {};
    let paletteIndex = 0;

    allocationColorData.assetItems.forEach((item) => {
      if (liabilityPrimaryInstitutionKey && item.key === liabilityPrimaryInstitutionKey) {
        defaults[item.key] = chartThemeColors.liabilities[0];
      } else {
        defaults[item.key] = getPortfolioPaletteColor(chartThemeColors.assets, paletteIndex);
        paletteIndex += 1;
      }
    });

    if (allocationColorData.assetItems.length > DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT) {
      defaults[OTHER_INSTITUTIONS_COLOR_KEY] = chartThemeColors.other;
    }

    return defaults;
  }, [allocationColorData.assetItems, liabilityPrimaryInstitutionKey, chartThemeColors]);

  const liabilityInstitutionDefaultColors = useMemo(() => {
    const defaults = {};
    allocationColorData.liabilityInstitutionItems.forEach((item, index) => {
      defaults[item.key] = getPortfolioPaletteColor(chartThemeColors.liabilities, index);
    });
    if (allocationColorData.liabilityInstitutionItems.length > DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT) {
      defaults[OTHER_INSTITUTIONS_COLOR_KEY] = chartThemeColors.other;
    }
    return defaults;
  }, [allocationColorData.liabilityInstitutionItems, chartThemeColors]);

  const liabilityTypeDefaultColors = useMemo(() => {
    const defaults = {};
    allocationColorData.liabilityTypeItems.forEach((item, index) => {
      defaults[item.key] = getPortfolioPaletteColor(chartThemeColors.liabilityTypes, index);
    });
    return defaults;
  }, [allocationColorData.liabilityTypeItems, chartThemeColors]);

  const assetColorKey = useMemo(
    () => getAllocationInstitutionColorKeys(
      allocationColorData.assetItems,
      DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT,
      OTHER_INSTITUTIONS_COLOR_KEY
    ).sort().join('|'),
    [allocationColorData.assetItems]
  );
  const liabilityInstitutionColorKey = useMemo(
    () => getAllocationInstitutionColorKeys(
      allocationColorData.liabilityInstitutionItems,
      DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT,
      OTHER_INSTITUTIONS_COLOR_KEY
    ).sort().join('|'),
    [allocationColorData.liabilityInstitutionItems]
  );
  const liabilityTypeColorKey = useMemo(
    () => allocationColorData.liabilityTypeItems.map((item) => item.key).sort().join('|'),
    [allocationColorData.liabilityTypeItems]
  );

  useEffect(() => {
    if (!convertedAccounts.length) return;
    let cancelled = false;

    const cleanColorMap = (source, activeKeys) => {
      const activeSet = new Set(activeKeys.filter(Boolean));
      const clean = {};
      let changed = false;

      Object.entries(source || {}).forEach(([key, color]) => {
        if (activeSet.has(key)) {
          clean[key] = color;
        } else {
          changed = true;
        }
      });

      return { clean, changed };
    };

    Promise.resolve().then(() => {
      if (cancelled) return;
      setChartColors((previous) => {
        const activeAssetKeys = assetColorKey ? assetColorKey.split('|') : [];
        const activeLiabilityInstitutionKeys = liabilityInstitutionColorKey ? liabilityInstitutionColorKey.split('|') : [];
        const activeLiabilityTypeKeys = liabilityTypeColorKey ? liabilityTypeColorKey.split('|') : [];
        const assetResult = cleanColorMap(previous.assetInstitutions, activeAssetKeys);
        const liabilityInstitutionResult = cleanColorMap(previous.liabilityInstitutions, activeLiabilityInstitutionKeys);
        const liabilityTypeResult = cleanColorMap(previous.liabilityTypes, activeLiabilityTypeKeys);
        const changed = assetResult.changed
          || liabilityInstitutionResult.changed
          || liabilityTypeResult.changed;

        if (!changed) return previous;

        const next = {
          ...previous,
          assetInstitutions: assetResult.clean,
          liabilityInstitutions: liabilityInstitutionResult.clean,
          liabilityTypes: liabilityTypeResult.clean,
        };
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [assetColorKey, convertedAccounts.length, liabilityInstitutionColorKey, liabilityTypeColorKey, setChartColors]);

  const handleAllocationChartColorApply = useCallback((mapKey, itemKey, color) => {
    const normalizedColor = normalizeChartColor(color);
    if (!normalizedColor) return;

    setChartColors((previous) => ({
      ...previous,
      [mapKey]: {
        ...(previous[mapKey] || {}),
        [String(itemKey)]: normalizedColor,
      },
    }));
  }, [setChartColors]);

  const handleResetAllocationChartColor = useCallback((mapKey, itemKey) => {
    setChartColors((previous) => {
      if (!previous[mapKey]?.[String(itemKey)]) return previous;
      const nextColors = { ...previous[mapKey] };
      delete nextColors[String(itemKey)];
      return { ...previous, [mapKey]: nextColors };
    });
  }, [setChartColors]);

  const allocationColorScope = useMemo(() => {
    const buildInstitutionOptions = (items, chartColorMap, defaultColorMap) => {
      const visibleItems = items.length > DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT
        ? items.slice(0, DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT - 1)
        : items;
      const options = visibleItems.map((item) => ({
        ...item,
        color: chartColorMap[item.key] || defaultColorMap[item.key],
        defaultColor: defaultColorMap[item.key],
      }));

      if (items.length > DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT) {
        options.push({
          key: OTHER_INSTITUTIONS_COLOR_KEY,
          label: OTHER_INSTITUTIONS_LABEL,
          value: items
            .slice(DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT - 1)
            .reduce((sum, item) => sum + Number(item.value || 0), 0),
          color: chartColorMap[OTHER_INSTITUTIONS_COLOR_KEY] || defaultColorMap[OTHER_INSTITUTIONS_COLOR_KEY],
          defaultColor: defaultColorMap[OTHER_INSTITUTIONS_COLOR_KEY],
        });
      }

      return options;
    };

    if (allocationTab === 'assets') {
      return {
        title: 'Asset Colors',
        mapKey: 'assetInstitutions',
        options: buildInstitutionOptions(
          allocationColorData.assetItems,
          assetInstitutionChartColors,
          assetInstitutionDefaultColors
        ),
      };
    }

    if (allocationLiabView === 'type') {
      return {
        title: 'Liability Type Colors',
        mapKey: 'liabilityTypes',
        options: allocationColorData.liabilityTypeItems.map((item) => ({
          ...item,
          color: liabilityTypeChartColors[item.key] || liabilityTypeDefaultColors[item.key],
          defaultColor: liabilityTypeDefaultColors[item.key],
        })),
      };
    }

    return {
      title: 'Liability Institution Colors',
      mapKey: 'liabilityInstitutions',
      options: buildInstitutionOptions(
        allocationColorData.liabilityInstitutionItems,
        liabilityInstitutionChartColors,
        liabilityInstitutionDefaultColors
      ),
    };
  }, [
    allocationColorData.assetItems,
    allocationColorData.liabilityInstitutionItems,
    allocationColorData.liabilityTypeItems,
    allocationLiabView,
    allocationTab,
    assetInstitutionChartColors,
    assetInstitutionDefaultColors,
    liabilityInstitutionChartColors,
    liabilityInstitutionDefaultColors,
    liabilityTypeChartColors,
    liabilityTypeDefaultColors,
  ]);

  const frozenNetworthHistory = useMemo(() => {
    if (!tourDemoActive || !Array.isArray(networth?.history)) return null;
    const points = networth.history
      .map((point) => {
        const date = String(point?.date || '').slice(0, 10);
        const totalAssets = Number(point?.total_assets);
        const totalLiabilities = Number(point?.total_liabilities);
        const netWorth = Number(point?.net_worth);
        if (!date || !Number.isFinite(netWorth)) return null;
        return {
          date,
          total_assets: Number.isFinite(totalAssets) ? totalAssets : 0,
          total_liabilities: Number.isFinite(totalLiabilities) ? totalLiabilities : 0,
          net_worth: netWorth,
        };
      })
      .filter(Boolean)
      .sort((left, right) => String(left.date).localeCompare(String(right.date)));
    return points.length ? points : null;
  }, [networth, tourDemoActive]);

  const history = useMemo(() => {
    if (frozenNetworthHistory) return frozenNetworthHistory;
    const entries = Object.entries(convertedBalanceHistory || {});
    if (entries.length === 0) return [];
    const dateSet = new Set();
    entries.forEach(([, balances]) => {
      Object.keys(balances || {}).forEach((date) => dateSet.add(date));
    });
    const sortedDates = Array.from(dateSet).sort();
    if (sortedDates.length === 0) return [];
    const running = {};
    return sortedDates.map((date) => {
      entries.forEach(([accountId, balances]) => {
        if (balances && Object.prototype.hasOwnProperty.call(balances, date)) {
          running[accountId] = balances[date];
        }
      });
      let assets = 0;
      let liabilities = 0;
      Object.entries(running).forEach(([accountId, balance]) => {
        const isLiability = accountIsLiabilityById.get(String(accountId));
        const numeric = Number(balance) || 0;
        if (isLiability) {
          liabilities += numeric;
        } else {
          assets += numeric;
        }
      });
      return {
        date,
        total_assets: assets,
        total_liabilities: liabilities,
        net_worth: assets - Math.abs(liabilities),
      };
    });
  }, [convertedBalanceHistory, accountIsLiabilityById, frozenNetworthHistory]);
  const filteredHistory = useMemo(() => {
    const now = tourDemoActive ? new Date(TOUR_DEMO_NOW_ISO) : getAppNow();
    return filterDashboardNetWorthHistory(history, timeframe, customDateRange, now);
  }, [customDateRange, history, timeframe, tourDemoActive]);

  const networthCurrent = useMemo(() => {
    if (!tourDemoActive) return currentTotals;
    const current = networth?.current;
    const totalAssets = Number(current?.total_assets);
    const totalLiabilities = Number(current?.total_liabilities);
    const netWorth = Number(current?.net_worth);
    if (
      Number.isFinite(totalAssets)
      && Number.isFinite(totalLiabilities)
      && Number.isFinite(netWorth)
    ) {
      return {
        total_assets: totalAssets,
        total_liabilities: totalLiabilities,
        net_worth: netWorth,
      };
    }
    return currentTotals;
  }, [currentTotals, networth, tourDemoActive]);
  const { change, assetsChange, liabilitiesChange } = useMemo(() => ({
    change: getAggregateHistoryMetricChange(filteredHistory, 'net_worth'),
    assetsChange: getAggregateHistoryMetricChange(filteredHistory, 'total_assets'),
    liabilitiesChange: getAggregateHistoryMetricChange(filteredHistory, 'total_liabilities'),
  }), [filteredHistory]);

  // Headline figures: for a historical custom range, show the net worth held AT the end of the
  // selected window rather than today's balance — a defunct/imported account that's $0 today should
  // still show the value it held across the chosen months. These come straight off the chart series
  // (filteredHistory), which INCLUDES imported/defunct accounts, so the headline and its change both
  // match the graph. Preset/live ranges end at "now", so they keep the live current totals.
  const displayTotals = useMemo(() => {
    if (timeframe !== PORTFOLIO_CUSTOM_TIMEFRAME_KEY) return networthCurrent;
    const last = filteredHistory.length ? filteredHistory[filteredHistory.length - 1] : null;
    return last
      ? { net_worth: last.net_worth, total_assets: last.total_assets, total_liabilities: last.total_liabilities }
      : networthCurrent;
  }, [timeframe, networthCurrent, filteredHistory]);

  const netWorthDefaultColor = chartThemeColors.netWorth;
  const netWorthChartColor = resolvePortfolioNetWorthChartColor(chartColors, chartThemeColors);
  const handleNetWorthChartColorApply = useCallback((color) => {
    const normalizedColor = normalizeChartColor(color);
    if (!normalizedColor) return;
    setChartColors((previous) => ({ ...previous, netWorth: normalizedColor }));
  }, [setChartColors]);

  const handleResetNetWorthChartColor = useCallback(() => {
    setChartColors((previous) => (
      previous.netWorth == null ? previous : { ...previous, netWorth: null }
    ));
  }, [setChartColors]);

  const timeframeDisplayLabel = timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY
    ? 'Custom Range'
    : PORTFOLIO_TIMEFRAMES.find((item) => item.label === timeframe)?.triggerLabel || timeframe;
  const overviewCurrency = primaryCurrency;
  const overviewChangeTone = !change ? 'neutral' : change.positive ? 'positive' : 'negative';
  const assetsChangeTone = !assetsChange ? 'neutral' : assetsChange.diff >= 0 ? 'positive' : 'negative';
  const liabilitiesChangeTone = !liabilitiesChange ? 'neutral' : liabilitiesChange.diff > 0 ? 'negative' : liabilitiesChange.diff < 0 ? 'positive' : 'neutral';
  // Signed dollar change + a ▲/▼ + magnitude percent (the triangle carries direction, tone the
  // good/bad). The % rides in the FitMetricValue change cluster: it compacts with the amount and
  // surfaces full figures in the tooltip when the line shrinks (a runaway ratio folds via FitMoney,
  // never a "×N" multiple).
  const buildChangeData = (entry, tone) => {
    if (!entry) return null;
    const amountFull = formatOverviewChangeValue(entry, overviewCurrency);
    const dir = entry.diff >= 0 ? '▲' : '▼';
    const magFull = formatChangePercentFull(entry.pct);     // signed, e.g. "+186.5%"
    const magCompact = formatChangePercentCompact(entry.pct);
    const pctFull = magFull ? `${dir} ${magFull.replace(/^[+-]/, '')}` : null;       // "▲ 186.5%"
    const pctCompact = magCompact ? `${dir} ${magCompact.replace(/^[+-]/, '')}` : null;
    return {
      amountFull,
      amountCompact: formatSignedCompactMoney(entry.diff, overviewCurrency),
      pctFull,
      pctCompact,
      tooltip: pctFull ? `${amountFull} ${pctFull}` : amountFull,
      suffix: (<span>{timeframeDisplayLabel}</span>),
      tone,
    };
  };
  const overviewChangeData = buildChangeData(change, overviewChangeTone);
  const assetsChangeData = buildChangeData(assetsChange, assetsChangeTone);
  const liabilitiesChangeData = buildChangeData(liabilitiesChange, liabilitiesChangeTone);
  const visibleDashboardWidgetIds = useMemo(
    () => dashboardLayout.order.filter((widgetId) => dashboardLayout.visible[widgetId] !== false),
    [dashboardLayout]
  );
  const dashboardWidgetGroups = useMemo(() => {
    const allGroupIds = [...DASHBOARD_TOP_BAND_IDS, ...DASHBOARD_MAIN_CUSTOMIZE_GROUPS.map((g) => g.id)];
    return allGroupIds.reduce((groups, groupId) => {
      groups[groupId] = visibleDashboardWidgetIds.filter(
        (widgetId) => getDashboardWidgetLayoutGroup(widgetId, dashboardLayout.columns) === groupId
      );
      return groups;
    }, {});
  }, [dashboardLayout.columns, visibleDashboardWidgetIds]);
  const visibleMetricWidgetIds = dashboardWidgetGroups.metrics || [];
  const visibleMetricWidgetCount = visibleMetricWidgetIds.length;
  const dashboardMetricBandClass = visibleMetricWidgetCount === 1
    ? 'has-one-metric'
    : visibleMetricWidgetCount === 2
      ? 'has-two-metrics'
      : visibleMetricWidgetCount > 2
        ? 'has-three-metrics'
        : 'has-no-metrics';
  const visibleMainGroupIds = ['main:left', 'main:right'].filter(
    (groupId) => (dashboardWidgetGroups[groupId] || []).length > 0
  );
  const hasMainColumnWidgets = visibleMainGroupIds.length > 0;

  const dashboardScopeControl = (
    <div className="investments-filter-popover dashboard-scope-popover" ref={dashboardScopePopoverRef}>
      <ScopeSelectorTrigger
        isOpen={showVisDropdown}
        summary={scopeSummary}
        controls="dashboard-scope-filter-panel"
        className={hasHiddenInstitutions ? 'has-hidden-sources' : ''}
        onClick={handleToggleScopeDropdown}
      />
      <div
        id="dashboard-scope-filter-panel"
        role="dialog"
        aria-label="Scope filters"
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

  const dashboardCustomizeButton = (
    <button
      type="button"
      ref={dashboardCustomizeTriggerRef}
      className={`dashboard-action-trigger dashboard-customize-trigger app-control-root ${isCustomizeOpen ? 'is-open' : ''} ${isArrangeMode ? 'is-arranging' : ''}`.trim()}
      aria-haspopup="dialog"
      aria-expanded={isCustomizeOpen}
      onClick={toggleCustomizeDrawer}
    >
      <span className="dashboard-action-icon app-control-icon" aria-hidden="true">
        <LuSlidersHorizontal />
      </span>
      <span className="dashboard-action-label app-control-label">Customize</span>
    </button>
  );

  const renderDashboardWidgetContent = (widgetId) => {
    switch (widgetId) {
      case 'market-strip':
        return <MarketStrip payload={marketStrip} onWatchlistChange={handleMarketStripWatchlistChange} />;
      case 'market-news':
        return <MarketNewsPanel payload={marketNews} />;
      case 'net-worth':
        return (
          <OverviewMetricCard
            label="Net Worth"
            value={balancesHidden ? '******' : (
              <OverviewMetricValue
                numberFull={formatOverviewMoney(displayTotals?.net_worth, overviewCurrency)}
                numberCompact={formatCompactMoney(Number(displayTotals?.net_worth) || 0, overviewCurrency)}
                change={overviewChangeData}
              />
            )}
            tone="accent"
          />
        );
      case 'assets':
        return (
          <OverviewMetricCard
            label="Total Assets"
            value={balancesHidden ? '******' : (
              <OverviewMetricValue
                numberFull={formatOverviewMoney(displayTotals?.total_assets, overviewCurrency)}
                numberCompact={formatCompactMoney(Number(displayTotals?.total_assets) || 0, overviewCurrency)}
                change={assetsChangeData}
              />
            )}
            tone="positive"
            icon={MdAccountBalance}
          />
        );
      case 'liabilities':
        return (
          <OverviewMetricCard
            label="Total Liabilities"
            value={balancesHidden ? '******' : (
              <OverviewMetricValue
                numberFull={formatOverviewMoney(Math.abs(displayTotals?.total_liabilities || 0), overviewCurrency)}
                numberCompact={formatCompactMoney(Math.abs(Number(displayTotals?.total_liabilities) || 0), overviewCurrency)}
                change={liabilitiesChangeData}
              />
            )}
            tone="negative"
            icon={MdCreditCard}
          />
        );
      case 'net-worth-history':
        return (
          <div className="panel-shell chart-card dashboard-networth-card">
            <ChartColorPopover title="Net Worth" className="networth-color-popover">
              <ChartColorRow
                label="Net worth graph"
                color={netWorthChartColor}
                defaultColor={netWorthDefaultColor}
                onApply={handleNetWorthChartColorApply}
                onReset={handleResetNetWorthChartColor}
              />
            </ChartColorPopover>
            <div className="dashboard-networth-chart-header">
              <h2>Net Worth History</h2>
              <span
                className="dashboard-networth-info"
                data-tooltip={NETWORTH_HISTORY_METHODOLOGY}
                aria-label={NETWORTH_HISTORY_METHODOLOGY}
                role="img"
                tabIndex={0}
              >
                <MdInfoOutline size={16} aria-hidden="true" />
              </span>
            </div>
            <NetWorthChart
              history={filteredHistory}
              balancesHidden={balancesHidden}
              height={224}
              color={netWorthChartColor}
              currency={primaryCurrency}
              chartColors={chartThemeColors}
            />
          </div>
        );
      case 'recent-transactions':
        return (
          <RecentTransactionsPanel
            transactions={recentTransactions}
            status={recentTransactionsStatus}
            balancesHidden={balancesHidden}
          />
        );
      case 'asset-allocation':
        return (
          <div className="panel-shell chart-card portfolio-allocation-card dashboard-allocation-card">
            <ChartColorPopover title={allocationColorScope.title} className="allocation-color-popover">
              {allocationColorScope.options.length > 0 ? (
                allocationColorScope.options.map((option) => (
                  <ChartColorRow
                    key={option.key}
                    label={option.label}
                    color={option.color}
                    defaultColor={option.defaultColor}
                    onApply={(color) => handleAllocationChartColorApply(allocationColorScope.mapKey, option.key, color)}
                    onReset={() => handleResetAllocationChartColor(allocationColorScope.mapKey, option.key)}
                  />
                ))
              ) : (
                <div className="chart-color-empty">No choices in this view</div>
              )}
            </ChartColorPopover>
            <div className="dashboard-allocation-header">
              <h2>Assets & Liabilities</h2>
              <div className="dashboard-allocation-header-controls" role="group" aria-label="Allocation view">
                <button
                  type="button"
                  className={`alloc-tab app-control-root${allocationTab === 'assets' ? ' active' : ''}`}
                  onClick={() => setAllocationTab('assets')}
                >
                  <span className="app-control-label">Assets</span>
                </button>
                <button
                  type="button"
                  className={`alloc-tab app-control-root${allocationTab === 'liabilities' ? ' active' : ''}`}
                  onClick={() => setAllocationTab('liabilities')}
                >
                  <span className="app-control-label">Liabilities</span>
                </button>
              </div>
            </div>
            <div className="dashboard-allocation-card-body">
              <AllocationChart
                accounts={convertedAccounts}
                balancesHidden={balancesHidden}
                currency={primaryCurrency}
                assetTotal={currentTotals.total_assets}
                liabilityTotal={currentTotals.total_liabilities}
                tab={allocationTab}
                liabView={allocationLiabView}
                onLiabViewChange={setAllocationLiabView}
                assetInstitutionColors={assetInstitutionChartColors}
                liabilityInstitutionColors={liabilityInstitutionChartColors}
                liabilityTypeColors={liabilityTypeChartColors}
                assetInstitutionDefaultColors={assetInstitutionDefaultColors}
                liabilityInstitutionDefaultColors={liabilityInstitutionDefaultColors}
                liabilityTypeDefaultColors={liabilityTypeDefaultColors}
                chartColors={chartThemeColors}
                legendVariant="bars"
                legendRowLimit={DASHBOARD_ALLOCATION_LEGEND_ROW_LIMIT}
                liabilityViewStepper
                donutHeight={CASH_FLOW_DONUT_HEIGHT}
              />
            </div>
          </div>
        );
      case 'institution-status':
        return (
          <InstitutionStatusPanel
            institutions={data.institutions || []}
            accounts={convertedAccounts}
            syncActivity={syncActivity}
            transactionImportStatus={data.transactionImportStatus}
            activeSyncBatches={activeSyncBatches}
            autoSyncStates={autoSyncStates}
            autoSyncInProgress={autoSyncInProgress}
            balancesHidden={balancesHidden}
            currency={overviewCurrency}
          />
        );
      case 'cash-flow':
        return (
          <DashboardCashFlowPanel
            currency={overviewCurrency}
            balancesHidden={balancesHidden}
            refreshId={dataRefreshId}
            tourDemoActive={tourDemoActive}
            themeColors={themeColors}
            chartThemeColors={chartThemeColors}
          />
        );
      default:
        return null;
    }
  };

  const renderDashboardLayoutPanel = (widgetId) => {
    const widget = DASHBOARD_WIDGET_BY_ID.get(widgetId);
    if (!widget) return null;

    const isDragging = draggedDashboardWidgetId === widgetId;
    const isDropTarget = dashboardDropTargetId === widgetId && draggedDashboardWidgetId !== widgetId;

    return (
      <div
        key={widgetId}
        data-dashboard-widget-id={widgetId}
        className={`dashboard-layout-panel is-${widget.kind} ${isArrangeMode ? 'is-arrangeable' : ''} ${isDragging ? 'is-dragging' : ''} ${isDropTarget ? 'is-drop-target' : ''}`.trim()}
        draggable={isArrangeMode}
        aria-grabbed={isArrangeMode ? isDragging : undefined}
        onDragStart={(event) => handleDashboardWidgetDragStart(event, widgetId)}
        onDragOver={(event) => handleDashboardWidgetDragOver(event, widgetId)}
        onDrop={(event) => handleDashboardWidgetDrop(event, widgetId)}
        onDragEnd={clearDashboardWidgetDrag}
      >
        {isArrangeMode ? (
          <div className="dashboard-panel-drag-affordance" aria-hidden="true">
            <MdDragIndicator size={18} />
          </div>
        ) : null}
        {renderDashboardWidgetContent(widgetId)}
      </div>
    );
  };

  const dashboardCustomizeDrawer = (
    <div
      ref={dashboardCustomizeTrayRef}
      className={`app-edge-tray dashboard-customize-tray ${isCustomizeOpen ? 'is-open' : ''}`.trim()}
      aria-hidden={!isCustomizeOpen}
    >
      <div className="app-edge-tray-backdrop" aria-hidden="true" />
      <div className="app-edge-tray-shell">
        <div
          className="app-detail-drawer-panel dashboard-customize-panel"
          role="dialog"
          aria-label="Customize dashboard"
        >
          <div className="app-detail-drawer-body dashboard-customize-body">
            <div className="dashboard-customize-actions">
              <button
                type="button"
                role="switch"
                aria-checked={isArrangeMode}
                className={`dashboard-customize-arrange-toggle ${isArrangeMode ? 'is-active' : ''}`.trim()}
                onClick={() => setIsArrangeMode((previous) => !previous)}
              >
                <span className="dashboard-customize-action-copy">Arrange layout</span>
                <span className="dashboard-customize-switch" aria-hidden="true">
                  <span />
                </span>
              </button>
              <button
                type="button"
                className="dashboard-customize-reset app-control-root"
                onClick={handleResetDashboardLayout}
              >
                <span className="app-control-icon" aria-hidden="true">
                  <MdRestartAlt />
                </span>
                <span className="app-control-label">Reset</span>
              </button>
            </div>

            <div
              className="dashboard-widget-list"
              aria-label="Dashboard widgets"
            >
              {[
                ...dashboardLayout.bandOrder.map((bandId, bandIndex) => ({
                  id: bandId,
                  label: DASHBOARD_TOP_BAND_LABELS[bandId] || bandId,
                  isTopBand: true,
                  bandIndex,
                })),
                ...DASHBOARD_MAIN_CUSTOMIZE_GROUPS.map((group) => ({
                  id: group.id,
                  label: group.label,
                  isTopBand: false,
                })),
              ].map((group) => {
                const groupWidgetIds = dashboardLayout.order.filter(
                  (widgetId) => getDashboardWidgetLayoutGroup(widgetId, dashboardLayout.columns) === group.id
                );
                const isBandDragging = group.isTopBand && draggedDashboardBandId === group.id;
                const isBandDropTarget = group.isTopBand
                  && dashboardBandDropTarget?.bandId === group.id
                  && draggedDashboardBandId !== group.id;
                return (
                  <div
                    key={group.id}
                    className={`dashboard-widget-group ${isBandDragging ? 'is-band-dragging' : ''} ${isBandDropTarget ? 'is-band-drop-target' : ''}`.trim()}
                    data-dashboard-widget-group={group.id}
                    draggable={isArrangeMode && group.isTopBand}
                    onDragStart={group.isTopBand ? (event) => handleDashboardBandDragStart(event, group.id) : undefined}
                    onDragOver={(event) => {
                      if (group.isTopBand && (draggedDashboardBandId || event.dataTransfer.types.includes('application/x-dashboard-band'))) {
                        handleDashboardBandDragOver(event, group.id);
                      } else {
                        handleDashboardWidgetContainerDragOver(event, group.id);
                      }
                    }}
                    onDrop={(event) => {
                      if (group.isTopBand && (draggedDashboardBandId || event.dataTransfer.types.includes('application/x-dashboard-band'))) {
                        handleDashboardBandDrop(event, group.id);
                      } else {
                        handleDashboardWidgetContainerDrop(event, group.id);
                      }
                    }}
                    onDragEnd={group.isTopBand ? clearDashboardBandDrag : undefined}
                  >
                    <div
                      className={`dashboard-widget-group-header ${isArrangeMode && group.isTopBand ? 'is-band-draggable' : ''}`.trim()}
                    >
                      {isArrangeMode && group.isTopBand ? (
                        <span className="dashboard-widget-band-drag-handle" aria-hidden="true">
                          <MdDragIndicator size={16} />
                        </span>
                      ) : null}
                      <span className="dashboard-widget-group-label">{group.label}</span>
                      {isArrangeMode && group.isTopBand ? (
                        <span className="dashboard-widget-group-actions">
                          <button
                            type="button"
                            className="dashboard-widget-move-btn"
                            aria-label={`Move ${group.label} band up`}
                            disabled={group.bandIndex === 0}
                            onPointerDown={(event) => event.stopPropagation()}
                            onClick={() => handleMoveDashboardBand(group.id, -1)}
                          >
                            <TriangleIcon direction="up" />
                          </button>
                          <button
                            type="button"
                            className="dashboard-widget-move-btn"
                            aria-label={`Move ${group.label} band down`}
                            disabled={group.bandIndex === dashboardLayout.bandOrder.length - 1}
                            onPointerDown={(event) => event.stopPropagation()}
                            onClick={() => handleMoveDashboardBand(group.id, 1)}
                          >
                            <TriangleIcon direction="down" />
                          </button>
                        </span>
                      ) : null}
                    </div>
                    {groupWidgetIds.map((widgetId, index) => {
                      const widget = DASHBOARD_WIDGET_BY_ID.get(widgetId);
                      if (!widget) return null;
                      const isWidgetVisible = dashboardLayout.visible[widgetId] !== false;
                      const isDragging = draggedDashboardWidgetId === widgetId;
                      const isDropTarget = dashboardDropTargetId === widgetId && draggedDashboardWidgetId !== widgetId;

                      return (
                        <div
                          key={widgetId}
                          data-dashboard-widget-id={widgetId}
                          className={`dashboard-widget-row ${isWidgetVisible ? 'is-visible' : 'is-hidden'} ${isArrangeMode ? 'is-arrangeable' : ''} ${isDragging ? 'is-dragging' : ''} ${isDropTarget ? 'is-drop-target' : ''}`.trim()}
                          draggable={isArrangeMode}
                          onDragStart={(event) => handleDashboardWidgetDragStart(event, widgetId)}
                          onDragOver={(event) => handleDashboardWidgetDragOver(event, widgetId)}
                          onDrop={(event) => handleDashboardWidgetDrop(event, widgetId)}
                          onDragEnd={clearDashboardWidgetDrag}
                        >
                          {isArrangeMode ? (
                            <span className="dashboard-widget-drag-handle" aria-hidden="true">
                              <MdDragIndicator size={18} />
                            </span>
                          ) : null}
                          <span className="dashboard-widget-row-label">{widget.label}</span>
                          <span className="dashboard-widget-row-actions">
                            {isArrangeMode ? (
                              <>
                                <button
                                  type="button"
                                  className="dashboard-widget-move-btn"
                                  aria-label={`Move ${widget.label} up`}
                                  disabled={index === 0}
                                  onPointerDown={(event) => event.stopPropagation()}
                                  onClick={() => handleMoveDashboardWidget(widgetId, -1)}
                                >
                                  <TriangleIcon direction="up" />
                                </button>
                                <button
                                  type="button"
                                  className="dashboard-widget-move-btn"
                                  aria-label={`Move ${widget.label} down`}
                                  disabled={index === groupWidgetIds.length - 1}
                                  onPointerDown={(event) => event.stopPropagation()}
                                  onClick={() => handleMoveDashboardWidget(widgetId, 1)}
                                >
                                  <TriangleIcon direction="down" />
                                </button>
                              </>
                            ) : null}
                            <button
                              type="button"
                              role="switch"
                              aria-checked={isWidgetVisible}
                              aria-label={`${isWidgetVisible ? 'Hide' : 'Show'} ${widget.label}`}
                              className={`dashboard-widget-visibility-switch ${isWidgetVisible ? 'is-on' : ''}`.trim()}
                              onPointerDown={(event) => event.stopPropagation()}
                              onClick={() => handleToggleDashboardWidget(widgetId)}
                            >
                              <span aria-hidden="true" />
                            </button>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </div>
  );

  return (
    <>
      <CsvExportButton
        dataset="networth-history"
        label="Export net worth history"
        getParams={() => {
          const points = filteredHistory || [];
          if (points.length === 0) return {};
          return { start_date: points[0].date, end_date: points[points.length - 1].date };
        }}
      />
      {dashboardToolbarScopeSlot ? createPortal(dashboardScopeControl, dashboardToolbarScopeSlot) : null}
      {dashboardToolbarCustomizeSlot ? createPortal(
        <>
          {dashboardCustomizeButton}
          {isCustomizeOpen ? dashboardCustomizeDrawer : null}
        </>,
        dashboardToolbarCustomizeSlot,
      ) : null}
      <div className="dashboard-page-content">
        <AppStatusNotice
          title="Scope update failed"
          message={scopeSaveError}
          onDismiss={() => setScopeSaveError('')}
        />
        <div className={`page-frame dashboard-layout ${isArrangeMode ? 'is-arranging' : ''}`.trim()} aria-label="Dashboard widgets">
          {visibleDashboardWidgetIds.length > 0 ? (
            <>
              {dashboardLayout.bandOrder.map((bandId) => {
                const bandWidgetIds = dashboardWidgetGroups[bandId] || [];
                if (bandWidgetIds.length === 0) return null;

                if (bandId === 'metrics') {
                  return (
                    <div
                      key={bandId}
                      className={`dashboard-layout-zone dashboard-metric-band ${dashboardMetricBandClass}`.trim()}
                      onDragOver={(event) => handleDashboardWidgetContainerDragOver(event, 'metrics')}
                      onDrop={(event) => handleDashboardWidgetContainerDrop(event, 'metrics')}
                    >
                      {bandWidgetIds.map(renderDashboardLayoutPanel)}
                    </div>
                  );
                }

                const zoneClass = bandId === 'top'
                  ? 'dashboard-top-zone'
                  : bandId === 'market-news'
                    ? 'dashboard-market-news-zone'
                    : 'dashboard-net-worth-history-zone';

                return (
                  <div
                    key={bandId}
                    className={`dashboard-layout-zone ${zoneClass}`}
                    onDragOver={(event) => handleDashboardWidgetContainerDragOver(event, bandId)}
                    onDrop={(event) => handleDashboardWidgetContainerDrop(event, bandId)}
                  >
                    {bandWidgetIds.map(renderDashboardLayoutPanel)}
                  </div>
                );
              })}

              {hasMainColumnWidgets ? (
                <div className={`page-split dashboard-main-columns ${visibleMainGroupIds.length === 1 ? 'has-one-column' : ''}`.trim()}>
                  {visibleMainGroupIds.map((groupId) => (
                    <div
                      key={groupId}
                      className={`dashboard-layout-zone dashboard-main-column is-${groupId.endsWith(':right') ? 'right' : 'left'}`.trim()}
                      onDragOver={(event) => handleDashboardWidgetContainerDragOver(event, groupId)}
                      onDrop={(event) => handleDashboardWidgetContainerDrop(event, groupId)}
                    >
                      {(dashboardWidgetGroups[groupId] || []).map(renderDashboardLayoutPanel)}
                    </div>
                  ))}
                </div>
              ) : null}
            </>
          ) : (
            <section className="panel-shell chart-card dashboard-layout-empty">
              <h2>No dashboard widgets selected</h2>
              <button type="button" className="dashboard-layout-empty-customize app-control-root" onClick={() => setIsCustomizeOpen(true)}>
                <span className="app-control-label">Customize</span>
              </button>
            </section>
          )}
        </div>
      </div>
    </>
  );
}

export default Dashboard;
