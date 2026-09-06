import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate, useLocation } from 'react-router-dom';
import EChart from '../components/charts/EChart';
import StablePieTooltipEChart from '../components/charts/StablePieTooltipEChart';
import TriangleIcon from '../components/TriangleIcon';
import { API } from '../config';
import { getAppliedBreakTwentyChartText } from '../theme/applyTheme';
import CategoryPill from '../components/CategoryPill';
import { TOUR_DEMO_CASH_FLOW_ANCHOR_DATE, getTourHint, isTourDemoActive } from '../components/tourDemoData';
import InstitutionLogo from '../components/InstitutionLogo';
import AppStatusNotice from '../components/AppStatusNotice';
import InstitutionAccountSelector, { ScopeSelectorTrigger, formatScopeSelectionSummary } from '../components/InstitutionAccountSelector';
import CashFlowTimelineControl, {
  computeRange as computeCashFlowRange,
  canStep as canStepCashFlow,
  stepAnchor as stepCashFlowAnchor,
} from '../components/CashFlowTimelineControl';
import { formatCompactMoney } from '../utils/format';
import {
  CASH_FLOW_DONUT_HEIGHT,
  formatMoney,
  rollupByParent,
  buildCfDonutOption,
} from '../utils/cashFlowChart';
import { escapeHtml } from '../utils/html';
import Money from '../components/Money';
import FitMoney from '../components/FitMoney';
import { formatLongDateValue } from '../utils/date';
import { getAppNow } from '../utils/appClock';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import useBalancesHidden from '../hooks/useBalancesHidden';
import {
  isPlainObject,
  readCashFlowResponse,
  readJsonResponse,
  readTransactionCollectionResponse,
} from '../utils/apiResponse';
import {
  persistVisibilityScope,
  reconcileVisibilityScope,
  scopeWithSelectedAccounts,
  selectedAccountIdsFromScope,
} from '../utils/visibilityScope';
import { getRemoteDataViewState } from '../utils/remoteDataState';
import { resolveCategoryAccentColor } from '../utils/categoryColors';
import {
  getTransactionAccountTooltip,
  getTransactionPrimaryDescription,
  getTransactionQuantityLabel,
} from '../utils/transactionDescription';
import { loadTransactionCategories } from '../utils/transactionCategories';
import { useRightTrayOpenState, useCurrency, usePersistentPanelCollapsed, useTheme } from '../appState';
import { SideDetailDrawerPanel } from '../components/SideDetailDrawer';
import TransactionDetailDrawer from '../components/TransactionDetailDrawer';
import RecentTransactionsTable from '../components/RecentTransactionsTable';
import HorizontalScrollProxy from '../components/HorizontalScrollProxy';
import './CashFlow.css';


const CADENCE_LABELS = {
  weekly: 'Weekly',
  biweekly: 'Biweekly',
  semimonthly: 'Semi-monthly',
  monthly: 'Monthly',
  quarterly: 'Quarterly',
  annual: 'Annual',
  irregular: 'Irregular',
};
const CATEGORY_BREAKDOWN_HORIZONTAL_SCROLL_PROXY_OPTIONS = {
  className: 'cf-breakdown-top-scrollbar app-horizontal-scroll-proxy',
  innerClassName: 'cf-breakdown-top-scrollbar-inner app-horizontal-scroll-proxy-inner',
  contentWidthProperty: '--app-horizontal-scroll-content-width',
  targetViewportProperty: '--app-horizontal-scroll-viewport-width',
  resetOnTargetWidthChange: true,
  clampTolerance: 0,
  resolveTarget: (controller) => (
    controller.closest('.cf-breakdown-scroll-frame')?.querySelector('.cf-breakdown-scroll-viewport')
  ),
  getContentElements: ({ target }) => [target.querySelector('.cf-breakdown-scroll-surface')],
  getObservedElements: ({ target, contentElements }) => [target, ...contentElements],
};

function CategoryBreakdownHorizontalScrollProxy() {
  return <HorizontalScrollProxy options={CATEGORY_BREAKDOWN_HORIZONTAL_SCROLL_PROXY_OPTIONS} />;
}
function getTourCashFlowAnchor() {
  const [year, month, day] = TOUR_DEMO_CASH_FLOW_ANCHOR_DATE.split('-').map(Number);
  return new Date(year, month - 1, day);
}

function getCashFlowDetailSourceKey(kind, row) {
  if (!kind || !row) return null;
  const rowKey = row.category_id ?? row.seed_key;
  return rowKey == null ? null : `${kind}:${rowKey}`;
}

function isSameCashFlowDetail(current, kind, row) {
  const currentKey = getCashFlowDetailSourceKey(current?.kind, current?.row);
  const nextKey = getCashFlowDetailSourceKey(kind, row);
  return currentKey != null && currentKey === nextKey;
}

function formatShortDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString('en-CA', { month: 'short', day: 'numeric' });
}

function cfTrendTooltipHtml(params, currency, balancesHidden) {
  if (!Array.isArray(params) || params.length === 0) return '';
  const rows = params.map((entry) => (
    '<div class="tooltip-row">'
    + `<span class="tooltip-dot" style="background:${entry.color}"></span>`
    + `<span class="tooltip-name">${escapeHtml(entry.seriesName)}</span>`
    + '<span class="tooltip-value">'
    + `<span class="tooltip-money">${escapeHtml(formatMoney(Number(entry.value || 0), currency, balancesHidden))}</span></span></div>`
  )).join('');
  return `<div class="tooltip-card"><div class="tooltip-heading">${escapeHtml(params[0].axisValue)}</div>`
    + `<div class="tooltip-breakdown">${rows}</div></div>`;
}

function buildCfTrendOption(trend, { currency, balancesHidden, colors, chartColors }) {
  const breaktwentyChartText = getAppliedBreakTwentyChartText();
  return {
    grid: { top: 28, right: 20, bottom: 44, left: 40 },
    legend: {
      bottom: 0,
      data: ['Income', 'Expense', 'Net'],
      textStyle: { color: chartColors.label, ...breaktwentyChartText.axisLabel },
      icon: 'roundRect',
    },
    tooltip: {
      trigger: 'axis',
      appendToBody: true,
      axisPointer: { type: 'shadow', shadowStyle: { color: chartColors.cursorFill } },
      backgroundColor: 'transparent',
      borderWidth: 0,
      padding: 0,
      extraCssText: 'box-shadow:none;',
      formatter: (params) => cfTrendTooltipHtml(params, currency, balancesHidden),
    },
    xAxis: {
      type: 'category',
      data: trend.map((point) => point.month),
      axisLabel: { color: chartColors.tick, ...breaktwentyChartText.denseTick },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: chartColors.grid } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: chartColors.tick, ...breaktwentyChartText.denseTick, formatter: (value) => `${(value / 1000).toFixed(0)}k` },
      splitLine: { lineStyle: { color: chartColors.grid } },
    },
    series: [
      { name: 'Income', type: 'bar', data: trend.map((point) => point.income), itemStyle: { color: colors.positive } },
      { name: 'Expense', type: 'bar', data: trend.map((point) => point.expense), itemStyle: { color: colors.negative } },
      { name: 'Net', type: 'bar', data: trend.map((point) => point.net), itemStyle: { color: chartColors.net } },
    ],
  };
}

function CashFlowNetDonut({ totals, currency, balancesHidden }) {
  const { colors, chartColors } = useTheme();
  const option = useMemo(
    () => buildCfDonutOption(
      [
        { name: 'Income', value: Math.max(totals.income, 0.01), color: colors.positive },
        { name: 'Expense', value: Math.max(totals.expense, 0.01), color: colors.negative },
      ],
      {
        total: Math.max(totals.income, 0) + Math.max(totals.expense, 0),
        currency,
        balancesHidden,
        startAngle: 90,
        chartColors,
      },
    ),
    [balancesHidden, chartColors, colors, currency, totals],
  );
  return <StablePieTooltipEChart option={option} height={CASH_FLOW_DONUT_HEIGHT} />;
}

function CashFlow({
  allScopeInstitutions = [],
  fetchAllScopeInstitutions,
  onDataChange,
}) {
  const [balancesHidden] = useBalancesHidden();
  const location = useLocation();
  // Cash-flow-local period state — independent of the global timeline used by
  // other pages, and not persisted across visits. It defaults to the current
  // month (Month size), but the Dashboard cash-flow panel's "Go to Cash Flow"
  // link seeds it (via router `state.cashFlowPeriod`) so the page opens on the
  // panel's selected period. Window-size model (Month/Quarter/Year/YTD/All/
  // Custom) + calendar prev/next navigation — see CashFlowTimelineControl.
  const [period, setPeriod] = useState(() => {
    const seeded = location.state?.cashFlowPeriod;
    if (seeded?.periodKey) {
      return {
        periodKey: seeded.periodKey,
        anchor: seeded.anchor ? new Date(seeded.anchor) : getAppNow(),
        customRange: seeded.customRange || { start: '', end: '' },
      };
    }
    return {
      periodKey: 'month',
      anchor: getAppNow(),
      customRange: { start: '', end: '' },
    };
  });
  // Two scope states: `appliedAccountIds` is what the cash-flow query
  // actually uses; `draftAccountIds` is the in-popover working set the user
  // can toggle freely before pressing Apply. Cancel/outside-click discards
  // the draft. Same pattern as the Accounts page.
  const [appliedAccountIds, setAppliedAccountIds] = useState(() => new Set());
  const [draftAccountIds, setDraftAccountIds] = useState(() => new Set());
  const [scopeInitialized, setScopeInitialized] = useState(false);
  // Single source of truth for which toolbar popover is open. Opening one
  // implicitly closes the other (mutual exclusion), and clicking outside
  // closes both via `useDismissibleLayer`.
  const [openMenu, setOpenMenu] = useState(null); // null | 'scope'
  const scopeOpen = openMenu === 'scope';
  const [data, setData] = useState(null);
  // CashFlow converts every transaction into the primary currency (chosen in
  // Settings) on the backend; the response echoes it back as the primary
  // currency. Per-transaction rows still show their own native currency.
  const currency = data ? data.period.currency : 'CAD';
  // Cash-flow totals arrive from the backend in the primary currency; re-base
  // them into the active primary currency for headline figures + category/trend
  // panels. Per-transaction amounts stay in their native currency.
  const { primaryCurrency, convert } = useCurrency();
  const { colors } = useTheme();
  const toDisplay = useCallback((amount) => convert(amount, currency, primaryCurrency), [convert, currency, primaryCurrency]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [scopeSaveError, setScopeSaveError] = useState(null);
  const [scopeSaving, setScopeSaving] = useState(false);
  // Bumped after a recurring-series action (dismiss/confirm) to re-fetch.
  const [recurringRefresh, setRecurringRefresh] = useState(0);
  // Bumped after a scope Apply persists `hidden`, to refetch once the global
  // visibility change has landed (covers unhiding accounts the early refetch
  // would otherwise miss).
  const [scopeRefresh, setScopeRefresh] = useState(0);
  const [reviewRefresh, setReviewRefresh] = useState(0);
  const [loadRetryVersion, setLoadRetryVersion] = useState(0);
  const [tourHintVersion, setTourHintVersion] = useState(0);

  const scopeRef = useRef(null);
  const spendingPanelRef = useRef(null);

  // Right-tray detail. One state (mutual exclusion) drives a single anchored
  // tray tied to whichever panel was clicked: a Spending family row opens its
  // sub-categories (client-side, from the leaf breakdown); an Income leaf row
  // opens that category's transactions for the period (fetched).
  const [detail, setDetail] = useState(null); // { kind:'spending'|'income'|'investment', row } | null
  const cashFlowDetailTray = useRightTrayOpenState('cashflow-detail', Boolean(detail));
  const [reviewTransaction, setReviewTransaction] = useState(null);
  const [reviewCategories, setReviewCategories] = useState([]);
  const reviewRowsRef = useRef(null);
  const reviewTransactionTray = useRightTrayOpenState('transaction-detail', Boolean(reviewTransaction));

  useEffect(() => {
    const handleTourHint = () => setTourHintVersion((value) => value + 1);
    window.addEventListener('breaktwenty-tour-hint', handleTourHint);
    return () => window.removeEventListener('breaktwenty-tour-hint', handleTourHint);
  }, []);

  // Discard the draft when the scope popover is dismissed without Apply.
  const closeScopeWithoutSave = useCallback(() => {
    setDraftAccountIds(new Set(appliedAccountIds));
    setOpenMenu(null);
  }, [appliedAccountIds]);
  useDismissibleLayer({
    open: scopeOpen,
    onDismiss: closeScopeWithoutSave,
    ref: scopeRef,
  });

  // Initialize scope from the *global* visibility — the currently-visible
  // (non-hidden) accounts — so the Cash Flow selector reflects the same scope
  // as Transactions / institution settings instead of claiming "everything"
  // while the data respects hidden. On a fresh install the authoritative scope
  // can be an empty array; initializing that state lets the page fetch the
  // valid empty cash-flow payload instead of staying in the pre-request loader.
  useEffect(() => {
    if (scopeInitialized) return undefined;
    let cancelled = false;
    const initializeScope = (institutions) => {
      if (cancelled) return;
      const ids = selectedAccountIdsFromScope(institutions || []);
      setAppliedAccountIds(ids);
      setDraftAccountIds(new Set(ids));
      setScopeInitialized(true);
    };

    if (allScopeInstitutions.length > 0) {
      Promise.resolve().then(() => initializeScope(allScopeInstitutions));
      return () => {
        cancelled = true;
      };
    }

    if (fetchAllScopeInstitutions) {
      Promise.resolve(fetchAllScopeInstitutions()).then((institutions) => {
        if (Array.isArray(institutions)) {
          initializeScope(institutions);
        }
      });
      return () => {
        cancelled = true;
      };
    }

    Promise.resolve().then(() => initializeScope([]));
    return () => {
      cancelled = true;
    };
  }, [allScopeInstitutions, scopeInitialized, fetchAllScopeInstitutions]);

  useEffect(() => {
    if (!scopeInitialized || scopeOpen || allScopeInstitutions.length === 0) return;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (cancelled) return;
      const ids = selectedAccountIdsFromScope(allScopeInstitutions);
      setAppliedAccountIds(ids);
      setDraftAccountIds(new Set(ids));
    });
    return () => {
      cancelled = true;
    };
  }, [allScopeInstitutions, scopeInitialized, scopeOpen]);

  useEffect(() => {
    if (!isTourDemoActive()) return;
    const hint = getTourHint();
    if (!hint || hint.page !== 'cashflow') return;
    const tourCashFlowAnchor = getTourCashFlowAnchor();
    const currentAnchor = period.anchor instanceof Date ? period.anchor : new Date(period.anchor || getAppNow().getTime());
    if (
      period.periodKey === 'month'
      && currentAnchor.getFullYear() === tourCashFlowAnchor.getFullYear()
      && currentAnchor.getMonth() === tourCashFlowAnchor.getMonth()
    ) {
      return;
    }
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) {
        setPeriod({
          periodKey: 'month',
          anchor: tourCashFlowAnchor,
          customRange: { start: '', end: '' },
        });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [period, tourHintVersion]);

  const { start: startDate, end: endDate } = useMemo(
    () => computeCashFlowRange(period.periodKey, period.anchor, period.customRange),
    [period.periodKey, period.anchor, period.customRange],
  );

  // The "vs. previous period" comparison uses the period the picker would show
  // one step back, so the backend's previous_totals matches it exactly (instead
  // of a fixed day-span that drops a day on unequal-length calendar months).
  // Only steppable presets (month/quarter/year) have a well-defined prior step.
  const { start: prevStartDate, end: prevEndDate } = useMemo(() => {
    if (!canStepCashFlow(period.periodKey)) return { start: null, end: null };
    const prevAnchor = stepCashFlowAnchor(period.periodKey, period.anchor, -1);
    return computeCashFlowRange(period.periodKey, prevAnchor, period.customRange);
  }, [period.periodKey, period.anchor, period.customRange]);

  // Build the URL once per scope/period change. Uses the *applied* account
  // set — the draft only drives the in-popover checkboxes until Apply.
  // `noSourcesSelected` is a distinct state from "scope not yet initialized":
  // when the user explicitly toggled All Off + Apply, the result should be
  // zero rows, not "default to everything".
  const noSourcesSelected = scopeInitialized && appliedAccountIds.size === 0;
  const fetchKey = useMemo(() => {
    const accountIds = Array.from(appliedAccountIds).sort((a, b) => a - b).join(',');
    return `${startDate || ''}|${endDate || ''}|${prevStartDate || ''}|${prevEndDate || ''}|${accountIds}|${noSourcesSelected ? 'empty' : 'set'}|${recurringRefresh}|${scopeRefresh}|${reviewRefresh}|${loadRetryVersion}|${tourHintVersion}`;
  }, [startDate, endDate, prevStartDate, prevEndDate, appliedAccountIds, noSourcesSelected, recurringRefresh, scopeRefresh, reviewRefresh, loadRetryVersion, tourHintVersion]);

  useEffect(() => {
    const tourCashFlowActive = isTourDemoActive() && getTourHint()?.page === 'cashflow';
    if (!scopeInitialized && !tourCashFlowActive) return;
    const params = new URLSearchParams();
    if (startDate) params.set('start_date', startDate);
    if (endDate) params.set('end_date', endDate);
    if (prevStartDate) params.set('previous_start_date', prevStartDate);
    if (prevEndDate) params.set('previous_end_date', prevEndDate);
    if (noSourcesSelected) {
      // Force zero results — backend treats an `IN (-1)` filter as "match
      // nothing" since no account has id -1.
      params.set('account_ids', '-1');
    } else {
      const acctIds = Array.from(appliedAccountIds);
      if (acctIds.length > 0) params.set('account_ids', acctIds.join(','));
    }

    let cancelled = false;
    const controller = new AbortController();
    const loadCashFlow = async () => {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(`${API}/cash-flow?${params.toString()}`, {
          signal: controller.signal,
        });
        const payload = await readCashFlowResponse(response);
        if (!cancelled) setData(payload);
      } catch (err) {
        if (!cancelled && err?.name !== 'AbortError') {
          setError(err.message || 'Failed to load cash flow data');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void loadCashFlow();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // `primaryCurrency` is included so changing the global currency refetches:
    // the backend bakes the saved primary into `period.currency` and every
    // `*_amount_primary` (recurring rows, detail-tray `convert_to`), none of
    // which re-derive client-side. Settings is persisted before the context
    // updates (App.setPrimaryCurrency), so the refetch reads the new primary.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchKey, scopeInitialized, primaryCurrency]);

  // A scope/period change replaces the dataset, so any open detail is stale.
  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) {
        setDetail(null);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [fetchKey]);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) setReviewTransaction(null);
    });
    return () => {
      cancelled = true;
    };
  }, [startDate, endDate, appliedAccountIds, noSourcesSelected]);

  // Scope toggling — operates on the DRAFT set so the user can iterate in
  // the popover before pressing Apply. `InstitutionAccountSelector` passes
  // the enriched institution object (with `accountIds`) to onToggleInstitution
  // and the bare account id to onToggleAccount.
  const handleToggleDraftAccount = useCallback((accountId) => {
    setDraftAccountIds((prev) => {
      const next = new Set(prev);
      if (next.has(accountId)) next.delete(accountId);
      else next.add(accountId);
      return next;
    });
  }, []);
  const handleToggleDraftInstitution = useCallback((institution) => {
    const accountIds = institution.accountIds;
    if (accountIds.length === 0) return;
    setDraftAccountIds((prev) => {
      const allSelected = accountIds.every((id) => prev.has(id));
      const next = new Set(prev);
      accountIds.forEach((id) => { if (allSelected) next.delete(id); else next.add(id); });
      return next;
    });
  }, []);

  // Total accounts across all institutions — used to drive the "All On / All Off"
  // toggle and the summary string.
  const allKnownAccountIds = useMemo(() => {
    const ids = new Set();
    allScopeInstitutions.forEach((inst) => {
      inst.accounts.forEach((acct) => ids.add(acct.id));
    });
    return ids;
  }, [allScopeInstitutions]);
  const allDraftSelected = useMemo(() => (
    allKnownAccountIds.size > 0
    && draftAccountIds.size === allKnownAccountIds.size
    && Array.from(allKnownAccountIds).every((id) => draftAccountIds.has(id))
  ), [allKnownAccountIds, draftAccountIds]);
  const handleToggleAllSources = useCallback(() => {
    if (allDraftSelected) {
      setDraftAccountIds(new Set());
    } else {
      setDraftAccountIds(new Set(allKnownAccountIds));
    }
  }, [allDraftSelected, allKnownAccountIds]);
  // Scope is global: applying persists the selection as institution/account
  // visibility (`hidden`) so it's shared everywhere (Transactions, Accounts,
  // recurring), then refreshes the shared universe. Deselected = hidden.
  const handleApplyScope = useCallback(async () => {
    setScopeSaveError(null);
    setScopeSaving(true);
    try {
      await persistVisibilityScope({
        apiBase: API,
        institutions: scopeWithSelectedAccounts(allScopeInstitutions, draftAccountIds),
        previousInstitutions: allScopeInstitutions,
      });
      setAppliedAccountIds(new Set(draftAccountIds));
      setOpenMenu(null);
      setScopeRefresh((n) => n + 1);
      if (onDataChange) Promise.resolve(onDataChange()).catch(() => {});
      if (fetchAllScopeInstitutions) Promise.resolve(fetchAllScopeInstitutions()).catch(() => {});
    } catch (err) {
      setScopeSaveError(err.message || 'The account scope could not be saved.');
      const authoritativeInstitutions = await reconcileVisibilityScope({
        onDataChange,
        fetchAllScopeInstitutions,
      });
      const reconciledAccountIds = Array.isArray(authoritativeInstitutions)
        ? selectedAccountIdsFromScope(authoritativeInstitutions)
        : new Set(appliedAccountIds);
      setAppliedAccountIds(reconciledAccountIds);
      setDraftAccountIds(new Set(reconciledAccountIds));
    } finally {
      setScopeSaving(false);
    }
  }, [appliedAccountIds, draftAccountIds, allScopeInstitutions, fetchAllScopeInstitutions, onDataChange]);

  const triggerAccountIds = scopeOpen ? draftAccountIds : appliedAccountIds;
  const selectedInstitutionCount = useMemo(() => (
    allScopeInstitutions.filter(
      (inst) => inst.accounts.some((acct) => triggerAccountIds.has(acct.id)),
    ).length
  ), [allScopeInstitutions, triggerAccountIds]);
  const scopeSummary = formatScopeSelectionSummary({
    totalInstitutions: allScopeInstitutions.length,
    selectedInstitutions: selectedInstitutionCount,
    selectedAccounts: triggerAccountIds.size,
  });
  const scopeSlot = typeof document === 'undefined' ? null : document.getElementById('cash-flow-toolbar-scope-slot');
  const timelineSlot = typeof document === 'undefined' ? null : document.getElementById('cash-flow-toolbar-timeline-slot');

  const scopeControl = (
    <div className="investments-filter-popover dashboard-scope-popover" ref={scopeRef}>
      <ScopeSelectorTrigger
        isOpen={scopeOpen}
        summary={scopeSummary}
        controls="cash-flow-scope-panel"
        onClick={() => {
          if (scopeOpen) {
            closeScopeWithoutSave();
          } else {
            // Open with a fresh draft cloned from the currently-applied set.
            setDraftAccountIds(new Set(appliedAccountIds));
            setOpenMenu('scope');
          }
        }}
      />
      <div
        id="cash-flow-scope-panel"
        role="dialog"
        aria-label="Cash Flow scope filters"
        className={`investments-filter-panel scope-selector-panel dashboard-scope-panel ${scopeOpen ? 'is-open' : ''}`.trim()}
        aria-hidden={!scopeOpen}
      >
        <InstitutionAccountSelector
          key={scopeOpen ? 'open' : 'closed'}
          institutions={allScopeInstitutions}
          selectedAccountIds={draftAccountIds}
          onToggleInstitution={handleToggleDraftInstitution}
          onToggleAccount={handleToggleDraftAccount}
          headerActions={(
            <>
              <button
                type="button"
                className="institution-filter-toggle-all app-control-root"
                onClick={handleToggleAllSources}
              >
                <span className="app-control-label">{allDraftSelected ? 'All Off' : 'All On'}</span>
              </button>
              <button
                type="button"
                className="btn-primary scope-filter-apply-btn app-control-root"
                onClick={handleApplyScope}
                disabled={scopeSaving}
              >
                <span className="app-control-label">{scopeSaving ? 'Saving…' : 'Apply'}</span>
              </button>
            </>
          )}
        />
      </div>
    </div>
  );
  const timelineControl = (
    <CashFlowTimelineControl
      periodKey={period.periodKey}
      anchor={period.anchor}
      customRange={period.customRange}
      onChange={(next) => setPeriod({
        periodKey: next.periodKey,
        anchor: next.anchor instanceof Date ? next.anchor : new Date(next.anchor || getAppNow().getTime()),
        customRange: next.customRange || { start: '', end: '' },
      })}
    />
  );

  const totals = data ? data.totals : { income: 0, expense: 0, net: 0 };
  const incomeBreakdown = data ? data.income_breakdown : [];
  // Spending rolls leaves up to their parent (Family > leaf). Income stays
  // at leaf granularity because the user has just a handful of income
  // categories (Paycheck, Refund, Dividend, etc.). Memo deps on `data` —
  // the `|| []` fallback would create a fresh array every render and
  // break memoization, so we re-resolve inside.
  const expenseByParent = useMemo(
    () => rollupByParent(data ? data.expense_breakdown : []),
    [data],
  );
  // Welcome tour (demo mode): pre-select a spending category so its detail tray opens.
  useEffect(() => {
    if (!isTourDemoActive()) return;
    const hint = getTourHint();
    if (!hint || hint.page !== 'cashflow' || hint.spendingCategoryId == null) return;
    const row = expenseByParent.find((r) => r.category_id === hint.spendingCategoryId);
    if (!row) return;
    let cancelled = false;
    let frameId = null;
    Promise.resolve().then(() => {
      if (!cancelled) {
        setDetail({ kind: 'spending', row });
        frameId = window.requestAnimationFrame(() => {
          if (cancelled) return;
          const panel = spendingPanelRef.current;
          const scrollContainer = document.querySelector('.app-main');
          if (!panel || !scrollContainer) return;
          const panelRect = panel.getBoundingClientRect();
          const containerRect = scrollContainer.getBoundingClientRect();
          const top = scrollContainer.scrollTop
            + panelRect.top
            - containerRect.top
            - Math.max(0, (containerRect.height - Math.min(panelRect.height, containerRect.height)) / 2);
          scrollContainer.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
        });
      }
    });
    return () => {
      cancelled = true;
      if (frameId != null) window.cancelAnimationFrame(frameId);
    };
  }, [expenseByParent]);
  const currentTourHint = getTourHint();
  const cashFlowTourTrayLocked = Boolean(
    detail
    && isTourDemoActive()
    && currentTourHint?.page === 'cashflow'
    && currentTourHint.spendingCategoryId != null
    && detail.kind === 'spending'
    && String(detail.row?.category_id) === String(currentTourHint.spendingCategoryId)
  );
  const handleDetailSelect = useCallback((kind, row) => {
    setReviewTransaction(null);
    setDetail((current) => {
      if (isSameCashFlowDetail(current, kind, row)) {
        return cashFlowTourTrayLocked ? current : null;
      }
      return { kind, row };
    });
  }, [cashFlowTourTrayLocked]);
  const loadReviewCategories = useCallback(async () => {
    const categories = await loadTransactionCategories();
    setReviewCategories(categories);
    return categories;
  }, []);
  const handleReviewTransactionSelect = useCallback((transaction) => {
    setDetail(null);
    setReviewTransaction(transaction);
  }, []);
  const investmentActivity = data ? data.investment_activity : null;
  const needsReview = data ? data.needs_review : null;
  const trend = data ? data.trend_12_months : [];
  const recurring = data ? data.recurring : [];
  const cashFlowViewState = getRemoteDataViewState({
    hasData: Boolean(data),
    loading,
    error,
  });

  return (
    <div className="page-frame cash-flow-page">
      {scopeSlot && createPortal(scopeControl, scopeSlot)}
      {timelineSlot && createPortal(timelineControl, timelineSlot)}
      <AppStatusNotice
        title="Account scope was not saved"
        message={scopeSaveError}
        onDismiss={() => setScopeSaveError(null)}
      />
      {data && error ? (
        <AppStatusNotice
          title="Cash flow could not be refreshed"
          message={error}
          actionLabel="Retry"
          onAction={() => setLoadRetryVersion((value) => value + 1)}
          actionDisabled={loading}
          onDismiss={() => setError(null)}
        />
      ) : null}

      {cashFlowViewState === 'loading' ? (
        <div className="cf-loading">Loading cash flow…</div>
      ) : cashFlowViewState === 'error' ? (
        <AppStatusNotice
          title="Cash flow could not be loaded"
          message={error}
          actionLabel="Retry"
          onAction={() => setLoadRetryVersion((value) => value + 1)}
        />
      ) : (
        <>
          {/* Headline row: left panel = Net Cash Flow donut with a 2x2 figures
              grid below it (Income / Expense / Savings Rate / vs-Previous);
              right panel = Recurring Expenses. Wrapper grid uses auto-fit so the
              two panels stack on narrow viewports without a manual breakpoint. */}
          <div className="cf-headline-row">
          <section className="panel-shell cf-headline">
            <div className="cf-headline-donut">
              <CashFlowNetDonut totals={totals} currency={currency} balancesHidden={balancesHidden} />
              <div className="cf-donut-center">
                <div className="cf-donut-center-label">Net Cash Flow</div>
                <div className={`cf-donut-center-value ${totals.net > 0 ? 'is-positive' : totals.net < 0 ? 'is-negative' : ''}`.trim()}>
                  <Money amount={toDisplay(totals.net)} currency={primaryCurrency} hidden={balancesHidden} signed />
                </div>
              </div>
            </div>

            <div className="cf-headline-figures">
              <div className="cf-net-bars">
                {[
                  { key: 'income', name: 'Income', color: colors.positive, amount: totals.income },
                  { key: 'expense', name: 'Expense', color: colors.negative, amount: totals.expense },
                ].map((row) => {
                  const denom = Math.max(totals.income, 0) + Math.max(totals.expense, 0);
                  const pct = denom > 0 ? (Math.max(row.amount, 0) / denom) * 100 : 0;
                  return (
                    <div className="cf-net-row" key={row.key}>
                      <span className="cf-net-chip">
                        <span className="cf-net-dot" style={{ background: row.color }} aria-hidden="true" />
                        <span className="cf-net-name">{row.name}</span>
                      </span>
                      <div className="cf-bar-track">
                        <div className="cf-bar-fill" style={{ width: `${Math.min(100, pct)}%`, background: row.color }} />
                      </div>
                      <div className="cf-bar-amount">
                        <Money amount={toDisplay(row.amount)} currency={primaryCurrency} hidden={balancesHidden} className="cf-bar-money" />
                        <span className="cf-bar-percent">{pct.toFixed(1)}%</span>
                      </div>
                    </div>
                  );
                })}
              </div>
              <SavingsStatsPanel
                totals={totals}
                previousTotals={data?.previous_totals ?? null}
                currency={currency}
                balancesHidden={balancesHidden}
              />
            </div>
          </section>

          <div className="cf-headline-side">
          <div className="cf-headline-side-inner">
          <RecurringPanel
            recurring={recurring}
            currency={currency}
            balancesHidden={balancesHidden}
            onChanged={() => setRecurringRefresh((n) => n + 1)}
          />
          </div>
          </div>
          </div>

          {/* Money Movement — non-spend / non-income rollup (investments, CC/loan
              paydowns), grouped Trading / Income / Debt. Full-width below the hero
              so the tiles have room to breathe instead of squeezing the donut row. */}
          <InvestmentActivityPanel
            activity={investmentActivity}
            currency={currency}
            balancesHidden={balancesHidden}
            onRowClick={(row) => handleDetailSelect('investment', row)}
            activeRowId={detail?.kind === 'investment' ? (detail.row.category_id ?? detail.row.seed_key) : null}
          />

          <NeedsReviewPanel
            review={needsReview}
            balancesHidden={balancesHidden}
            startDate={startDate}
            endDate={endDate}
            accountIds={appliedAccountIds}
            noSourcesSelected={noSourcesSelected}
            refreshVersion={reviewRefresh}
            activeTransactionId={reviewTransaction?.id ?? null}
            rowsRef={reviewRowsRef}
            loadCategories={loadReviewCategories}
            onTransactionClick={handleReviewTransactionSelect}
          />

          {/* Expense breakdown — donut + horizontal bars, rolled up to
              parent family categories (e.g. FOOD & DRINK, not GROCERIES). */}
          <CategoryBreakdownPanel
            panelId="cash-flow:spending-by-category"
            panelRef={spendingPanelRef}
            title="Spending by Category"
            breakdown={expenseByParent.map((row) => ({ ...row, amount: toDisplay(row.amount) }))}
            currency={primaryCurrency}
            balancesHidden={balancesHidden}
            emptyMessage="No expense transactions in this period."
            centerLabel="Total Spent"
            defaultOpen
            onRowClick={(row) => handleDetailSelect('spending', row)}
            activeRowId={detail?.kind === 'spending' ? detail.row.category_id : null}
          />

          {/* Income breakdown — leaf-level, no rollup. Open by default, same as Spending. */}
          <CategoryBreakdownPanel
            panelId="cash-flow:income-by-category"
            title="Income by Category"
            breakdown={incomeBreakdown.map((row) => ({ ...row, amount: toDisplay(row.amount) }))}
            currency={primaryCurrency}
            balancesHidden={balancesHidden}
            emptyMessage="No income transactions in this period."
            centerLabel="Total Received"
            defaultOpen
            onRowClick={(row) => handleDetailSelect('income', row)}
            activeRowId={detail?.kind === 'income' ? detail.row.category_id : null}
          />

          {/* 12-month trend */}
          <TrendPanel trend={(trend || []).map((m) => ({ ...m, income: toDisplay(m.income), expense: toDisplay(m.expense), net: toDisplay(m.net) }))} currency={primaryCurrency} balancesHidden={balancesHidden} />

          <CashFlowDetailTray
            detail={detail}
            animatedOpen={cashFlowDetailTray.animatedOpen}
            onClose={() => setDetail(null)}
            expenseBreakdown={data ? data.expense_breakdown : []}
            startDate={startDate}
            endDate={endDate}
            accountIds={appliedAccountIds}
            noSourcesSelected={noSourcesSelected}
            currency={currency}
            balancesHidden={balancesHidden}
            dismissLocked={cashFlowTourTrayLocked}
          />

          <TransactionDetailDrawer
            transaction={reviewTransaction}
            animatedOpen={reviewTransactionTray.animatedOpen}
            categories={reviewCategories}
            excludeFromDismissRef={reviewRowsRef}
            onClose={() => setReviewTransaction(null)}
            onCategoriesChanged={loadReviewCategories}
            onSaved={() => {
              setReviewRefresh((value) => value + 1);
              if (onDataChange) Promise.resolve(onDataChange()).catch(() => {});
            }}
          />
        </>
      )}
    </div>
  );
}

function CategoryBreakdownPanel({ panelId, panelRef = null, title, breakdown, currency, balancesHidden, emptyMessage, centerLabel, defaultOpen = false, onRowClick, activeRowId = null }) {
  const [collapsed, setCollapsed] = usePersistentPanelCollapsed(panelId, !defaultOpen);
  const { chartColors, mode } = useTheme();
  const open = !collapsed;
  const total = breakdown.reduce((sum, row) => sum + row.amount, 0);
  const top = breakdown.slice(0, 10);
  const hasData = breakdown.length > 0;
  const donutOption = useMemo(
    () => buildCfDonutOption(
      breakdown.slice(0, 10).map((row) => ({
        name: row.name,
        value: row.amount,
        color: resolveCategoryAccentColor(row, mode, chartColors.other),
      })),
      { total, currency, balancesHidden, chartColors },
    ),
    [balancesHidden, breakdown, chartColors, currency, mode, total],
  );

  return (
    <section ref={panelRef} className={`panel-shell cf-panel cf-breakdown-panel ${open ? 'is-open' : ''}`.trim()}>
      <button
        type="button"
        className="cf-panel-header"
        onClick={() => setCollapsed((p) => !p)}
        aria-expanded={open}
      >
        <span className="cf-panel-chevron" aria-hidden="true">
          <TriangleIcon direction={open ? 'down' : 'right'} />
        </span>
        <span className="cf-panel-title">{title}</span>
      </button>
      {open && (
        <div className="cf-panel-body cf-breakdown-body">
          {!hasData ? (
            <div className="cf-empty">{emptyMessage}</div>
          ) : (
            <div className="cf-breakdown-scroll-frame">
              <CategoryBreakdownHorizontalScrollProxy />
              <div className="cf-breakdown-scroll-viewport">
                <div className="cf-breakdown-scroll-surface">
                  <div className="page-split cf-breakdown-grid">
              <div className="cf-breakdown-donut">
                <StablePieTooltipEChart
                  option={donutOption}
                  height={CASH_FLOW_DONUT_HEIGHT}
                  onEvents={onRowClick ? {
                    // Clicking a donut segment opens the same tray as its bar row.
                    // The donut and bars share `breakdown.slice(0, 10)` (= `top`),
                    // so the pie dataIndex maps straight onto the row.
                    click: (params) => {
                      const row = top[params?.dataIndex];
                      if (row) onRowClick(row);
                    },
                  } : undefined}
                />
                {centerLabel && (
                  <div className="cf-donut-center">
                    <div className="cf-donut-center-label">{centerLabel}</div>
                    <div className="cf-donut-center-value">
                      <Money amount={total} currency={currency} hidden={balancesHidden} />
                    </div>
                  </div>
                )}
              </div>
              <div className="cf-breakdown-bars">
                {top.map((row) => (
                  <div
                    className={`cf-bar-row ${onRowClick ? 'is-clickable' : ''} ${row.category_id === activeRowId ? 'is-active' : ''}`.trim()}
                    key={row.category_id}
                    onClick={onRowClick ? () => onRowClick(row) : undefined}
                    role={onRowClick ? 'button' : undefined}
                    tabIndex={onRowClick ? 0 : undefined}
                    onKeyDown={onRowClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRowClick(row); } } : undefined}
                  >
                    <div className="cf-bar-label">
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
                    </div>
                    <div className="cf-bar-track">
                      <div
                        className="cf-bar-fill"
                        style={{
                          width: `${Math.min(100, row.percent_of_total)}%`,
                          background: resolveCategoryAccentColor(row, mode, chartColors.other),
                        }}
                      />
                    </div>
                    <div className="cf-bar-amount">
                      <Money amount={row.amount} currency={currency} hidden={balancesHidden} className="cf-bar-money" />
                      <span className="cf-bar-percent">{row.percent_of_total.toFixed(1)}%</span>
                    </div>
                  </div>
                ))}
              </div>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// Category ids behind a clicked breakdown row: Income/Investment = the leaf itself;
// Spending = every leaf under the family (+ the family id). Shared by the
// tray's transaction fetch and its "View all" handoff to the Transactions page.
function categoryIdsForDetail(detail, expenseBreakdown) {
  if (!detail) return [];
  return detail.kind === 'spending'
    ? expenseBreakdown
        .filter((r) => r.parent_id === detail.row.category_id)
        .map((r) => r.category_id)
        .concat(detail.row.category_id)
    : [detail.row.category_id];
}

// Bucket the tray's transactions into day groups, most-recent day first, so the
// list reads top-down newest → oldest under full-date headers (same
// "Thursday, May 28, 2026" format as the Transactions page).
function groupTrayTransactionsByDate(txns) {
  const groups = new Map();
  (txns || []).forEach((t) => {
    const key = String(t?.date || '').slice(0, 10);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(t);
  });
  return Array.from(groups.entries())
    .sort((a, b) => (a[0] < b[0] ? 1 : a[0] > b[0] ? -1 : 0))
    .map(([key, items]) => ({ key: key || 'undated', label: formatLongDateValue(key, '—'), items }));
}

// Pinned right-tray for the Cash Flow breakdown panels. One component serves
// both panels: a Spending family row shows its sub-categories (client-side from
// the leaf breakdown); an Income leaf row shows that category's transactions for
// the period (fetched). The selected source remains identified by its highlight
// and the tray title, matching the app's other pinned detail trays.
function CashFlowDetailTray({ detail, animatedOpen = false, onClose, expenseBreakdown, startDate, endDate, accountIds, noSourcesSelected, currency, balancesHidden, dismissLocked = false }) {
  const [snapshot, setSnapshot] = useState(detail || null);
  const [txns, setTxns] = useState(null);
  const [txTotal, setTxTotal] = useState(0);
  const [txLoading, setTxLoading] = useState(false);
  const [txError, setTxError] = useState(null);
  const [txRetryVersion, setTxRetryVersion] = useState(0);
  const shellRef = useRef(null);
  const navigate = useNavigate();
  const handleClose = useCallback((event) => {
    if (dismissLocked) {
      event?.preventDefault?.();
      return;
    }
    onClose();
  }, [dismissLocked, onClose]);

  // Keep mounted through the slide-out, then clear after the transition.
  useEffect(() => {
    let cancelled = false;
    if (detail) {
      Promise.resolve().then(() => {
        if (!cancelled) {
          setSnapshot(detail);
        }
      });
      return () => {
        cancelled = true;
      };
    }
    const t = setTimeout(() => setSnapshot(null), 360);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [detail]);

  // Close on any click that isn't the tray itself or a tray-trigger — a
  // clickable breakdown/money-movement row, or a breakdown donut segment.
  // So clicking empty space anywhere (including inside a panel) closes the
  // tray, while clicking a different row/segment switches it instead.
  useDismissibleLayer({
    open: Boolean(detail),
    refs: [shellRef],
    ignoreSelector: '.cf-bar-row.is-clickable, .cf-invest-row.is-clickable, .cf-breakdown-donut',
    ignoreAppChrome: true,
    onDismiss: handleClose,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

  // Fetch the period's transactions behind the clicked row. Income = the leaf
  // category itself; Spending = every leaf under the clicked family (plus the
  // family id, for txns categorized straight to the parent). Each row then
  // surfaces its institution/account (and, for spending, its sub-category).
  // The tray mirrors the page's account-scope selector and timeline, and asks
  // the backend to convert each amount into the primary currency (for the
  // bracketed value) via `convert_to`.
  useEffect(() => {
    let cancelled = false;
    if (!detail) {
      Promise.resolve().then(() => {
        if (!cancelled) {
          setTxns(null);
          setTxError(null);
        }
      });
      return () => {
        cancelled = true;
      };
    }
    const isRealizedPnl = detail.kind === 'investment' && detail.row?.seed_key === 'realized_pnl';
    const params = new URLSearchParams();
    if (isRealizedPnl) {
      // Future P/L isn't a category — it's the realized_pnl stamped on futures
      // trades. Pull the period's FUT trades and keep the ones carrying P&L.
      params.set('asset_category', 'FUT');
    } else {
      const categoryIds = categoryIdsForDetail(detail, expenseBreakdown);
      params.set('category_ids', categoryIds.join(','));
    }
    if (startDate) params.set('start_date', startDate);
    // `end_date` is an inclusive calendar day; the backend (services.date_window)
    // covers the whole day, so the tray matches the breakdown bar for the period.
    if (endDate) params.set('end_date', endDate);
    if (noSourcesSelected) {
      params.set('account_ids', '-1');
    } else {
      const acctIds = Array.from(accountIds || []);
      if (acctIds.length > 0) params.set('account_ids', acctIds.join(','));
    }
    if (currency) params.set('convert_to', currency);
    // High cap so a wide category (e.g. Financial / All Time) loads in full and
    // the tray's own scroll handles display. `total` from the response tells us
    // the true count so any truncation beyond the cap is shown to the user.
    params.set('limit', '500');
    const controller = new AbortController();
    const loadTransactions = async () => {
      setTxLoading(true);
      setTxError(null);
      setTxns(null);
      setTxTotal(0);
      try {
        const response = await fetch(`${API}/transactions?${params.toString()}`, {
          signal: controller.signal,
        });
        const payload = await readTransactionCollectionResponse(response, {
          label: 'Cash-flow transactions',
        });
        if (cancelled) return;
        let list = payload.transactions;
        let total = payload.total;
          // CC/Loan payment categories carry both legs (the funding outflow + the
          // paydown credit that lands on the liability); show only the actual
          // payments (the positive paydown leg) so the tray matches the
          // positive-only box total.
          if (detail.kind === 'investment' && (detail.row?.seed_key === 'cc_payment' || detail.row?.seed_key === 'loan_payment')) {
            list = list.filter((t) => Number(t.amount) > 0);
            total = list.length;
          }
          // Futures don't count in buys/sells (the notional isn't cash — realized P&L shows in
          // its own tile), so the buy/sell tray must drop them too, or the listed trades wouldn't
          // sum to the tile total.
          if (detail.kind === 'investment' && (detail.row?.seed_key === 'buy' || detail.row?.seed_key === 'sell')) {
            list = list.filter((t) => (t.asset_category || '') !== 'FUT');
            total = list.length;
          }
          // Future P/L tray: keep only the trades that actually realized a gain/loss
          // (the closing legs); the row amount shown is each trade's realized_pnl, so
          // the list sums to the tile total instead of the excluded notionals.
          if (isRealizedPnl) {
            list = list.filter((t) => Math.abs(Number(t.realized_pnl) || 0) > 0.005);
            total = list.length;
          }
        setTxns(list);
        setTxTotal(total);
      } catch (err) {
        if (!cancelled && err?.name !== 'AbortError') {
          setTxError(err.message || 'Transactions could not be loaded.');
        }
      } finally {
        if (!cancelled) setTxLoading(false);
      }
    };
    void loadTransactions();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [detail, startDate, endDate, accountIds, noSourcesSelected, currency, expenseBreakdown, txRetryVersion]);

  const target = typeof document !== 'undefined' ? document.querySelector('.app-main') : null;
  if (!snapshot || !target) return null;

  const { kind, row } = snapshot;
  // Future P/L rows show each trade's realized P&L (the gain/loss it produced),
  // not its notional amount — so the list reconciles with the tile.
  const isPnlRow = kind === 'investment' && row?.seed_key === 'realized_pnl';
  const handleViewAll = () => {
    navigate('/transactions', {
      state: {
        // Future P/L has no category — hand off the futures asset class so the
        // Transactions page shows every buy/sell that produced the realized P&L.
        cashFlowFilter: isPnlRow
          ? { assetCategory: 'FUT', startDate, endDate }
          : { categoryIds: categoryIdsForDetail(snapshot, expenseBreakdown), startDate, endDate },
      },
    });
    handleClose();
  };

  return createPortal((
    <div
      className={`app-edge-tray is-pinned cashflow-detail-tray ${animatedOpen ? 'is-open' : ''}`.trim()}
      role="dialog"
      aria-label={`${row.name} detail`}
      aria-hidden={!animatedOpen}
    >
      <div className="app-edge-tray-backdrop" aria-hidden="true" />
      <div className="app-edge-tray-shell" ref={shellRef}>
        <SideDetailDrawerPanel
          title={row.name}
          onClose={handleClose}
          className="panel-shell cashflow-detail-panel"
        >
          {isPnlRow ? (
            <p className="cf-detail-note">
              Realized gain/loss on closed futures — the underlying buy/sell trades, at full contract price, open via View all.
            </p>
          ) : null}
          {txLoading ? (
            <div className="cf-detail-empty">Loading…</div>
          ) : txError ? (
            <AppStatusNotice
              title="Transactions unavailable"
              message={txError}
              actionLabel="Retry"
              onAction={() => setTxRetryVersion((value) => value + 1)}
            />
          ) : !txns || txns.length === 0 ? (
            <div className="cf-detail-empty">No transactions.</div>
          ) : (
            <>
              <div className="cf-detail-head">
                <span className="cf-detail-count">
                  {txTotal > txns.length
                    ? `Showing ${txns.length} of ${txTotal}`
                    : `${txns.length} transaction${txns.length === 1 ? '' : 's'}`}
                </span>
                <button type="button" className="cf-detail-viewall button-shell-opt-out" onClick={handleViewAll}>
                  View all →
                </button>
              </div>
              <div className="cf-detail-list">
                {groupTrayTransactionsByDate(txns).map((group) => (
                  <div className="cf-detail-group" key={group.key}>
                    <div className="cf-detail-group-label">{group.label}</div>
                    <div className="cf-detail-group-rows">
                      {group.items.map((t) => {
                        const amt = isPnlRow ? t.realized_pnl : t.amount;
                        const amtPrimary = isPnlRow ? t.realized_pnl_primary : t.amount_primary;
                        const transactionTitle = getTransactionPrimaryDescription(t);
                        const quantityLabel = getTransactionQuantityLabel(t);
                        const transactionTooltip = [transactionTitle, quantityLabel].filter(Boolean).join(' ');
                        const accountTooltip = getTransactionAccountTooltip(t);
                        // Tone follows the signed transaction amount; refund categorization stays separate.
                        const amountToneClass = amt > 0 ? 'is-income' : amt < 0 ? 'is-spending' : '';
                        return (
                        <div className="cf-detail-tx" key={t.id}>
                          <span className="cf-detail-tx-logo" data-tooltip={accountTooltip || undefined}>
                            <InstitutionLogo name={t.institution_name} size={28} />
                          </span>
                          {kind === 'spending' && t.category ? (
                            <CategoryPill category={t.category} size="sm" />
                          ) : null}
                          <div className="cf-detail-tx-main">
                            <span className="cf-detail-tx-title" data-tooltip={transactionTooltip}>
                              <span className="cf-detail-tx-desc">{transactionTitle}</span>
                              {quantityLabel ? (
                                <span className="cf-detail-tx-quantity">{quantityLabel}</span>
                              ) : null}
                            </span>
                            {isPnlRow ? (
                              <span className="cf-detail-tx-meta">Realized P/L on this {t.type === 'sell' ? 'sell' : t.type === 'buy' ? 'buy' : 'trade'}</span>
                            ) : null}
                          </div>
                          <span className={`cf-detail-amount ${amountToneClass}`.trim()}>
                            <span className="cf-amount-native">
                              <Money amount={amt} currency={t.currency || currency} hidden={balancesHidden} className="cf-detail-money" />
                              {!balancesHidden ? (
                                <span className="cf-amount-native-code"> {t.currency || currency}</span>
                              ) : null}
                            </span>
                            {!balancesHidden && t.currency && t.currency !== currency && amtPrimary != null ? (
                              <FitMoney
                                className="cf-amount-converted"
                                full={`(${formatMoney(amtPrimary, currency)} ${currency})`}
                                compact={`(${formatCompactMoney(amtPrimary, currency)} ${currency})`}
                              />
                            ) : null}
                          </span>
                        </div>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </SideDetailDrawerPanel>
      </div>
    </div>
  ), target);
}

// Sibling panel to the Net Cash Flow donut. Surfaces two highest-signal
// observations for the period: how much of income the user kept (savings
// rate) and how that compares to the immediately preceding same-length
// window. `previousTotals` is null when the period is unbounded on the
// left (e.g. "All Time").
function cashFlowAmountToneClass(amount) {
  const value = Number(amount);
  if (value > 0) return 'is-positive';
  if (value < 0) return 'is-negative';
  return 'is-neutral';
}

function SavingsStatsPanel({ totals, previousTotals, currency, balancesHidden }) {
  const { primaryCurrency, convert } = useCurrency();
  const toDisplay = (amount) => convert(amount, currency, primaryCurrency);
  const income = Number(totals?.income || 0);
  const expense = Number(totals?.expense || 0);
  const net = Number(totals?.net || 0);

  // Standard personal-finance definition: share of income retained after
  // expenses. Negative when expenses outpace income. Null when there's
  // no income to divide by.
  const savingsRate = income > 0 ? ((income - expense) / income) * 100 : null;
  const savingsRateIsPositive = savingsRate !== null && savingsRate >= 0;
  const savingsBarWidth = savingsRate === null
    ? 0
    : Math.min(100, Math.max(0, savingsRate));
  const savingsRateDisplay = savingsRate === null
    ? '—'
    : `${savingsRate < 0 ? '-' : ''}${Math.abs(savingsRate).toFixed(0)}%`;

  const hasPrevious = previousTotals && previousTotals.net !== undefined && previousTotals.net !== null;
  const previousNet = hasPrevious ? Number(previousTotals.net) : null;
  const delta = hasPrevious ? net - previousNet : null;
  const deltaToneClass = hasPrevious ? cashFlowAmountToneClass(delta) : 'is-neutral';
  const previousNetToneClass = hasPrevious ? cashFlowAmountToneClass(previousNet) : 'is-neutral';

  return (
    <>
      <div className="cf-stat-block cf-stat-block--savings">
        <div className="cf-stat-label">Savings Rate</div>
        <div className="cf-stat-value-row">
          <div className="cf-stat-value-big">
            {savingsRateDisplay}
          </div>
          <div className="cf-stat-meta">
            {savingsRate === null ? (
              'No income in this period.'
            ) : (
              <>
                <Money amount={toDisplay(income - expense)} currency={primaryCurrency} hidden={balancesHidden} className="cf-meta-money" />
                {' / '}
                <Money amount={toDisplay(income)} currency={primaryCurrency} hidden={balancesHidden} className="cf-meta-money" />
              </>
            )}
          </div>
        </div>
        <div className="cf-stat-bar" aria-hidden="true">
          <div
            className={`cf-stat-bar-fill ${savingsRateIsPositive ? 'is-positive' : 'is-negative'}`.trim()}
            style={{ width: `${savingsBarWidth}%` }}
          />
        </div>
      </div>

      <div className="cf-stat-block">
        <div className="cf-stat-label">vs. Previous Period</div>
        {hasPrevious ? (
          <>
            <div className={`cf-stat-value-big ${deltaToneClass}`}>
              <Money amount={toDisplay(delta)} currency={primaryCurrency} hidden={balancesHidden} signed />
            </div>
            <div className="cf-stat-meta">
              Previous net:{' '}
              <Money amount={toDisplay(previousNet)} currency={primaryCurrency} hidden={balancesHidden} className={`cf-meta-money ${previousNetToneClass}`} />
            </div>
          </>
        ) : (
          <div className="cf-stat-meta">No previous-period data to compare against.</div>
        )}
      </div>
    </>
  );
}

function InvestmentActivityPanel({ activity, currency, balancesHidden, onRowClick, activeRowId = null }) {
  const { primaryCurrency, convert } = useCurrency();
  const toDisplay = (amount) => convert(amount, currency, primaryCurrency);
  const [collapsed, setCollapsed] = usePersistentPanelCollapsed('cash-flow:money-movement');
  const open = !collapsed;
  if (!activity) return null;
  // Each box maps to one structural category leaf (by seed_key). Resolve the
  // matching breakdown entry so clicking a box opens that exact category in the
  // tray — these are direct, leaf-level categories (no sub-categories), so they
  // behave like an Income leaf row, not a Spending family rollup.
  const bySeed = {};
  (activity.breakdown || []).forEach((e) => {
    if (e.seed_key && !(e.seed_key in bySeed)) bySeed[e.seed_key] = e;
  });
  // Dividends/Interest are income (income donut + net) and Withholding Tax is an
  // expense (Taxes), so none of them belong here — Money Movement is the
  // non-income/non-expense rollup: capital trades + debt principal only. One flat
  // row of tiles, no group headers.
  const rows = [
    { label: 'Buys', value: activity.buys, hint: 'Money out to buy', seedKey: 'buy' },
    { label: 'Sells', value: activity.sells, hint: 'Money in from sales', seedKey: 'sell' },
    { label: 'Future P/L', value: activity.realized_pnl, hint: 'Futures gain/loss on close', seedKey: 'realized_pnl' },
    { label: 'Deposits', value: activity.deposits, hint: 'Money moved in', seedKey: 'deposit' },
    { label: 'Withdrawals', value: activity.withdrawals, hint: 'Money moved out', seedKey: 'withdrawal' },
    { label: 'CC Payments', value: activity.cc_payments, hint: 'Card paydowns', seedKey: 'cc_payment' },
    { label: 'Loan Payments', value: activity.loan_payments, hint: 'Loan paydowns', seedKey: 'loan_payment' },
    { label: 'Loan Advances', value: activity.loan_advances, hint: 'Loans taken', seedKey: 'loan_advance' },
  ]
    .map((r) => ({
      ...r,
      value: Number(r.value || 0),
      category: bySeed[r.seedKey] || null,
    }));

  return (
    <section className={`panel-shell cf-panel cf-invest-panel ${open ? 'is-open' : ''}`.trim()}>
      <button
        type="button"
        className="cf-panel-header"
        onClick={() => setCollapsed((p) => !p)}
        aria-expanded={open}
      >
        <span className="cf-panel-chevron" aria-hidden="true">
          <TriangleIcon direction={open ? 'down' : 'right'} />
        </span>
        <span className="cf-panel-title">Money Movement</span>
      </button>
      {open && (
        <div className="cf-panel-body cf-invest-body">
          <div className="cf-invest-tiles">
            {rows.map((r) => {
              const hasValue = Math.abs(r.value) > 0.005;
              // Future P/L has no backing category (it's summed from the
              // realized_pnl column on futures trades), so give it a synthetic
              // detail row keyed by seed_key; every other tile uses its category.
              const detailRow = r.category
                || (r.seedKey === 'realized_pnl'
                  ? { category_id: null, name: 'Future P/L', seed_key: 'realized_pnl' }
                  : null);
              const clickable = Boolean(hasValue && onRowClick && detailRow);
              const rowKey = detailRow ? (detailRow.category_id ?? detailRow.seed_key) : null;
              const active = Boolean(hasValue && rowKey != null && rowKey === activeRowId);
              return (
                <div
                  className={`cf-invest-row ${hasValue ? '' : 'is-muted'} ${clickable ? 'is-clickable' : ''} ${active ? 'is-active' : ''}`.trim()}
                  key={r.label}
                  onClick={clickable ? () => onRowClick(detailRow) : undefined}
                  role={clickable ? 'button' : undefined}
                  tabIndex={clickable ? 0 : undefined}
                  onKeyDown={clickable ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRowClick(detailRow); } } : undefined}
                >
                  <div className="cf-invest-label">{r.label}</div>
                  <div className={`cf-invest-value ${r.value >= 0 ? 'is-positive' : 'is-negative'}`}>
                    <Money amount={toDisplay(r.value)} currency={primaryCurrency} hidden={balancesHidden} signed className="cf-invest-money" />
                  </div>
                  <div className="cf-invest-hint">{r.hint}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}

function NeedsReviewPanel({
  review,
  balancesHidden,
  startDate,
  endDate,
  accountIds,
  noSourcesSelected,
  refreshVersion,
  activeTransactionId,
  rowsRef,
  loadCategories,
  onTransactionClick,
}) {
  const [collapsed, setCollapsed] = usePersistentPanelCollapsed('cash-flow:needs-review', false);
  const [transactions, setTransactions] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [retryVersion, setRetryVersion] = useState(0);
  const count = Number(review?.transaction_count || 0);
  const categoryId = review?.category_id;
  const applicable = count > 0 && categoryId != null;
  const open = !collapsed;

  useEffect(() => {
    if (!applicable || !open) return undefined;
    const params = new URLSearchParams();
    params.set('category_ids', String(categoryId));
    if (startDate) params.set('start_date', startDate);
    if (endDate) params.set('end_date', endDate);
    if (noSourcesSelected) {
      params.set('account_ids', '-1');
    } else {
      const selectedIds = Array.from(accountIds || []);
      if (selectedIds.length > 0) params.set('account_ids', selectedIds.join(','));
    }
    params.set('limit', '10000');

    let cancelled = false;
    const controller = new AbortController();
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const [response] = await Promise.all([
          fetch(`${API}/transactions?${params.toString()}`, { signal: controller.signal }),
          loadCategories(),
        ]);
        const payload = await readTransactionCollectionResponse(response, {
          label: 'Needs Review transactions',
        });
        if (!cancelled) setTransactions(payload.transactions);
      } catch (err) {
        if (!cancelled && err?.name !== 'AbortError') {
          setError(err.message || 'Needs Review transactions could not be loaded.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [accountIds, applicable, categoryId, endDate, loadCategories, noSourcesSelected, open, refreshVersion, retryVersion, startDate]);

  if (!applicable) return null;

  return (
    <section className={`panel-shell cf-panel cf-needs-review-panel ${open ? 'is-open' : ''}`.trim()} aria-labelledby="cash-flow-needs-review-title">
      <button
        type="button"
        className="cf-panel-header"
        onClick={() => setCollapsed((value) => !value)}
        aria-expanded={open}
      >
        <span className="cf-panel-chevron" aria-hidden="true">
          <TriangleIcon direction={open ? 'down' : 'right'} />
        </span>
        <span className="cf-panel-title" id="cash-flow-needs-review-title">Uncategorized Transactions (Needs Review)</span>
      </button>
      {open && (
        <div className="cf-panel-body cf-needs-review-body">
          {error ? (
            <AppStatusNotice
              title="Needs Review unavailable"
              message={error}
              actionLabel="Retry"
              onAction={() => setRetryVersion((value) => value + 1)}
            />
          ) : (
            <RecentTransactionsTable
              transactions={transactions || []}
              status={loading && transactions === null ? 'loading' : 'idle'}
              balancesHidden={balancesHidden}
              showCategory={false}
              ariaLabel="Transactions needing review"
              emptyLabel="No transactions need review."
              loadingLabel="Loading transactions..."
              activeTransactionId={activeTransactionId}
              rowsRef={rowsRef}
              onTransactionClick={onTransactionClick}
            />
          )}
        </div>
      )}
    </section>
  );
}

function RecurringPanel({ recurring, currency, balancesHidden, onChanged }) {
  const [busyId, setBusyId] = useState(null);
  const [actionError, setActionError] = useState(null);
  const series = recurring || [];

  const runAction = useCallback(async (id, action) => {
    setBusyId(id);
    setActionError(null);
    try {
      const response = await fetch(`${API}/recurring/${id}/${action}`, { method: 'POST' });
      await readJsonResponse(response, {
        label: 'Recurring-series update',
        validate: (value) => isPlainObject(value) && value.status === 'ok',
      });
      if (onChanged) onChanged();
    } catch (err) {
      setActionError(err.message || 'The recurring-series update could not be saved.');
    } finally {
      setBusyId(null);
    }
  }, [onChanged]);

  if (series.length === 0) {
    return (
      <section className="panel-shell cf-panel">
        <div className="cf-panel-header is-static">
          <span className="cf-panel-title">Recurring Expenses</span>
        </div>
        <div className="cf-empty">No recurring expenses detected yet.</div>
      </section>
    );
  }

  return (
    <section className="panel-shell cf-panel is-open">
      <div className="cf-panel-header is-static">
        <span className="cf-panel-title">Recurring Expenses</span>
      </div>
      <div className="cf-panel-body">
        <AppStatusNotice
          title="Recurring expense was not updated"
          message={actionError}
          onDismiss={() => setActionError(null)}
        />
        <div className="cf-recurring-list">
          {series.map((s) => {
            // Stable "current recurring expenses" view: always the most recent
            // ACTUAL charge (`last_amount` @ `last_seen_date`) — never a
            // projected "due" row, and independent of the page's selected month.
            const dueLabel = s.last_seen_date ? `Charged ${formatShortDate(s.last_seen_date)}` : '';
            return (
              <div className="cf-recurring-row" key={s.id}>
                <span className="cf-recurring-cadence">{CADENCE_LABELS[s.cadence] || s.cadence}</span>
                {s.category ? (
                  <span className="cf-recurring-pill">
                    <CategoryPill category={s.category} size="sm" />
                  </span>
                ) : <span />}
                <div className="cf-recurring-desc-cell">
                  <span className="cf-recurring-description">{s.name}</span>
                  {(s.institution_name || s.account_name) ? (
                    <span className="cf-recurring-source">
                      {s.institution_name ? <InstitutionLogo name={s.institution_name} size={16} /> : null}
                      {s.account_name ? <span className="cf-recurring-account">{s.account_name}</span> : null}
                    </span>
                  ) : null}
                </div>
                <span className="cf-recurring-due is-charged">{dueLabel}</span>
                <span className={`cf-recurring-amount ${s.last_amount < 0 ? 'is-negative' : 'is-positive'}`}>
                  <span className="cf-amount-native">
                    <Money amount={s.last_amount} currency={s.currency || currency} hidden={balancesHidden} className="cf-recurring-money" />
                    {!balancesHidden && s.currency && s.currency !== currency ? (
                      <span className="cf-amount-native-code"> {s.currency}</span>
                    ) : null}
                  </span>
                  {!balancesHidden && s.currency && s.currency !== currency && s.last_amount_primary != null ? (
                    <FitMoney
                      className="cf-amount-converted"
                      full={`(${formatMoney(s.last_amount_primary, currency)} ${currency})`}
                      compact={`(${formatCompactMoney(s.last_amount_primary, currency)} ${currency})`}
                    />
                  ) : null}
                </span>
                <button
                  type="button"
                  className="cf-recurring-action cf-recurring-dismiss"
                  data-tooltip="Not recurring — dismiss"
                  aria-label="Not recurring — dismiss"
                  disabled={busyId === s.id}
                  onClick={() => runAction(s.id, 'dismiss')}
                >
                  ×
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function TrendPanel({ trend, currency, balancesHidden }) {
  const [collapsed, setCollapsed] = usePersistentPanelCollapsed('cash-flow:12-month-trend');
  const { colors, chartColors } = useTheme();
  const open = !collapsed;
  const trendOption = useMemo(
    () => buildCfTrendOption(trend || [], { currency, balancesHidden, colors, chartColors }),
    [balancesHidden, chartColors, colors, currency, trend],
  );
  if (!trend || trend.length === 0) return null;
  return (
    <section className={`panel-shell cf-panel ${open ? 'is-open' : ''}`.trim()}>
      <button
        type="button"
        className="cf-panel-header"
        onClick={() => setCollapsed((p) => !p)}
        aria-expanded={open}
      >
        <span className="cf-panel-chevron" aria-hidden="true">
          <TriangleIcon direction={open ? 'down' : 'right'} />
        </span>
        <span className="cf-panel-title">12-Month Trend</span>
      </button>
      {open && (
        <div className="cf-panel-body">
          <EChart option={trendOption} height={260} />
        </div>
      )}
    </section>
  );
}

export default CashFlow;
