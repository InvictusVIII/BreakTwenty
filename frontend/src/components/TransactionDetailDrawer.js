import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { MdCheck, MdRestartAlt } from 'react-icons/md';
import { SideDetailDrawerPanel } from './SideDetailDrawer';
import CategoryPicker from './CategoryPicker';
import CategoryPill from './CategoryPill';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import { API } from '../config';
import { formatSignedDollarAmount } from '../utils/format';
import { formatLongDateValue } from '../utils/date';
import './TransactionDetailDrawer.css';

function joinClassNames(...values) {
  return values.filter(Boolean).join(' ');
}

const TRANSITION_DURATION_MS = 360;
const CATEGORY_PICKER_VERTICAL_NUDGE_PX = 108;

function isAbortError(error) {
  return error?.name === 'AbortError';
}

function TransactionDetailDrawer({
  transaction,
  animatedOpen = false,
  categories,
  excludeFromDismissRef,
  onClose,
  onSaved,
  onCategoriesChanged,
  dismissLocked = false,
}) {
  // Drawer keeps its own copy of the transaction so we can run an exit
  // animation after the parent has already cleared its prop.
  const [internalTransaction, setInternalTransaction] = useState(transaction || null);

  const [userDescription, setUserDescription] = useState('');
  const [userNotes, setUserNotes] = useState('');
  const [categoryId, setCategoryId] = useState(null);
  const [applyDescriptionToSimilar, setApplyDescriptionToSimilar] = useState(false);
  const [applyCategoryToSimilar, setApplyCategoryToSimilar] = useState(false);
  const [similarCount, setSimilarCount] = useState(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [pendingDefault, setPendingDefault] = useState(false);
  const [defaultLoading, setDefaultLoading] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');

  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerAnchorRef = useRef(null);
  const [pickerAnchorRect, setPickerAnchorRect] = useState(null);
  const shellRef = useRef(null);
  const requestGenerationRef = useRef(0);
  const activeTransactionIdRef = useRef(transaction?.id ?? null);
  const activeRequestControllersRef = useRef(new Set());
  const liveTransactionId = transaction?.id ?? null;
  const abortActiveRequests = useCallback(() => {
    activeRequestControllersRef.current.forEach((controller) => controller.abort());
    activeRequestControllersRef.current.clear();
  }, []);
  const createTransactionRequest = useCallback((transactionId) => {
    if (transactionId == null || transactionId !== liveTransactionId) return null;
    const controller = new AbortController();
    const generation = requestGenerationRef.current;
    activeRequestControllersRef.current.add(controller);
    return {
      signal: controller.signal,
      abort: () => controller.abort(),
      finish: () => activeRequestControllersRef.current.delete(controller),
      isCurrent: () => (
        !controller.signal.aborted
        && requestGenerationRef.current === generation
        && activeTransactionIdRef.current === transactionId
      ),
    };
  }, [liveTransactionId]);
  const handleClose = useCallback((event) => {
    if (dismissLocked) {
      event?.preventDefault?.();
      return;
    }
    onClose();
  }, [dismissLocked, onClose]);

  useEffect(() => {
    activeTransactionIdRef.current = transaction?.id ?? null;
    requestGenerationRef.current += 1;
    abortActiveRequests();
  }, [abortActiveRequests, transaction?.id]);

  useEffect(() => () => {
    activeTransactionIdRef.current = null;
    requestGenerationRef.current += 1;
    abortActiveRequests();
  }, [abortActiveRequests]);

  // Snapshot effect: mirror the live prop into internal state immediately on
  // open; on close, defer clearing the snapshot until after the slide-out so
  // the DOM stays mounted while the .is-open class is being removed.
  useEffect(() => {
    let cancelled = false;
    if (transaction) {
      Promise.resolve().then(() => {
        if (!cancelled) {
          setInternalTransaction(transaction);
        }
      });
      return () => {
        cancelled = true;
      };
    }
    const t = setTimeout(() => setInternalTransaction(null), TRANSITION_DURATION_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [transaction]);

  const categoriesById = useMemo(() => {
    const map = new Map();
    for (const c of categories) map.set(c.id, c);
    return map;
  }, [categories]);

  const dismissRefs = excludeFromDismissRef ? [shellRef, excludeFromDismissRef] : [shellRef];

  // Close on click outside drawer + outside the rows list (so clicking another
  // row switches transactions rather than closing).
  useDismissibleLayer({
    open: Boolean(transaction) && !pickerOpen && !deleteConfirm,
    refs: dismissRefs,
    ignoreAppChrome: true,
    onDismiss: handleClose,
    pointerEvent: 'mousedown',
    includeTouch: true,
  });

  // Sync form fields when a different transaction is selected (no re-mount).
  // Uses internalTransaction so form data stays visible through the exit animation.
  useEffect(() => {
    if (!internalTransaction) return;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (cancelled) return;
      setUserDescription(internalTransaction.user_description || '');
      setUserNotes(internalTransaction.user_notes || '');
      setCategoryId(internalTransaction.category?.id ?? null);
      setApplyDescriptionToSimilar(false);
      setApplyCategoryToSimilar(false);
      setErrorMessage('');
      setSimilarCount(null);
      // Reset transient action state so a prior row's in-flight save/delete can't
      // leak into a newly opened transaction (the drawer instance is reused).
      setSaving(false);
      setDeleting(false);
      setDeleteConfirm(false);
      setPendingDefault(false);
      setDefaultLoading(false);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [internalTransaction?.id]);

  useEffect(() => {
    if (!internalTransaction) return undefined;
    const transactionId = internalTransaction.id;
    const request = createTransactionRequest(transactionId);
    if (!request) return undefined;
    (async () => {
      try {
        const resp = await fetch(`${API}/transactions/${transactionId}/similar-count`, {
          signal: request.signal,
        });
        const data = await resp.json();
        if (request.isCurrent()) setSimilarCount(data.count ?? 0);
      } catch (error) {
        if (request.isCurrent() && !isAbortError(error)) setSimilarCount(0);
      } finally {
        request.finish();
      }
    })();
    return () => {
      request.abort();
      request.finish();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [createTransactionRequest, internalTransaction?.id]);

  const selectedCategory = categoryId ? categoriesById.get(categoryId) : null;
  const parentCategory = selectedCategory?.parent_id
    ? categoriesById.get(selectedCategory.parent_id)
    : null;

  const handleOpenPicker = useCallback(() => {
    if (pickerAnchorRef.current) {
      const r = pickerAnchorRef.current.getBoundingClientRect();
      setPickerAnchorRect({ top: r.top, left: r.left, bottom: r.bottom, right: r.right });
    }
    setPickerOpen(true);
  }, []);

  const handlePickCategory = useCallback((nextId) => {
    setCategoryId(nextId);
    setPickerOpen(false);
    // Manually choosing a category overrides a staged "Default" revert.
    setPendingDefault(false);
  }, []);

  // Toggling "Default" previews the reverted category in the pills immediately
  // (fetched, no mutation) but stages it — the change is only persisted on Save.
  // Toggling off restores the currently-saved category.
  const handleToggleDefault = useCallback(async () => {
    if (pendingDefault) {
      setPendingDefault(false);
      setCategoryId(internalTransaction?.category?.id ?? null);
      return;
    }
    if (!internalTransaction) return;
    const transactionId = internalTransaction.id;
    const request = createTransactionRequest(transactionId);
    if (!request) return;
    setDefaultLoading(true);
    setErrorMessage('');
    try {
      const resp = await fetch(`${API}/transactions/${transactionId}/default-category`, {
        signal: request.signal,
      });
      if (!request.isCurrent()) return;
      if (!resp.ok) {
        const detail = await resp.json().catch(() => null);
        throw new Error(detail?.detail || 'Failed to load default category');
      }
      const result = await resp.json();
      if (!request.isCurrent()) return;
      setCategoryId(result.category_id ?? null);
      setPendingDefault(true);
    } catch (err) {
      if (request.isCurrent() && !isAbortError(err)) {
        setErrorMessage(err.message || 'Failed to load default category');
      }
    } finally {
      if (request.isCurrent()) setDefaultLoading(false);
      request.finish();
    }
  }, [createTransactionRequest, pendingDefault, internalTransaction]);

  const handleResetDescription = useCallback(() => {
    setUserDescription('');
  }, []);

  const handleSave = useCallback(async () => {
    if (!internalTransaction) return;
    const transactionId = internalTransaction.id;
    const request = createTransactionRequest(transactionId);
    if (!request) return;
    setSaving(true);
    setErrorMessage('');
    const trimmedDescription = userDescription.trim();
    const trimmedNotes = userNotes.trim();
    const originalDescription = internalTransaction.user_description || '';
    const originalNotes = internalTransaction.user_notes || '';
    const originalCategoryId = internalTransaction.category?.id ?? null;

    const descriptionChanged = trimmedDescription !== originalDescription;
    const notesChanged = trimmedNotes !== originalNotes;
    // A staged "Default" revert is carried by reset_category on this PATCH,
    // never as a manual category change.
    const categoryChanged = !pendingDefault && categoryId !== originalCategoryId;

    const payload = {};
    if (descriptionChanged) {
      if (trimmedDescription) {
        payload.user_description = trimmedDescription;
      } else {
        payload.clear_user_description = true;
      }
    }
    if (notesChanged) {
      if (trimmedNotes) {
        payload.user_notes = trimmedNotes;
      } else {
        payload.clear_user_notes = true;
      }
    }
    if (categoryChanged) {
      if (categoryId == null) {
        payload.clear_category = true;
      } else {
        payload.category_id = categoryId;
      }
    }
    if (descriptionChanged && applyDescriptionToSimilar) {
      payload.apply_description_to_similar = true;
    }
    if (categoryChanged && applyCategoryToSimilar) {
      payload.apply_category_to_similar = true;
    }
    if (pendingDefault) {
      payload.reset_category = true;
    }

    if (Object.keys(payload).length === 0) {
      setSaving(false);
      request.finish();
      return;
    }

    try {
      const resp = await fetch(`${API}/transactions/${transactionId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: request.signal,
      });
      if (!request.isCurrent()) return;
      if (!resp.ok) {
        const errorDetail = await resp.json().catch(() => null);
        throw new Error(errorDetail?.detail || 'Save failed');
      }
      const result = await resp.json();
      if (!request.isCurrent()) return;
      const nextCategory = result.category_id != null
        ? categoriesById.get(result.category_id) || null
        : null;
      // Re-baseline the snapshot from the single committed response so text,
      // category, category source, and sign can never reflect separate saves.
      setInternalTransaction((prev) => (prev?.id === transactionId
        ? {
            ...prev,
            user_description: result.user_description ?? null,
            user_notes: result.user_notes ?? null,
            category: nextCategory,
            category_source: result.category_source ?? null,
            amount: result.amount ?? prev.amount,
          }
        : prev));
      setCategoryId(result.category_id ?? null);
      if (pendingDefault) setPendingDefault(false);
      if (onSaved) onSaved({ bulkUpdated: result.bulk_updated_count || 0 });
    } catch (err) {
      if (request.isCurrent() && !isAbortError(err)) {
        setErrorMessage(err.message || 'Save failed');
      }
    } finally {
      if (request.isCurrent()) setSaving(false);
      request.finish();
    }
  }, [createTransactionRequest, internalTransaction, userDescription, userNotes, categoryId, applyDescriptionToSimilar, applyCategoryToSimilar, pendingDefault, categoriesById, onSaved]);

  const confirmDelete = useCallback(async () => {
    if (!internalTransaction) return;
    const transactionId = internalTransaction.id;
    const request = createTransactionRequest(transactionId);
    if (!request) return;
    setDeleting(true);
    setErrorMessage('');
    try {
      const resp = await fetch(`${API}/transactions/manual/${transactionId}`, {
        method: 'DELETE',
        signal: request.signal,
      });
      if (!request.isCurrent()) return;
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body?.detail || 'Failed to delete transaction');
      }
      setDeleting(false);
      setDeleteConfirm(false);
      if (onSaved) await onSaved();
      if (!request.isCurrent()) return;
      handleClose();
    } catch (e) {
      if (request.isCurrent() && !isAbortError(e)) {
        setErrorMessage(e?.message || 'Failed to delete transaction');
        setDeleting(false);
      }
    } finally {
      request.finish();
    }
  }, [createTransactionRequest, internalTransaction, onSaved, handleClose]);

  // Transaction-detail tray uses the standard side-panel pattern:
  // position:fixed at a stable viewport top (see App.css → scoped
  // .transaction-detail-tray rule). No anchor-to-row math, no
  // ResizeObserver re-clamp, no edge cases at the top/bottom of the
  // list. The clicked row's highlight stripe + the tray title bar
  // provide the link to the row.

  if (!internalTransaction) return null;

  const dateLabel = internalTransaction.date ? formatLongDateValue(internalTransaction.date) : '';
  const accountLabel = [internalTransaction.institution_name, internalTransaction.account_name].filter(Boolean).join(' · ');
  const amountText = formatSignedDollarAmount(internalTransaction.amount, internalTransaction.currency);
  const rawDescription = internalTransaction.description || '';
  const similarHasOthers = (similarCount ?? 0) > 1;
  const manualKind = internalTransaction.manual_kind || null;
  // Cash mirror legs and opening balances are system-managed: their amount is
  // sign-locked to the neutral Cash category, so re-categorizing is blocked.
  const isSystemCashLeg = manualKind === 'cash_mirror' || manualKind === 'cash_opening';
  // User-created cash entries are removable; a mirror leg is removed by un-tagging its source.
  const isDeletable = manualKind === 'manual' || manualKind === 'cash_opening'
    || internalTransaction.institution_provider === 'manual_custom';
  // A synced row carrying a manual category override can be reverted to its initial
  // auto category. System cash legs and user-created manual entries (both have a
  // manual_kind) are excluded.
  const canResetCategory = manualKind === null && internalTransaction.category_source === 'manual';

  // Portal into .app-main; position:fixed at a stable viewport top
  // is supplied by App.css's scoped .transaction-detail-tray rule.
  // Render nothing if .app-main isn't mounted yet — the portal target
  // must exist before createPortal can attach.
  const portalTarget = typeof document !== 'undefined' ? document.querySelector('.app-main') : null;
  if (!portalTarget) {
    return null;
  }

  return createPortal((
    <div
      className={`app-edge-tray is-pinned transaction-detail-tray ${animatedOpen ? 'is-open' : ''}`.trim()}
      role="dialog"
      aria-label="Transaction details"
      aria-hidden={!animatedOpen}
    >
      <div className="app-edge-tray-backdrop" aria-hidden="true" />
      <div
        className="app-edge-tray-shell"
        ref={shellRef}
      >
        <SideDetailDrawerPanel
          meta={dateLabel}
          title={internalTransaction.display_description || rawDescription || internalTransaction.type}
          subtitle={accountLabel}
          onClose={handleClose}
          className="panel-shell transaction-detail-panel"
        >
        <div className="tx-detail-amount">
          <span className={joinClassNames(
            'tx-detail-amount-value',
            internalTransaction.amount > 0 ? 'is-positive' : internalTransaction.amount < 0 ? 'is-negative' : '',
          )}>
            {amountText}
          </span>
          <span className="tx-detail-amount-currency">{internalTransaction.currency}</span>
        </div>

        <div className="tx-detail-section">
          <label className="tx-detail-label" htmlFor="tx-detail-description">
            Description
          </label>
          <div className="tx-detail-input-row">
            <input
              id="tx-detail-description"
              type="text"
              className="tx-detail-input"
              value={userDescription}
              placeholder={rawDescription || 'Custom description'}
              onChange={(e) => setUserDescription(e.target.value)}
              maxLength={500}
            />
            {userDescription && (
              <button
                type="button"
                className="tx-detail-icon-button"
                onClick={handleResetDescription}
                data-tooltip="Reset to raw description"
                aria-label="Reset to raw description"
              >
                <MdRestartAlt size={16} />
              </button>
            )}
          </div>
          {(userDescription && userDescription.trim() !== rawDescription) && (
            <p className="tx-detail-hint">
              Raw: <span className="tx-detail-hint-mono">{rawDescription}</span>
            </p>
          )}
        </div>

        <div className="tx-detail-section">
          <label className="tx-detail-label">Category</label>
          <div className="tx-detail-category-pills" ref={pickerAnchorRef}>
            {parentCategory ? (
              <CategoryPill category={parentCategory} size="md" />
            ) : (
              <span className="tx-detail-category-pills-placeholder">—</span>
            )}
            <span className="tx-detail-category-divider" aria-hidden="true">›</span>
            <CategoryPill
              category={selectedCategory || null}
              size="md"
              onClick={isSystemCashLeg ? undefined : handleOpenPicker}
            />
          </div>
          {isSystemCashLeg && (
            <p className="tx-detail-hint">
              {manualKind === 'cash_mirror'
                ? 'Auto-created from a Cash-tagged transaction — category is locked.'
                : 'Opening cash balance — category is locked.'}
            </p>
          )}
          {canResetCategory && (
            <>
              <button
                type="button"
                className={joinClassNames('tx-detail-reset-category', 'app-control-root', pendingDefault && 'is-active')}
                onClick={handleToggleDefault}
                disabled={saving || deleting || defaultLoading}
                aria-pressed={pendingDefault}
              >
                <span className="app-control-icon" aria-hidden="true"><MdRestartAlt /></span>
                <span className="app-control-label">Default</span>
              </button>
              {pendingDefault && (
                <p className="tx-detail-hint">Reverts to the default category when you save.</p>
              )}
            </>
          )}
        </div>

        <div className="tx-detail-section">
          <label className="tx-detail-label" htmlFor="tx-detail-notes">Notes</label>
          <textarea
            id="tx-detail-notes"
            className="tx-detail-textarea"
            value={userNotes}
            placeholder="Add a private note for this transaction"
            onChange={(e) => setUserNotes(e.target.value)}
            maxLength={2000}
            rows={3}
          />
        </div>

        {similarHasOthers && (
          <div className="tx-detail-section tx-detail-apply">
            <p className="tx-detail-apply-heading">
              <span className="tx-detail-apply-count">{similarCount - 1}</span> other transaction{similarCount - 1 === 1 ? '' : 's'} share the same raw description
            </p>
            <label className="tx-detail-apply-option">
              <input
                type="checkbox"
                className="tx-detail-apply-input"
                checked={applyDescriptionToSimilar}
                onChange={(e) => setApplyDescriptionToSimilar(e.target.checked)}
              />
              <span
                className={joinClassNames('transactions-toolbar-checkbox', applyDescriptionToSimilar && 'is-selected')}
                aria-hidden="true"
              >
                {applyDescriptionToSimilar ? <MdCheck size={14} /> : null}
              </span>
              <span className="tx-detail-apply-option-label">Apply description to similar</span>
            </label>
            <label className="tx-detail-apply-option">
              <input
                type="checkbox"
                className="tx-detail-apply-input"
                checked={applyCategoryToSimilar}
                onChange={(e) => setApplyCategoryToSimilar(e.target.checked)}
              />
              <span
                className={joinClassNames('transactions-toolbar-checkbox', applyCategoryToSimilar && 'is-selected')}
                aria-hidden="true"
              >
                {applyCategoryToSimilar ? <MdCheck size={14} /> : null}
              </span>
              <span className="tx-detail-apply-option-label">Apply category to similar</span>
            </label>
          </div>
        )}

        {errorMessage && <div className="tx-detail-error">{errorMessage}</div>}

        <div className="tx-detail-actions">
          {isDeletable && (
            <button
              type="button"
              className="btn-danger tx-detail-button app-control-root"
              onClick={() => setDeleteConfirm(true)}
              disabled={deleting || saving}
            >
              <span className="app-control-label">Delete</span>
            </button>
          )}
          <button
            type="button"
            className="btn-primary tx-detail-button app-control-root"
            onClick={handleSave}
            disabled={saving || deleting}
          >
            <span className="app-control-label">{saving ? 'Saving…' : 'Save'}</span>
          </button>
        </div>
        </SideDetailDrawerPanel>

        <CategoryPicker
          isOpen={pickerOpen}
          onClose={() => setPickerOpen(false)}
          currentCategoryId={categoryId}
          categories={categories}
          anchorRect={pickerAnchorRect}
          onSelect={handlePickCategory}
          onCategoriesChanged={onCategoriesChanged}
          placement="left"
          offsetY={-CATEGORY_PICKER_VERTICAL_NUDGE_PX}
        />

      {deleteConfirm && (
        <div className="modal-overlay modal-overlay-elevated">
          <div className="modal-content modal-dialog-card modal-dialog-wide">
            <p className="modal-2fa-text modal-dialog-title">Delete this transaction?</p>
            <p className="modal-dialog-copy">This can’t be undone.</p>
            {errorMessage && <p className="modal-error-block">{errorMessage}</p>}
            <div className="modal-dialog-actions">
              <button className="btn-danger modal-dialog-action app-control-root" onClick={confirmDelete} disabled={deleting}>
                <span className="app-control-label">{deleting ? 'Deleting…' : 'Yes, delete'}</span>
              </button>
              <button
                className="btn-secondary modal-dialog-action app-control-root"
                onClick={() => setDeleteConfirm(false)}
                disabled={deleting}
              >
                <span className="app-control-label">Cancel</span>
              </button>
            </div>
          </div>
        </div>
      )}
      </div>
    </div>
  ), portalTarget);
}

export default TransactionDetailDrawer;
