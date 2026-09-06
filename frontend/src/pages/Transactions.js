import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useLocation, useNavigate } from 'react-router-dom';
import { MdArrowForward, MdBlock, MdCheck, MdClose, MdLabel, MdSearch } from 'react-icons/md';
import { useRightTrayOpenState, useTheme } from '../appState';
import InstitutionAccountSelector, { ScopeSelectorTrigger, formatScopeSelectionSummary } from '../components/InstitutionAccountSelector';
import InstitutionLogo from '../components/InstitutionLogo';
import TimelineTrigger from '../components/TimelineTrigger';
import ControlChevron from '../components/ControlChevron';
import TimelineCustomRangePicker from '../components/TimelineCustomRangePicker';
import TwemojiIcon from '../components/TwemojiIcon';
import CategoryPill from '../components/CategoryPill';
import TransactionDetailDrawer from '../components/TransactionDetailDrawer';
import HorizontalScrollProxy from '../components/HorizontalScrollProxy';
import { getTourHint, isTourDemoActive } from '../components/tourDemoData';
import CsvExportButton from '../components/CsvExportButton';
import AppStatusNotice from '../components/AppStatusNotice';
import AccountTypeBadge from '../components/AccountTypeBadge';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import useTimelineCustomRangeDraft from '../hooks/useTimelineCustomRangeDraft';
import { API } from '../config';
import { cloneScopeInstitutions } from '../utils/portfolioViewUtils';
import { subscribeMainWindowZoomStatus } from '../utils/desktopBridge';
import { persistVisibilityScope, reconcileVisibilityScope } from '../utils/visibilityScope';
import { readTransactionCollectionResponse } from '../utils/apiResponse';
import { loadTransactionCategories } from '../utils/transactionCategories';
import { getAppNow } from '../utils/appClock';
import { getRemoteDataViewState } from '../utils/remoteDataState';
import { getCategoryThemeVars } from '../utils/categoryColors';
import { formatSignedDollarAmount as formatAmount, currencySymbolFor, formatSignedCompactMoney } from '../utils/format';
import {
  getTransactionPrimaryDescription,
  isCurrencyTransactionSymbol,
} from '../utils/transactionDescription';
import FitMoney from '../components/FitMoney';
import {
  formatLongDateValue as formatGroupDate,
  formatShortDateValue as formatShortDate,
  toLocalDateValue,
} from '../utils/date';
import './Transactions.css';

const DATE_PRESETS = [
  { key: '1D', label: '1D', triggerLabel: '1 Day', days: 1 },
  { key: '5D', label: '5D', triggerLabel: '5 Days', days: 5 },
  { key: '30D', label: '30D', triggerLabel: '30 Days', days: 30 },
  { key: '90D', label: '90D', triggerLabel: '90 Days', days: 90 },
  { key: '6M', label: '6M', triggerLabel: '6 Months', days: 180 },
  { key: 'YTD', label: 'YTD', triggerLabel: 'Year to Date', days: null, ytd: true },
  { key: '1Y', label: '1Y', triggerLabel: '1 Year', days: 365 },
  { key: 'All', label: 'All', triggerLabel: 'All Time', days: null },
];

const CUSTOM_TIMELINE_KEY = 'CUSTOM';

const PAGE_SIZE_OPTIONS = [100, 250, 500];
const DEFAULT_PAGE_SIZE = PAGE_SIZE_OPTIONS[0];
const TRANSACTION_DETAIL_TRAY_MIN_USABLE_HEIGHT = 420;
const TRANSACTION_DETAIL_TRAY_PAGER_GAP = 12;
const TRANSACTION_DETAIL_TRAY_BOTTOM_FLOOR = 16;

function getDateKey(dateStr) {
  return String(dateStr || '').slice(0, 10);
}

function normalizeTransactionSearchQuery(value) {
  return String(value || '').trim().toLowerCase();
}

function setBottomPaginationSuppressed(element, isSuppressed) {
  if (!element) return;
  element.classList.toggle('is-tray-suppressed', isSuppressed);
  if (isSuppressed) {
    element.setAttribute('aria-hidden', 'true');
  } else {
    element.removeAttribute('aria-hidden');
  }
}

function getStartDate(preset) {
  if (!preset?.days && !preset?.ytd) return null;
  const now = getAppNow();
  if (preset.ytd) {
    return toLocalDateValue(new Date(now.getFullYear(), 0, 1));
  }
  const d = getAppNow();
  d.setDate(d.getDate() - preset.days);
  return toLocalDateValue(d);
}

function getTimelineBounds(timelineKey, customDateRange) {
  if (timelineKey === CUSTOM_TIMELINE_KEY) {
    return {
      startDate: customDateRange.start || null,
      endDate: customDateRange.end || null,
    };
  }

  const preset = DATE_PRESETS.find((item) => item.key === timelineKey);
  return {
    startDate: getStartDate(preset),
    endDate: null,
  };
}

function getTimelineSummary(timelineKey, customDateRange) {
  if (timelineKey === CUSTOM_TIMELINE_KEY) {
    const { start, end } = customDateRange;
    if (start && end) {
      return `${formatShortDate(start)} - ${formatShortDate(end)}`;
    }
    if (start) return `From ${formatShortDate(start)}`;
    if (end) return `Until ${formatShortDate(end)}`;
    return 'Custom Range';
  }

  return DATE_PRESETS.find((item) => item.key === timelineKey)?.triggerLabel || 'Timeline';
}

function getPaginationItems(currentPage, totalPages) {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }

  if (currentPage <= 4) {
    return [1, 2, 3, 4, 5, 'ellipsis', totalPages];
  }

  if (currentPage >= totalPages - 3) {
    return [1, 'ellipsis', totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages];
  }

  return [1, 'ellipsis', currentPage - 1, currentPage, currentPage + 1, 'ellipsis', totalPages];
}

function clampPageNumber(value, totalPages) {
  if (!Number.isFinite(value)) {
    return 1;
  }

  return Math.min(Math.max(1, value), Math.max(1, totalPages));
}

function joinClassNames(...values) {
  return values.filter(Boolean).join(' ');
}

function isSameTransactionDetail(current, transaction) {
  return current?.transaction?.id != null
    && transaction?.id != null
    && String(current.transaction.id) === String(transaction.id);
}

const TRANSACTIONS_HORIZONTAL_SCROLL_PROXY_OPTIONS = {
  className: 'transactions-table-horizontal-scrollbar',
  innerClassName: 'transactions-table-horizontal-scrollbar-inner',
  contentWidthProperty: '--transactions-horizontal-scroll-content-width',
  targetViewportProperty: '--horizontal-scroll-viewport-width',
  targetScrollProperty: '--horizontal-scroll-left',
  resetOnTargetWidthChange: true,
  clampTolerance: 0,
  resolveTarget: (controller) => controller.closest('[data-transactions-horizontal-scroll-sync]'),
  getContentElements: ({ target }) => [
    target.querySelector('.transactions-table-scroll-sizer'),
    target.querySelector('.transactions-table-scroll-surface'),
    target.querySelector('.transactions-table-header-strip'),
  ],
  getObservedElements: ({ controller, target }) => [
    target,
    ...Array.from(target.children).filter((child) => child !== controller),
  ],
};

function TransactionsHorizontalScrollProxy({ className = '' }) {
  return <HorizontalScrollProxy options={TRANSACTIONS_HORIZONTAL_SCROLL_PROXY_OPTIONS} className={className} />;
}

function CategoryFilterTrigger({
  isOpen = false,
  summary,
  icon: Icon = MdLabel,
  onClick,
  className = '',
  controls,
  ariaLabel = 'Categories',
}) {
  return (
    <button
      type="button"
      className={joinClassNames('scope-selector-trigger', 'transactions-type-trigger', 'app-control-root', isOpen ? 'is-open' : '', className)}
      aria-haspopup="dialog"
      aria-expanded={isOpen}
      aria-controls={controls}
      aria-label={`${ariaLabel}: ${summary}`}
      onClick={onClick}
    >
      <span className="scope-selector-icon transactions-type-trigger-icon app-control-icon" aria-hidden="true">
        <Icon />
      </span>
      <span className="scope-selector-summary transactions-type-trigger-summary app-control-label">
        {summary}
      </span>
      <span className={joinClassNames('scope-selector-chevron', 'transactions-type-trigger-chevron', 'app-control-chevron', isOpen ? 'is-open' : '')} aria-hidden="true">
        <ControlChevron />
      </span>
    </button>
  );
}

function Transactions({
  data,
  dataRefreshId = 0,
  allScopeInstitutions = [],
  fetchAllScopeInstitutions,
  onDataChange,
  timeframe,
  setTimeframe,
  customDateRange: globalCustomDateRange,
  setCustomDateRange: setGlobalCustomDateRange,
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const { mode } = useTheme();
  const handoffFilter = location.state?.cashFlowFilter || null;

  // A Cash Flow "View all" handoff filters this page to the source category +
  // period, but that must NOT overwrite the user's global (persisted) timeline —
  // they're just peeking at one category for one month, and the rest of the app
  // should keep whatever timeline they chose. So the handoff's date range lives
  // in a transient, page-local override (seeded at init to avoid a global-timeline
  // flash); only a manual timeline pick writes through to the global preference.
  const [handoffTimeline, setHandoffTimeline] = useState(
    () => (handoffFilter?.startDate || handoffFilter?.endDate
      ? { start: handoffFilter.startDate || '', end: handoffFilter.endDate || '' }
      : null),
  );
  // Cash Flow → Future P/L "View all" hands off the futures asset class (no
  // category) so this page lists every futures buy/sell behind the realized P&L.
  // Transient like the handoff timeline — a manual timeline change ends the peek.
  const [handoffAssetCategory, setHandoffAssetCategory] = useState(
    () => handoffFilter?.assetCategory || null,
  );
  const [handoffAccountIds, setHandoffAccountIds] = useState(
    () => (Array.isArray(handoffFilter?.accountIds) ? handoffFilter.accountIds : null),
  );
  const [handoffInstitutionIds, setHandoffInstitutionIds] = useState(
    () => (Array.isArray(handoffFilter?.institutionIds) ? handoffFilter.institutionIds : null),
  );
  const timelineKey = handoffTimeline ? CUSTOM_TIMELINE_KEY : timeframe;
  const customDateRange = handoffTimeline || globalCustomDateRange;
  const setTimelineKey = useCallback((key) => {
    setHandoffTimeline(null);
    setHandoffAssetCategory(null);
    setTimeframe(key);
  }, [setTimeframe]);
  const setCustomDateRange = useCallback((range) => {
    setHandoffTimeline(null);
    setHandoffAssetCategory(null);
    setGlobalCustomDateRange(range);
  }, [setGlobalCustomDateRange]);
  const [transactions, setTransactions] = useState([]);
  const [totalTransactions, setTotalTransactions] = useState(0);
  const [hasLoadedTransactions, setHasLoadedTransactions] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  const [localScopeInstitutions, setLocalScopeInstitutions] = useState([]);
  const transactionsScopeDraftRef = useRef(null);
  const [categories, setCategories] = useState([]);
  // Seed from a Cash Flow "View all" handoff at init so the async categories
  // fetch (which defaults to all-leaves when not initialized) doesn't clobber it.
  const [selectedCategoryIds, setSelectedCategoryIds] = useState(
    () => (handoffFilter?.categoryIds?.length ? new Set(handoffFilter.categoryIds) : new Set()),
  );
  const [categorySelectionInitialized, setCategorySelectionInitialized] = useState(
    () => Boolean(handoffFilter?.categoryIds?.length),
  );
  const [detailDrawer, setDetailDrawer] = useState(null); // { transaction }
  const transactionDetailTray = useRightTrayOpenState('transaction-detail', Boolean(detailDrawer));
  // Welcome tour (demo mode): open a transaction's detail drawer ONCE (when the list first loads)
  // so the edit affordance shows — guarded so refetches don't re-open it.
  const tourDrawerOpenedRef = useRef(false);
  useEffect(() => {
    if (!isTourDemoActive()) { tourDrawerOpenedRef.current = false; return; }
    const hint = getTourHint();
    if (!hint || hint.page !== 'transactions' || !hint.openDetail) return;
    if (transactions.length > 0 && !tourDrawerOpenedRef.current) {
      tourDrawerOpenedRef.current = true;
      setDetailDrawer({ transaction: transactions[0] });
    }
  }, [transactions]);
  const currentTourHint = getTourHint();
  const transactionTourTrayLocked = Boolean(
    detailDrawer
    && isTourDemoActive()
    && currentTourHint?.page === 'transactions'
    && currentTourHint.openDetail
  );
  const handleTransactionDetailSelect = useCallback((transaction) => {
    setDetailDrawer((current) => {
      if (isSameTransactionDetail(current, transaction)) {
        return transactionTourTrayLocked ? current : null;
      }
      return { transaction };
    });
  }, [transactionTourTrayLocked]);
  const [searchQueryDraft, setSearchQueryDraft] = useState('');
  const [appliedSearchQuery, setAppliedSearchQuery] = useState('');
  const [pageJumpInput, setPageJumpInput] = useState('1');
  const [openToolbarMenu, setOpenToolbarMenu] = useState(null);
  const [scopeSaveError, setScopeSaveError] = useState('');
  const [scopeSaving, setScopeSaving] = useState(false);
  const [openPageSizeMenu, setOpenPageSizeMenu] = useState(null);
  const isTimelineMenuOpen = openToolbarMenu === 'timeline';
  const {
    isCustomCommitted: isCustomTimelineCommitted,
    isCustomSelected: isCustomTimelineSelected,
    openCustomRangeDraft,
    clearCustomRangeDraft,
  } = useTimelineCustomRangeDraft({
    isOpen: isTimelineMenuOpen,
    committedKey: timelineKey,
    customKey: CUSTOM_TIMELINE_KEY,
  });

  const institutionMenuRef = useRef(null);
  const typeMenuRef = useRef(null);
  const timelineMenuRef = useRef(null);
  const pageSizeMenuRef = useRef(null);
  const fetchRequestIdRef = useRef(0);
  const lastQuerySignatureRef = useRef('');
  const knownLeafIdsRef = useRef(null);
  const rowsFrameRef = useRef(null);
  const rowsContainerRef = useRef(null);
  const rowHoverPointerRef = useRef(null);
  const topPaginationRef = useRef(null);
  const bottomPaginationRef = useRef(null);
  const categoryToolbarSlot = typeof document === 'undefined' ? null : document.getElementById('transactions-toolbar-category-slot');
  const timelineToolbarSlot = typeof document === 'undefined' ? null : document.getElementById('transactions-toolbar-timeline-slot');
  const scopeToolbarSlot = typeof document === 'undefined' ? null : document.getElementById('transactions-toolbar-scope-slot');

  // The handoff's category + date halves are both seeded into page-local state
  // at init (category set above, date range as the transient timeline override),
  // so neither touches global/persisted prefs; here we just clear the nav state
  // once so a back/refresh doesn't re-apply it.
  const appliedHandoffRef = useRef(false);
  useEffect(() => {
    if (!handoffFilter || appliedHandoffRef.current) return;
    appliedHandoffRef.current = true;
    navigate(location.pathname, { replace: true, state: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handoffFilter, navigate]);

  const closeToolbarMenu = useCallback(() => {
    setOpenToolbarMenu((prev) => {
      if (prev === 'institution' && transactionsScopeDraftRef.current) {
        setLocalScopeInstitutions(cloneScopeInstitutions(transactionsScopeDraftRef.current));
        transactionsScopeDraftRef.current = null;
      }
      if (prev === 'timeline') {
        clearCustomRangeDraft();
      }
      return null;
    });
  }, [clearCustomRangeDraft]);
  const closePageSizeMenu = useCallback(() => setOpenPageSizeMenu(null), []);
  const activeToolbarMenuRefs = useMemo(() => {
    const activeMenuRef = {
      institution: institutionMenuRef,
      type: typeMenuRef,
      timeline: timelineMenuRef,
    }[openToolbarMenu];
    return activeMenuRef ? [activeMenuRef] : [];
  }, [openToolbarMenu]);
  const selectedTransactionId = detailDrawer?.transaction?.id == null
    ? ''
    : String(detailDrawer.transaction.id);

  const clearTransactionRowHighlight = useCallback(() => {
    if (!rowsFrameRef.current) return;
    delete rowsFrameRef.current.dataset.transactionsSelectedRowHighlight;
    delete rowsFrameRef.current.dataset.transactionsSelectedRowEdge;
    delete rowsFrameRef.current.dataset.transactionsHoverRowHighlight;
    delete rowsFrameRef.current.dataset.transactionsHoverRowEdge;
  }, []);

  const clearTransactionHoverHighlight = useCallback(() => {
    if (!rowsFrameRef.current) return;
    delete rowsFrameRef.current.dataset.transactionsHoverRowHighlight;
    delete rowsFrameRef.current.dataset.transactionsHoverRowEdge;
  }, []);

  const syncTransactionRowHighlight = useCallback((rowElement, mode) => {
    const frameElement = rowsFrameRef.current;
    if (!frameElement || !rowElement) return;

    const frameRect = frameElement.getBoundingClientRect();
    const rowRect = rowElement.getBoundingClientRect();
    const rowEdge = rowElement.matches('.transactions-date-group:last-child .transactions-row:last-child')
      ? 'last'
      : 'middle';
    const highlightState = mode === 'selected' ? 'selected' : 'hover';
    frameElement.style.setProperty(`--transactions-${highlightState}-row-highlight-top`, `${rowRect.top - frameRect.top}px`);
    frameElement.style.setProperty(`--transactions-${highlightState}-row-highlight-height`, `${rowRect.height}px`);

    if (highlightState === 'selected') {
      frameElement.dataset.transactionsSelectedRowHighlight = 'true';
      frameElement.dataset.transactionsSelectedRowEdge = rowEdge;
      return;
    }

    frameElement.dataset.transactionsHoverRowHighlight = 'true';
    frameElement.dataset.transactionsHoverRowEdge = rowEdge;
  }, []);

  const getSelectedTransactionRow = useCallback(() => (
    selectedTransactionId
      ? Array.from(rowsContainerRef.current?.querySelectorAll('.transactions-row') || [])
        .find((rowElement) => rowElement.dataset.transactionRowId === selectedTransactionId)
      : null
  ), [selectedTransactionId]);

  const getCurrentTransactionHoverRow = useCallback(() => {
    const rowsContainer = rowsContainerRef.current;
    if (!rowsContainer) return null;

    const pointer = rowHoverPointerRef.current;
    const pointTarget = pointer && typeof document !== 'undefined'
      ? document.elementFromPoint(pointer.x, pointer.y)
      : null;
    const rowAtPointer = pointTarget?.closest?.('.transactions-row');
    if (rowAtPointer && rowsContainer.contains(rowAtPointer)) {
      return rowAtPointer;
    }

    return rowsContainer.querySelector('.transactions-row:hover, .transactions-row:focus-visible');
  }, []);

  const syncSelectedTransactionRowHighlight = useCallback(() => {
    const selectedRow = getSelectedTransactionRow();
    if (!selectedRow) {
      clearTransactionRowHighlight();
      return;
    }

    syncTransactionRowHighlight(selectedRow, 'selected');
  }, [clearTransactionRowHighlight, getSelectedTransactionRow, syncTransactionRowHighlight]);

  const syncVisibleTransactionRowHighlights = useCallback(() => {
    if (selectedTransactionId) {
      syncSelectedTransactionRowHighlight();
    }

    const hoverRow = getCurrentTransactionHoverRow();
    if (hoverRow) {
      syncTransactionRowHighlight(hoverRow, 'hover');
      return;
    }

    clearTransactionHoverHighlight();
  }, [
    clearTransactionHoverHighlight,
    getCurrentTransactionHoverRow,
    selectedTransactionId,
    syncSelectedTransactionRowHighlight,
    syncTransactionRowHighlight,
  ]);

  const applyCategoryList = useCallback((list) => {
    setCategories(list);
    const currentLeafIds = list.filter((c) => c.parent_id !== null).map((c) => c.id);
    const currentSet = new Set(currentLeafIds);
    if (!categorySelectionInitialized) {
      setSelectedCategoryIds(new Set(currentLeafIds));
      setCategorySelectionInitialized(true);
    } else if (knownLeafIdsRef.current) {
      // On a refetch (e.g. after inline-creating a category), keep the user's
      // existing selection but auto-include any brand-new leaves and drop any
      // that were deleted — so a freshly created category is visible by
      // default and a just-tagged transaction doesn't fall outside the filter.
      const known = knownLeafIdsRef.current;
      setSelectedCategoryIds((prev) => {
        const next = new Set();
        for (const id of prev) if (currentSet.has(id)) next.add(id);
        for (const id of currentLeafIds) if (!known.has(id)) next.add(id);
        return next;
      });
    }
    knownLeafIdsRef.current = currentSet;
  }, [categorySelectionInitialized]);

  const fetchCategories = useCallback(async () => {
    try {
      const list = await loadTransactionCategories();
      if (list) applyCategoryList(list);
    } catch (err) {
      console.error('Failed to fetch categories:', err);
    }
  }, [applyCategoryList]);

  useEffect(() => {
    let cancelled = false;

    async function loadCategories() {
      try {
        const list = await loadTransactionCategories();
        if (!cancelled && list) applyCategoryList(list);
      } catch (err) {
        if (!cancelled) console.error('Failed to fetch categories:', err);
      }
    }

    void loadCategories();
    return () => {
      cancelled = true;
    };
  }, [applyCategoryList]);

  const categoriesById = useMemo(() => {
    const map = new Map();
    for (const cat of categories) map.set(cat.id, cat);
    return map;
  }, [categories]);

  const leafCategoryIds = useMemo(
    () => categories.filter((c) => c.parent_id !== null).map((c) => c.id),
    [categories]
  );

  const groupedCategories = useMemo(() => {
    const parents = categories
      .filter((c) => c.parent_id === null)
      .sort((a, b) => a.sort_order - b.sort_order);
    return parents.map((parent) => ({
      parent,
      children: categories
        .filter((c) => c.parent_id === parent.id)
        .sort((a, b) => a.sort_order - b.sort_order),
    }));
  }, [categories]);

  const allCategoriesSelected = leafCategoryIds.length > 0
    && selectedCategoryIds.size === leafCategoryIds.length;
  const noCategoriesSelected = selectedCategoryIds.size === 0;

  useDismissibleLayer({
    open: Boolean(openToolbarMenu),
    refs: activeToolbarMenuRefs,
    onDismiss: closeToolbarMenu,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

  useDismissibleLayer({
    open: Boolean(openPageSizeMenu),
    ref: pageSizeMenuRef,
    onDismiss: closePageSizeMenu,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

  useEffect(() => {
    if (transactionsScopeDraftRef.current) return;
    setLocalScopeInstitutions(cloneScopeInstitutions(allScopeInstitutions));
  }, [allScopeInstitutions]);

  const selectedScopeAccountIds = useMemo(
    () => localScopeInstitutions.flatMap((inst) => (
      inst.hidden
        ? []
        : inst.accounts.filter((account) => !account.hidden).map((account) => account.id)
    )),
    [localScopeInstitutions]
  );

  const selectedScopeAccountIdSet = useMemo(
    () => new Set(selectedScopeAccountIds),
    [selectedScopeAccountIds]
  );

  const selectedScopeInstitutionIds = useMemo(
    () => localScopeInstitutions
      .filter((inst) => !inst.hidden && inst.accounts.some((account) => !account.hidden))
      .map((inst) => inst.id),
    [localScopeInstitutions]
  );

  const appliedInstitutionIds = useMemo(
    () => (data?.institutions || []).map((inst) => inst.id),
    [data?.institutions]
  );

  const selectedInstitutionCount = selectedScopeInstitutionIds.length;
  const scopeTotalAccountCount = localScopeInstitutions.reduce(
    (total, inst) => total + inst.accounts.length,
    0
  );
  const allScopeSourcesSelected = scopeTotalAccountCount > 0
    && selectedScopeAccountIds.length === scopeTotalAccountCount;
  const hasHiddenInstitutions = localScopeInstitutions.some(
    (inst) => inst.hidden || inst.accounts.some((account) => account.hidden)
  );

  const buildParams = useCallback((offset, limit = pageSize) => {
    const params = new URLSearchParams();
    params.set('limit', String(limit));
    params.set('offset', String(offset));

    if (categorySelectionInitialized && leafCategoryIds.length > 0) {
      if (noCategoriesSelected) {
        params.set('category_ids', '-1');
      } else if (!allCategoriesSelected) {
        params.set('category_ids', [...selectedCategoryIds].join(','));
      }
    }

    if (handoffAccountIds?.length) {
      params.set('account_ids', handoffAccountIds.join(','));
    } else if (handoffInstitutionIds?.length) {
      params.set('institution_ids', handoffInstitutionIds.join(','));
    } else if (appliedInstitutionIds.length === 0) {
      params.set('institution_ids', '-1');
    }

    const { startDate, endDate } = getTimelineBounds(timelineKey, customDateRange);

    if (startDate) {
      params.set('start_date', startDate);
    }

    // `end_date` is an inclusive calendar day; the backend (services.date_window)
    // covers the whole day, so no client-side next-day adjustment is needed.
    if (endDate) {
      params.set('end_date', endDate);
    }

    if (handoffAssetCategory) {
      params.set('asset_category', handoffAssetCategory);
    }

    if (appliedSearchQuery) {
      params.set('search', appliedSearchQuery);
    }

    return params;
  }, [
    allCategoriesSelected,
    appliedInstitutionIds.length,
    categorySelectionInitialized,
    customDateRange,
    appliedSearchQuery,
    handoffAccountIds,
    handoffAssetCategory,
    handoffInstitutionIds,
    leafCategoryIds.length,
    noCategoriesSelected,
    pageSize,
    selectedCategoryIds,
    timelineKey,
  ]);

  const fetchTransactionsPage = useCallback(async ({ page = currentPage } = {}) => {
    const requestId = fetchRequestIdRef.current + 1;
    fetchRequestIdRef.current = requestId;
    const targetPage = Math.max(1, page);
    setLoading(true);
    setLoadError(null);

    try {
      const params = buildParams((targetPage - 1) * pageSize, pageSize);
      const resp = await fetch(`${API}/transactions?${params.toString()}`);
      const payload = await readTransactionCollectionResponse(resp, {
        label: 'Transactions',
      });

      if (fetchRequestIdRef.current !== requestId) return;
      setHasLoadedTransactions(true);
      const nextTotalPages = Math.max(1, Math.ceil(payload.total / pageSize));
      if (targetPage > nextTotalPages) {
        setTotalTransactions(payload.total);
        setCurrentPage(nextTotalPages);
        setPageJumpInput(String(nextTotalPages));
        return;
      }
      setTransactions(payload.transactions);
      setTotalTransactions(payload.total);
    } catch (err) {
      if (fetchRequestIdRef.current !== requestId) return;
      console.error('Failed to fetch transactions:', err);
      setLoadError(err.message || 'Transactions could not be loaded.');
    } finally {
      if (fetchRequestIdRef.current === requestId) {
        setLoading(false);
      }
    }
  }, [buildParams, currentPage, pageSize]);

  const querySignature = useMemo(() => {
    const params = buildParams(0, pageSize);
    params.delete('limit');
    params.delete('offset');
    return params.toString();
  }, [buildParams, pageSize]);

  useEffect(() => {
    const previousSignature = lastQuerySignatureRef.current;
    const signatureChanged = previousSignature && previousSignature !== querySignature;
    lastQuerySignatureRef.current = querySignature;

    if (signatureChanged && currentPage !== 1) {
      setCurrentPage(1);
      setPageJumpInput('1');
      return;
    }

    fetchTransactionsPage({ page: currentPage });
  }, [currentPage, dataRefreshId, fetchTransactionsPage, querySignature]);

  const totalPages = Math.max(1, Math.ceil(totalTransactions / pageSize));
  const safeCurrentPage = Math.min(currentPage, totalPages);
  const pageStartIndex = (safeCurrentPage - 1) * pageSize;
  const paginatedTransactions = transactions;
  const paginationItems = getPaginationItems(safeCurrentPage, totalPages);
  const pageStartItem = totalTransactions === 0 ? 0 : pageStartIndex + 1;
  const pageEndItem = totalTransactions === 0 ? 0 : pageStartIndex + paginatedTransactions.length;
  const normalizedSearchQueryDraft = normalizeTransactionSearchQuery(searchQueryDraft);
  const isSearchQueryDirty = normalizedSearchQueryDraft !== appliedSearchQuery;
  const canClearSearchQuery = Boolean(searchQueryDraft || appliedSearchQuery);

  const grouped = useMemo(() => {
    const groups = {};
    const order = [];

    for (const tx of paginatedTransactions) {
      const key = getDateKey(tx.date);
      if (!groups[key]) {
        groups[key] = { date: tx.date, transactions: [], total: 0 };
        order.push(key);
      }

      groups[key].transactions.push(tx);
      groups[key].total += tx.amount;
    }

    return order.map((key) => groups[key]);
  }, [paginatedTransactions]);

  useLayoutEffect(() => {
    syncSelectedTransactionRowHighlight();
  }, [grouped, syncSelectedTransactionRowHighlight, transactionDetailTray.reservationActive]);

  useEffect(() => {
    if (!selectedTransactionId || typeof window === 'undefined') return undefined;

    const frameId = window.requestAnimationFrame(() => {
      syncSelectedTransactionRowHighlight();
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [
    selectedTransactionId,
    syncSelectedTransactionRowHighlight,
    transactionDetailTray.animatedOpen,
    transactionDetailTray.reservationActive,
  ]);

  useEffect(() => {
    if (!selectedTransactionId || typeof window === 'undefined') return undefined;

    let frameId = null;
    const scheduleSync = () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      frameId = window.requestAnimationFrame(() => {
        frameId = null;
        syncSelectedTransactionRowHighlight();
      });
    };

    const selectedRow = getSelectedTransactionRow();
    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(scheduleSync);
    [rowsFrameRef.current, rowsContainerRef.current, selectedRow].filter(Boolean).forEach((element) => {
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
    getSelectedTransactionRow,
    grouped,
    selectedTransactionId,
    syncSelectedTransactionRowHighlight,
    transactionDetailTray.reservationActive,
  ]);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;

    let frameId = null;
    let pendingFrames = 0;
    const runScheduledSync = () => {
      frameId = null;
      syncVisibleTransactionRowHighlights();
      pendingFrames -= 1;
      if (pendingFrames > 0) {
        frameId = window.requestAnimationFrame(runScheduledSync);
      }
    };
    const scheduleSync = () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      pendingFrames = 4;
      frameId = window.requestAnimationFrame(runScheduledSync);
    };

    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(scheduleSync);
    [rowsFrameRef.current, rowsContainerRef.current].filter(Boolean).forEach((element) => {
      resizeObserver?.observe(element);
    });

    const unsubscribeZoom = subscribeMainWindowZoomStatus(scheduleSync);
    window.addEventListener('resize', scheduleSync);
    window.visualViewport?.addEventListener('resize', scheduleSync);

    return () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      resizeObserver?.disconnect();
      unsubscribeZoom();
      window.removeEventListener('resize', scheduleSync);
      window.visualViewport?.removeEventListener('resize', scheduleSync);
    };
  }, [grouped, syncVisibleTransactionRowHighlights]);

  const syncTransactionTrayClearance = useCallback(() => {
    if (typeof document === 'undefined' || typeof window === 'undefined') return;

    const mainElement = document.querySelector('.app-main');
    if (!mainElement) return;

    if (!selectedTransactionId) {
      mainElement.style.removeProperty('--transaction-detail-tray-top-clearance');
      mainElement.style.removeProperty('--transaction-detail-tray-bottom-clearance');
      setBottomPaginationSuppressed(bottomPaginationRef.current, false);
      return;
    }

    const viewportHeight = window.visualViewport?.height || window.innerHeight;
    let topClearance = 0;

    if (topPaginationRef.current) {
      const topPaginationRect = topPaginationRef.current.getBoundingClientRect();
      const topPagerVisible = topPaginationRect.bottom > 0 && topPaginationRect.top < viewportHeight;
      topClearance = topPagerVisible ? topPaginationRect.bottom + TRANSACTION_DETAIL_TRAY_PAGER_GAP : 0;
      mainElement.style.setProperty('--transaction-detail-tray-top-clearance', `${topClearance}px`);
    } else {
      mainElement.style.removeProperty('--transaction-detail-tray-top-clearance');
    }

    if (bottomPaginationRef.current) {
      const bottomPaginationRect = bottomPaginationRef.current.getBoundingClientRect();
      const bottomPagerVisible = bottomPaginationRect.bottom > 0 && bottomPaginationRect.top < viewportHeight;
      const protectedBottomClearance = bottomPagerVisible
        ? Math.max(
          TRANSACTION_DETAIL_TRAY_BOTTOM_FLOOR,
          viewportHeight - bottomPaginationRect.top + TRANSACTION_DETAIL_TRAY_PAGER_GAP
        )
        : TRANSACTION_DETAIL_TRAY_BOTTOM_FLOOR;
      const availableTrayHeight = viewportHeight - topClearance - protectedBottomClearance;
      const shouldSuppressBottomPagination = bottomPagerVisible
        && availableTrayHeight < TRANSACTION_DETAIL_TRAY_MIN_USABLE_HEIGHT;
      setBottomPaginationSuppressed(bottomPaginationRef.current, shouldSuppressBottomPagination);
      const bottomClearance = bottomPagerVisible && !shouldSuppressBottomPagination
        ? protectedBottomClearance
        : TRANSACTION_DETAIL_TRAY_BOTTOM_FLOOR;
      mainElement.style.setProperty('--transaction-detail-tray-bottom-clearance', `${bottomClearance}px`);
    } else {
      mainElement.style.removeProperty('--transaction-detail-tray-bottom-clearance');
      setBottomPaginationSuppressed(bottomPaginationRef.current, false);
    }
  }, [selectedTransactionId]);

  useLayoutEffect(() => {
    syncTransactionTrayClearance();
  }, [
    grouped,
    pageSize,
    safeCurrentPage,
    syncTransactionTrayClearance,
    totalPages,
    transactionDetailTray.reservationActive,
  ]);

  useEffect(() => {
    if (typeof document === 'undefined' || typeof window === 'undefined') return undefined;

    const mainElement = document.querySelector('.app-main');
    if (!selectedTransactionId || !mainElement) {
      mainElement?.style.removeProperty('--transaction-detail-tray-top-clearance');
      mainElement?.style.removeProperty('--transaction-detail-tray-bottom-clearance');
      return undefined;
    }

    let frameId = null;
    const scheduleSync = () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      frameId = window.requestAnimationFrame(() => {
        frameId = null;
        syncTransactionTrayClearance();
      });
    };

    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(scheduleSync);
    if (topPaginationRef.current) {
      resizeObserver?.observe(topPaginationRef.current);
    }
    if (bottomPaginationRef.current) {
      resizeObserver?.observe(bottomPaginationRef.current);
    }

    scheduleSync();
    window.addEventListener('resize', scheduleSync);
    window.visualViewport?.addEventListener('resize', scheduleSync);
    mainElement.addEventListener('scroll', scheduleSync);

    return () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      resizeObserver?.disconnect();
      window.removeEventListener('resize', scheduleSync);
      window.visualViewport?.removeEventListener('resize', scheduleSync);
      mainElement.removeEventListener('scroll', scheduleSync);
      mainElement.style.removeProperty('--transaction-detail-tray-top-clearance');
      mainElement.style.removeProperty('--transaction-detail-tray-bottom-clearance');
    };
  }, [selectedTransactionId, syncTransactionTrayClearance]);

  const handleToggleScopeInstitution = (institution) => {
    const accountIds = institution.accountIds;

    if (accountIds.length === 0) {
      return;
    }

    setLocalScopeInstitutions((previous) => previous.map((inst) => {
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

    setLocalScopeInstitutions((previous) => previous.map((inst) => {
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

  const toggleCategoryId = (categoryId) => {
    setSelectedCategoryIds((prev) => {
      const next = new Set(prev);
      if (next.has(categoryId)) next.delete(categoryId);
      else next.add(categoryId);
      return next;
    });
  };

  const toggleCategoryGroup = (parentId) => {
    const groupLeafIds = categories
      .filter((c) => c.parent_id === parentId)
      .map((c) => c.id);
    if (groupLeafIds.length === 0) return;
    setSelectedCategoryIds((prev) => {
      const next = new Set(prev);
      const allSelected = groupLeafIds.every((id) => next.has(id));
      if (allSelected) {
        for (const id of groupLeafIds) next.delete(id);
      } else {
        for (const id of groupLeafIds) next.add(id);
      }
      return next;
    });
  };

  const handleToggleAllCategories = () => {
    setSelectedCategoryIds((prev) => (
      prev.size === leafCategoryIds.length ? new Set() : new Set(leafCategoryIds)
    ));
  };

  const institutionFilterSummary = formatScopeSelectionSummary({
    totalInstitutions: localScopeInstitutions.length,
    selectedInstitutions: selectedInstitutionCount,
    selectedAccounts: selectedScopeAccountIds.length,
  });

  const categoryFilterSummary = useMemo(() => {
    if (!categorySelectionInitialized || leafCategoryIds.length === 0) return 'All categories';
    if (allCategoriesSelected) return 'All categories';
    if (noCategoriesSelected) return 'No categories';
    if (selectedCategoryIds.size === 1) {
      const onlyId = [...selectedCategoryIds][0];
      return categoriesById.get(onlyId)?.name || '1 selected';
    }
    return `${selectedCategoryIds.size} selected`;
  }, [
    allCategoriesSelected,
    categorySelectionInitialized,
    categoriesById,
    leafCategoryIds.length,
    noCategoriesSelected,
    selectedCategoryIds,
  ]);

  const categoryFilterTriggerIcon = noCategoriesSelected ? MdBlock : MdLabel;

  const timelineSummary = useMemo(
    () => getTimelineSummary(timelineKey, customDateRange),
    [customDateRange, timelineKey]
  );

  const handleToggleAllScopeSources = () => {
    setLocalScopeInstitutions((previous) => previous.map((inst) => ({
      ...inst,
      hidden: allScopeSourcesSelected,
      accounts: inst.accounts.map((account) => ({
        ...account,
        hidden: allScopeSourcesSelected,
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
        previousInstitutions: transactionsScopeDraftRef.current,
      });
      transactionsScopeDraftRef.current = null;
      setOpenToolbarMenu(null);
      setHandoffAccountIds(null);
      setHandoffInstitutionIds(null);
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
        setLocalScopeInstitutions(cloneScopeInstitutions(authoritativeInstitutions));
        transactionsScopeDraftRef.current = cloneScopeInstitutions(authoritativeInstitutions);
      }
    } finally {
      setScopeSaving(false);
    }
  };

  const handleToggleToolbarMenu = (menuName) => {
    setOpenToolbarMenu((prev) => {
      const next = prev === menuName ? null : menuName;
      if (next === 'institution') {
        transactionsScopeDraftRef.current = cloneScopeInstitutions(localScopeInstitutions);
      } else if (prev === 'institution' && transactionsScopeDraftRef.current) {
        setLocalScopeInstitutions(cloneScopeInstitutions(transactionsScopeDraftRef.current));
        transactionsScopeDraftRef.current = null;
      }
      if (prev === 'timeline' && next !== 'timeline') {
        clearCustomRangeDraft();
      }
      return next;
    });
  };

  const handleSelectTimeline = (nextTimelineKey) => {
    if (nextTimelineKey === CUSTOM_TIMELINE_KEY) {
      openCustomRangeDraft();
      return;
    }

    setCustomDateRange({ start: '', end: '' });
    clearCustomRangeDraft();
    setTimelineKey(nextTimelineKey);
    setOpenToolbarMenu(null);
  };

  const handleApplyCustomDateRange = (nextRange) => {
    setCustomDateRange(nextRange);
    setTimelineKey(CUSTOM_TIMELINE_KEY);
    clearCustomRangeDraft();
    setOpenToolbarMenu(null);
  };

  const handleCancelCustomDateRange = () => {
    clearCustomRangeDraft();
    setOpenToolbarMenu(null);
  };

  const handleSelectPageSize = (nextPageSize) => {
    setPageSize(nextPageSize);
    setCurrentPage(1);
    setPageJumpInput('1');
    setOpenPageSizeMenu(null);
  };

  const commitPage = useCallback((nextPage) => {
    const clampedPage = clampPageNumber(nextPage, totalPages);
    setCurrentPage(clampedPage);
    setPageJumpInput(String(clampedPage));
  }, [totalPages]);

  const handlePageJumpSubmit = useCallback((event) => {
    event.preventDefault();

    const parsedPage = Number.parseInt(pageJumpInput, 10);
    const nextPage = clampPageNumber(Number.isNaN(parsedPage) ? safeCurrentPage : parsedPage, totalPages);

    commitPage(nextPage);
  }, [commitPage, pageJumpInput, safeCurrentPage, totalPages]);

  const handleSearchSubmit = useCallback((event) => {
    event.preventDefault();
    if (normalizedSearchQueryDraft === appliedSearchQuery) return;
    setAppliedSearchQuery(normalizedSearchQueryDraft);
  }, [appliedSearchQuery, normalizedSearchQueryDraft]);

  const handleClearSearch = useCallback(() => {
    setSearchQueryDraft('');
    if (appliedSearchQuery) {
      setAppliedSearchQuery('');
    }
  }, [appliedSearchQuery]);

  const renderPaginationRow = (placement) => {
    const isPageSizeMenuOpen = openPageSizeMenu === placement;

    return (
      <div
        className={`transactions-pagination-row transactions-pagination-row-${placement} app-surface-button-scope`.trim()}
        ref={placement === 'top' ? topPaginationRef : bottomPaginationRef}
      >
        <div className="transactions-pagination-summary">
          {`Page ${safeCurrentPage} of ${totalPages} · ${pageStartItem}-${pageEndItem} of ${totalTransactions}`}
        </div>

        <div className="transactions-pagination-controls">
          <form className="transactions-pagination-jump" onSubmit={handlePageJumpSubmit}>
            <label className="transactions-pagination-jump-label" htmlFor={`transactions-page-jump-${placement}`}>
              Page
            </label>
            <input
              id={`transactions-page-jump-${placement}`}
              type="number"
              inputMode="numeric"
              min="1"
              max={totalPages}
              step="1"
              className="transactions-pagination-jump-input"
              value={pageJumpInput}
              onChange={(event) => {
                const nextValue = event.target.value.replace(/[^\d]/g, '');
                setPageJumpInput(nextValue);
              }}
              onBlur={() => {
                if (!pageJumpInput) {
                  setPageJumpInput(String(safeCurrentPage));
                }
              }}
              aria-label={`Jump to page (${totalPages} total pages)`}
            />
            <button
              type="submit"
              className="transactions-pagination-button transactions-pagination-jump-submit app-control-root"
              disabled={totalTransactions === 0}
              aria-label="Go to page"
              data-tooltip="Go to page"
            >
              <span className="app-control-icon">
                <MdArrowForward className="transactions-pagination-icon" aria-hidden="true" />
              </span>
            </button>
          </form>

          <button
            type="button"
            className="transactions-pagination-button app-control-root"
            onClick={() => commitPage(safeCurrentPage - 1)}
            disabled={safeCurrentPage === 1}
          >
            <span className="app-control-label">Previous</span>
          </button>

          {paginationItems.map((item, index) => (
            item === 'ellipsis' ? (
              <span key={`ellipsis-${placement}-${index}`} className="transactions-pagination-ellipsis" aria-hidden="true">
                …
              </span>
            ) : (
              <button
                key={`${placement}-${item}`}
                type="button"
                className={`transactions-pagination-button transactions-pagination-number app-control-root ${safeCurrentPage === item ? 'is-active' : ''}`.trim()}
                aria-current={safeCurrentPage === item ? 'page' : undefined}
                onClick={() => commitPage(item)}
              >
                <span className="app-control-label">{item}</span>
              </button>
            )
          ))}

          <button
            type="button"
            className="transactions-pagination-button app-control-root"
            onClick={() => commitPage(safeCurrentPage + 1)}
            disabled={safeCurrentPage === totalPages}
          >
            <span className="app-control-label">Next</span>
          </button>
        </div>

        <div className="transactions-pagination-utilities">
          {placement === 'top' && (
            <form className="transactions-pagination-search" onSubmit={handleSearchSubmit}>
              <div className="transactions-search-field">
                <input
                  type="text"
                  className="transactions-filter-search"
                  placeholder="Search transactions"
                  value={searchQueryDraft}
                  onChange={(event) => setSearchQueryDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Escape') {
                      handleClearSearch();
                    }
                  }}
                  aria-label="Search transactions"
                />
                <button
                  type="button"
                  className={`transactions-search-inline-clear ${canClearSearchQuery ? '' : 'is-hidden'}`.trim()}
                  onClick={handleClearSearch}
                  disabled={!canClearSearchQuery}
                  tabIndex={canClearSearchQuery ? 0 : -1}
                  aria-hidden={!canClearSearchQuery}
                  aria-label="Clear transaction search"
                >
                  <MdClose aria-hidden="true" />
                </button>
              </div>
              <button
                type="submit"
                className="transactions-pagination-button transactions-search-action app-control-root"
                disabled={!isSearchQueryDirty}
                aria-label="Search transactions"
                data-tooltip="Search transactions"
              >
                <span className="app-control-icon">
                  <MdSearch className="transactions-pagination-icon" aria-hidden="true" />
                </span>
              </button>
            </form>
          )}

          <div
            className="transactions-page-size-popover"
            ref={isPageSizeMenuOpen ? pageSizeMenuRef : null}
          >
            <button
              type="button"
              className={`transactions-page-size-trigger app-control-root ${isPageSizeMenuOpen ? 'is-open' : ''}`.trim()}
              aria-haspopup="listbox"
              aria-expanded={isPageSizeMenuOpen}
              onClick={() => setOpenPageSizeMenu((prev) => (prev === placement ? null : placement))}
            >
              <span className="transactions-page-size-value app-control-label">{pageSize}</span>
              <span className={`transactions-page-size-trigger-chevron app-control-chevron ${isPageSizeMenuOpen ? 'is-open' : ''}`.trim()} aria-hidden="true">
                <ControlChevron />
              </span>
            </button>

            <div
              className={`transactions-page-size-menu ${isPageSizeMenuOpen ? 'is-open' : ''}`.trim()}
              role="listbox"
              aria-hidden={!isPageSizeMenuOpen}
            >
              {PAGE_SIZE_OPTIONS.map((option) => (
                <button
                  key={`${placement}-size-${option}`}
                  type="button"
                  className={`transactions-page-size-option app-control-root ${pageSize === option ? 'is-selected' : ''}`.trim()}
                  role="option"
                  aria-selected={pageSize === option}
                  onClick={() => handleSelectPageSize(option)}
                >
                  <span className="app-control-label">{option}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  };

  const isInstitutionMenuOpen = openToolbarMenu === 'institution';
  const isTypeMenuOpen = openToolbarMenu === 'type';
  const isCustomTimelinePickerVisible = isTimelineMenuOpen && isCustomTimelineSelected;

  const categoryFilterControl = (
      <div className="investments-filter-popover transactions-toolbar-popover transactions-toolbar-popover-type" ref={typeMenuRef}>
        <CategoryFilterTrigger
          isOpen={isTypeMenuOpen}
          summary={categoryFilterSummary}
          icon={categoryFilterTriggerIcon}
          controls="transactions-category-filter-panel"
          onClick={() => handleToggleToolbarMenu('type')}
        />

        <div
          id="transactions-category-filter-panel"
          role="dialog"
          aria-label="Category filters"
          className={`investments-filter-panel transactions-toolbar-panel transactions-toolbar-panel-type ${isTypeMenuOpen ? 'is-open' : ''}`.trim()}
          aria-hidden={!isTypeMenuOpen}
        >
          <div className="transactions-toolbar-menu-list" role="menu" aria-label="Categories">
            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={allCategoriesSelected}
              className={`transactions-toolbar-menu-item ${allCategoriesSelected ? 'is-selected' : ''}`.trim()}
              onClick={handleToggleAllCategories}
            >
              <span className="transactions-toolbar-menu-item-copy">
                <span className="transactions-toolbar-menu-item-label">All categories</span>
              </span>
              <span className={`transactions-toolbar-checkbox ${allCategoriesSelected ? 'is-selected' : ''}`.trim()} aria-hidden="true">
                {allCategoriesSelected ? <MdCheck size={14} /> : null}
              </span>
            </button>

            {groupedCategories.map((group) => {
              const groupLeafIds = group.children.map((c) => c.id);
              const groupSelectedCount = groupLeafIds.filter((id) => selectedCategoryIds.has(id)).length;
              const allInGroupSelected = groupLeafIds.length > 0 && groupSelectedCount === groupLeafIds.length;
              return (
                <React.Fragment key={group.parent.id}>
                  <button
                    type="button"
                    role="menuitemcheckbox"
                    aria-checked={allInGroupSelected}
                    className={`transactions-toolbar-menu-item transactions-toolbar-menu-group-header ${allInGroupSelected ? 'is-selected' : ''}`.trim()}
                    onClick={() => toggleCategoryGroup(group.parent.id)}
                    style={getCategoryThemeVars(group.parent, mode)}
                  >
                    <span className="transactions-toolbar-menu-item-copy">
                      <TwemojiIcon emoji={group.parent.icon} set={group.parent.icon_set} size={14} />
                      <span className="transactions-toolbar-menu-item-label">{group.parent.name}</span>
                      <span className="transactions-toolbar-menu-item-count">
                        {groupSelectedCount}/{groupLeafIds.length}
                      </span>
                    </span>
                    <span className={`transactions-toolbar-checkbox ${allInGroupSelected ? 'is-selected' : ''}`.trim()} aria-hidden="true">
                      {allInGroupSelected ? <MdCheck size={14} /> : null}
                    </span>
                  </button>
                  {group.children.map((cat) => {
                    const isSelected = selectedCategoryIds.has(cat.id);
                    return (
                      <button
                        key={cat.id}
                        type="button"
                        role="menuitemcheckbox"
                        aria-checked={isSelected}
                        className={`transactions-toolbar-menu-item transactions-toolbar-menu-leaf ${isSelected ? 'is-selected' : ''}`.trim()}
                        onClick={() => toggleCategoryId(cat.id)}
                        style={getCategoryThemeVars(cat, mode)}
                      >
                        <span className="transactions-toolbar-menu-item-copy">
                          <TwemojiIcon emoji={cat.icon} set={cat.icon_set} size={14} />
                          <span className="transactions-toolbar-menu-item-label">{cat.name}</span>
                        </span>
                        <span className={`transactions-toolbar-checkbox ${isSelected ? 'is-selected' : ''}`.trim()} aria-hidden="true">
                          {isSelected ? <MdCheck size={14} /> : null}
                        </span>
                      </button>
                    );
                  })}
                </React.Fragment>
              );
            })}
          </div>
        </div>
      </div>
  );

  const timelineFilterControl = (
      <div className="investments-filter-popover transactions-toolbar-popover transactions-toolbar-popover-timeline" ref={timelineMenuRef}>
        <TimelineTrigger
          isOpen={isTimelineMenuOpen}
          summary={timelineSummary}
          controls="transactions-timeline-filter-panel"
          className="transactions-timeline-trigger"
          ariaLabel="Transactions timeline"
          onClick={() => handleToggleToolbarMenu('timeline')}
        />

        <div
          id="transactions-timeline-filter-panel"
          role="dialog"
          aria-label="Timeline filters"
          className={`investments-filter-panel transactions-toolbar-panel transactions-toolbar-panel-timeline timeline-range-panel ${isTimelineMenuOpen ? 'is-open' : ''}`.trim()}
          aria-hidden={!isTimelineMenuOpen}
        >
          <div className="transactions-toolbar-menu-list" role="menu" aria-label="Timeline ranges">
            {DATE_PRESETS.map((preset) => {
              const isSelected = timelineKey === preset.key;
              return (
                <button
                  key={preset.key}
                  type="button"
                  role="menuitemradio"
                  aria-checked={isSelected}
                  className={`transactions-toolbar-menu-item ${isSelected ? 'is-selected' : ''}`.trim()}
                  onClick={() => handleSelectTimeline(preset.key)}
                >
                  <span className="transactions-toolbar-menu-item-copy">
                    <span className="transactions-toolbar-menu-item-label">{preset.label}</span>
                  </span>
                  <span className={`transactions-toolbar-checkbox ${isSelected ? 'is-selected' : ''}`.trim()} aria-hidden="true">
                    {isSelected ? <MdCheck size={14} /> : null}
                  </span>
                </button>
              );
            })}

            <button
              type="button"
              role="menuitemradio"
              aria-checked={isCustomTimelineCommitted}
              className={`transactions-toolbar-menu-item ${isCustomTimelineSelected ? 'is-selected' : ''}`.trim()}
              onClick={() => handleSelectTimeline(CUSTOM_TIMELINE_KEY)}
            >
              <span className="transactions-toolbar-menu-item-copy">
                <span className="transactions-toolbar-menu-item-label">Custom Range</span>
              </span>
              <span className={`transactions-toolbar-checkbox ${isCustomTimelineCommitted ? 'is-selected' : ''}`.trim()} aria-hidden="true">
                {isCustomTimelineCommitted ? <MdCheck size={14} /> : null}
              </span>
            </button>
          </div>

          {isCustomTimelinePickerVisible ? (
            <div
              className="timeline-range-picker-popover is-open"
              aria-label="Custom transaction timeline range"
            >
              <TimelineCustomRangePicker
                startDate={timelineKey === CUSTOM_TIMELINE_KEY ? customDateRange.start : ''}
                endDate={timelineKey === CUSTOM_TIMELINE_KEY ? customDateRange.end : ''}
                onApply={handleApplyCustomDateRange}
                onCancel={handleCancelCustomDateRange}
              />
            </div>
          ) : null}
        </div>
      </div>
  );

  const institutionFilterControl = (
      <div className="investments-filter-popover transactions-toolbar-popover transactions-toolbar-popover-institutions" ref={institutionMenuRef}>
        <ScopeSelectorTrigger
          isOpen={isInstitutionMenuOpen}
          summary={institutionFilterSummary}
          controls="transactions-institution-filter-panel"
          className={hasHiddenInstitutions ? 'has-hidden-sources' : ''}
          onClick={() => handleToggleToolbarMenu('institution')}
        />

        <div
          id="transactions-institution-filter-panel"
          role="dialog"
          aria-label="Scope filters"
          className={`investments-filter-panel scope-selector-panel transactions-toolbar-panel ${isInstitutionMenuOpen ? 'is-open' : ''}`.trim()}
          aria-hidden={!isInstitutionMenuOpen}
        >
          <InstitutionAccountSelector
            key={isInstitutionMenuOpen ? 'open' : 'closed'}
            institutions={localScopeInstitutions}
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

  const transactionsExportControl = (
      <CsvExportButton
        dataset="transactions"
        getParams={() => {
          const params = buildParams(0);
          params.delete('limit');
          params.delete('offset');
          return params;
        }}
      />
  );

  // Only blank the list with the spinner before the first successful response. On refetches
  // (filters, scope, refresh) keep the existing rows visible and update them in place — otherwise
  // the list flashes placeholder↔content on every refetch (very visible when responses are instant).
  const transactionsViewState = getRemoteDataViewState({
    hasData: hasLoadedTransactions,
    loading,
    error: loadError,
  });
  const showInitialLoading = transactionsViewState === 'loading';
  const showInitialError = transactionsViewState === 'error';

  return (
    <div className="page-frame transactions-page">
      {categoryToolbarSlot && createPortal(categoryFilterControl, categoryToolbarSlot)}
      {timelineToolbarSlot && createPortal(timelineFilterControl, timelineToolbarSlot)}
      {scopeToolbarSlot && createPortal(institutionFilterControl, scopeToolbarSlot)}
      {transactionsExportControl}

      <AppStatusNotice
        title="Scope update failed"
        message={scopeSaveError}
        onDismiss={() => setScopeSaveError('')}
      />
      <AppStatusNotice
        title={hasLoadedTransactions ? 'Transactions could not be refreshed' : 'Transactions could not be loaded'}
        message={loadError}
        actionLabel="Retry"
        onAction={() => fetchTransactionsPage({ page: currentPage })}
        actionDisabled={loading}
        onDismiss={hasLoadedTransactions ? () => setLoadError(null) : undefined}
      />

      {!showInitialLoading && !showInitialError && renderPaginationRow('top')}

      {showInitialLoading ? (
        <div className="panel-shell transactions-loading">
          <div className="transactions-loading-spinner" />
          <span>Loading transactions...</span>
        </div>
      ) : showInitialError ? (
        null
      ) : totalTransactions === 0 ? (
        <div className="panel-shell transactions-empty">
          <span className="transactions-empty-icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
              <polyline points="10 9 9 9 8 9" />
            </svg>
          </span>
          <span className="transactions-empty-text">No transactions found</span>
          <span className="transactions-empty-hint">Try adjusting your filters or search</span>
        </div>
      ) : (
        <>
          <div className="transactions-list-frame" ref={rowsFrameRef}>
            <div className="transactions-list-scroll-clip">
              <div className="panel-shell transactions-list" ref={rowsContainerRef} data-transactions-horizontal-scroll-sync>
                <div className="transactions-table-header-strip" role="row">
                  <span className="transactions-table-header-account">Account</span>
                  <span className="transactions-table-header-description">Description</span>
                  <span className="transactions-table-header-type">Category</span>
                  <span className="transactions-table-header-amount">Amount</span>
                </div>
                <TransactionsHorizontalScrollProxy className="transactions-table-top-scrollbar" />
                <div className="transactions-table-scroll-sizer" aria-hidden="true" />
                <div className="transactions-table-scroll-surface">
                  {grouped.map((group, groupIndex) => (
                    <div key={groupIndex} className="transactions-date-group">
                      <div className="transactions-date-header">
                        <span className="transactions-date-label">{formatGroupDate(group.date)}</span>
                      </div>

                      <div className="transactions-date-rows">
                        {group.transactions.map((tx) => {
                          const isActive = isSameTransactionDetail(detailDrawer, tx);
                          return (
                            <div
                              key={tx.id}
                              className={`transactions-row ${isActive ? 'is-active' : ''}`.trim()}
                              data-transaction-row-id={String(tx.id)}
                              role="button"
                              tabIndex={0}
                              onMouseEnter={(event) => {
                                rowHoverPointerRef.current = { x: event.clientX, y: event.clientY };
                                syncTransactionRowHighlight(event.currentTarget, 'hover');
                              }}
                              onMouseMove={(event) => {
                                rowHoverPointerRef.current = { x: event.clientX, y: event.clientY };
                              }}
                              onMouseLeave={() => {
                                rowHoverPointerRef.current = null;
                                clearTransactionHoverHighlight();
                              }}
                              onFocus={(event) => {
                                syncTransactionRowHighlight(event.currentTarget, 'hover');
                              }}
                              onBlur={clearTransactionHoverHighlight}
                              onClick={() => {
                                handleTransactionDetailSelect(tx);
                                if (isActive && !transactionTourTrayLocked) {
                                  clearTransactionRowHighlight();
                                  return;
                                }
                                clearTransactionHoverHighlight();
                              }}
                              onKeyDown={(event) => {
                                if (event.key === 'Enter' || event.key === ' ') {
                                  event.preventDefault();
                                  handleTransactionDetailSelect(tx);
                                  if (isActive && !transactionTourTrayLocked) {
                                    clearTransactionRowHighlight();
                                    return;
                                  }
                                  clearTransactionHoverHighlight();
                                }
                              }}
                            >
                      {/* Col 1: Institution logo */}
                      <div className="transactions-row-logo">
                        <InstitutionLogo name={tx.institution_name} size={32} />
                      </div>

                      {/* Col 2: Account name */}
                      <div className="transactions-row-account-name">
                        <span className="transactions-account-name">{tx.account_name}</span>
                      </div>

                      {/* Col 3: Account type */}
                      <div className="transactions-row-account-type">
                        {tx.account_type && (
                          <AccountTypeBadge
                            accountType={tx.account_type}
                            className="transactions-account-type-badge"
                          />
                        )}
                      </div>

                      {/* Col 3: Transaction description */}
                      <div className="transactions-row-main">
                        {(() => {
                          const isCurrencySymbol = tx.symbol && isCurrencyTransactionSymbol(tx.symbol);
                          const isBrokerage = tx.symbol && !isCurrencySymbol;
                          const mainTitle = getTransactionPrimaryDescription(tx, '');

                          // For brokerage rows, surface the description as extra detail *only*
                          // when it carries info beyond the connector's auto-generated
                          // "Bought 0.123 SOL" / "Sold 100 USDC" pattern. That way a real Buy
                          // stays clean (Qty + price tells the story) but a row like
                          // `2023 gasless USDC send` becomes distinguishable from the Buy row
                          // sitting next to it with the same symbol + similar quantity.
                          const isAutoGeneratedTrade = (
                            tx.description
                            && /^(Bought|Sold)\s+[\d.]+\s+/.test(tx.description)
                          );

                          return (
                            <>
                              <span className="transactions-description">{mainTitle}</span>
                              {isBrokerage && (tx.quantity != null || tx.price != null) && (
                                <span className="transactions-brokerage-detail">
                                  {tx.quantity != null && `Qty: ${tx.quantity}`}
                                  {tx.quantity != null && tx.price != null && ' '}
                                  {tx.price != null && `@ ${currencySymbolFor(tx.currency)}${Number(tx.price).toLocaleString('en-CA', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`}
                                </span>
                              )}
                              {isBrokerage && tx.description && !isAutoGeneratedTrade && (
                                <span className="transactions-sub-description">{tx.description}</span>
                              )}
                              {!isBrokerage && tx.symbol && tx.description && tx.description !== mainTitle && (
                                <span className="transactions-sub-description">{tx.description}</span>
                              )}
                            </>
                          );
                        })()}
                      </div>

                      {/* Col 4: Category pill (display-only — edit via row drawer) */}
                      <div className="transactions-row-type">
                        <CategoryPill
                          category={tx.category}
                          source={tx.category_source}
                        />
                      </div>

                      {/* Col 5: Amount + currency */}
                      <div className="transactions-row-amount">
                        <span className={`transactions-amount ${
                          tx.category?.classification === 'transfer'
                            ? 'is-transfer'
                            : tx.amount > 0
                              ? 'is-positive'
                              : tx.amount < 0
                                ? 'is-negative'
                                : ''
                        }`}>
                          <FitMoney full={formatAmount(tx.amount, tx.currency)} compact={formatSignedCompactMoney(tx.amount, tx.currency)} />
                        </span>
                        <span className={`transactions-currency-inline ${
                          tx.category?.classification === 'transfer'
                            ? 'is-transfer'
                            : tx.amount > 0
                              ? 'is-positive'
                              : tx.amount < 0
                                ? 'is-negative'
                                : ''
                        }`}>{tx.currency}</span>
                      </div>

                          </div>
                          );
                        })}
                      </div>
                    </div>
                  ))}
                </div>
                <TransactionsHorizontalScrollProxy className="transactions-table-bottom-scrollbar" />
              </div>
            </div>
          </div>
        </>
      )}

      {!showInitialLoading && !showInitialError && totalTransactions > 0 && renderPaginationRow('bottom')}

      <TransactionDetailDrawer
        transaction={detailDrawer?.transaction || null}
        animatedOpen={transactionDetailTray.animatedOpen}
        categories={categories}
        excludeFromDismissRef={rowsContainerRef}
        onClose={() => setDetailDrawer(null)}
        dismissLocked={transactionTourTrayLocked}
        onCategoriesChanged={fetchCategories}
        onSaved={() => {
          fetchTransactionsPage({ page: safeCurrentPage });
          // Cash edits/deletes change account balances — refresh app data so the
          // Accounts Cash panel, net worth, and allocation stay in sync.
          if (onDataChange) onDataChange();
        }}
      />
    </div>
  );
}

export default Transactions;
