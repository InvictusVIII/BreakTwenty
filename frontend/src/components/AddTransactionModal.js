import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { API } from '../config';
import BrandName from './BrandName';
import CategoryPicker from './CategoryPicker';
import ControlChevron from './ControlChevron';
import Dropdown from './Dropdown';
import SingleDatePicker from './SingleDatePicker';
import MoneyInput from './MoneyInput';
import TwemojiIcon from './TwemojiIcon';
import { SUPPORTED_CURRENCIES } from '../constants/currencies';
import { todayLocalDateValue } from '../utils/date';
import { loadTransactionCategories } from '../utils/transactionCategories';
import AccountTypeBadge from './AccountTypeBadge';
import './AddTransactionModal.css';

// Cash is physical fiat — crypto lives in crypto wallets/exchanges, not your wallet.
const CURRENCY_OPTIONS = SUPPORTED_CURRENCIES.map((c) => ({ value: c, label: c }));
const DATASET_OPTIONS = [
  { value: 'transactions', label: 'Transactions' },
  { value: 'balances', label: 'Balance history' },
];
// Transfer "paydown" categories surfaced only for manual LIABILITY accounts (loans / cards),
// so a repayment or draw can be recorded and tracked in the Cash Flow payments activity.
const LIABILITY_PAYMENT_SEED_KEYS = ['cc_payment', 'loan_payment', 'loan_advance'];

const accountLabel = (a) => `${a.institution || 'Manual'} — ${a.name}`;

// Unified "Add Transactions" entry. Manual mode (default) posts a hand-entered transaction
// to the wallet Cash holder (per-currency, created on demand) or a user manual-institution
// account; import mode loads a CSV (transactions or balance history) into a manual
// institution. Synced accounts are provider-authoritative and excluded from both. Posts
// /transactions/manual (manual) or /import/account/preview|commit (import).
function AddTransactionModal({ accounts = [], onClose, onCreated }) {
  // Manual institutions are the only non-cash targets for manual entry + the only import targets.
  const manualAccounts = useMemo(
    () => accounts.filter((a) => a.provider === 'manual_custom'),
    [accounts]
  );

  const [mode, setMode] = useState('manual'); // 'manual' | 'import'

  // ---- manual entry ----
  const targetOptions = useMemo(
    () => [
      { value: 'cash', label: 'Cash' },
      ...manualAccounts.map((a) => ({ value: `acct:${a.id}`, label: accountLabel(a) })),
    ],
    [manualAccounts]
  );
  const [target, setTarget] = useState('cash');
  const [currency, setCurrency] = useState('CAD');
  const isCashTarget = target === 'cash';
  const selectedManualAccount = useMemo(
    () => (isCashTarget ? null : manualAccounts.find((a) => `acct:${a.id}` === target) || null),
    [isCashTarget, target, manualAccounts]
  );
  const activeCurrency = isCashTarget ? currency : String(selectedManualAccount?.currency || 'CAD').toUpperCase();

  const [categories, setCategories] = useState([]);
  const [categoryId, setCategoryId] = useState(null);
  const [amount, setAmount] = useState('');
  const [date, setDate] = useState(todayLocalDateValue());
  const [description, setDescription] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerAnchor, setPickerAnchor] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [successMsg, setSuccessMsg] = useState(null);
  const categoryButtonRef = useRef(null);

  const fetchCategories = useCallback(async () => {
    try {
      setCategories(await loadTransactionCategories());
    } catch {
      setCategories([]);
    }
  }, []);
  useEffect(() => {
    let cancelled = false;

    async function loadInitialCategories() {
      try {
        const list = await loadTransactionCategories();
        if (!cancelled) setCategories(list);
      } catch {
        if (!cancelled) setCategories([]);
      }
    }

    void loadInitialCategories();
    return () => {
      cancelled = true;
    };
  }, []);

  // After a manual add, flash a success screen then return to the (reset) form — the modal
  // stays open so several transactions can be entered in a row.
  useEffect(() => {
    if (!successMsg) return undefined;
    const t = setTimeout(() => setSuccessMsg(null), 1600);
    return () => clearTimeout(t);
  }, [successMsg]);

  // Manual transactions are money in / money out only — restrict to income/expense leaves.
  // For a manual LIABILITY account, also surface the transfer paydown categories so a loan /
  // credit-card repayment (or draw) can be recorded and tracked as such.
  const liabilityTarget = !isCashTarget && Boolean(selectedManualAccount?.is_liability);
  const pickableCategories = useMemo(
    () => categories.filter(
      (c) => c.parent_id === null
        || c.classification === 'income'
        || c.classification === 'expense'
        || (liabilityTarget && LIABILITY_PAYMENT_SEED_KEYS.includes(c.seed_key))
    ),
    [categories, liabilityTarget]
  );
  const selectedCategory = useMemo(
    () => pickableCategories.find((c) => c.id === categoryId) || null,
    [pickableCategories, categoryId]
  );
  const classification = selectedCategory?.classification;
  const isLoanAdvance = selectedCategory?.seed_key === 'loan_advance';
  const activeCategoryId = selectedCategory?.id ?? null;

  const numericAmount = Number(amount);
  const canSubmit = activeCategoryId != null && amount !== '' && Number.isFinite(numericAmount) && numericAmount > 0
    && Boolean(date) && (isCashTarget ? Boolean(currency) : Boolean(selectedManualAccount)) && !submitting;

  const openPicker = () => {
    if (categoryButtonRef.current) setPickerAnchor(categoryButtonRef.current.getBoundingClientRect());
    setPickerOpen(true);
  };

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    const acctName = isCashTarget ? `Cash (${currency})` : (selectedManualAccount?.name || 'account');
    const label = description.trim() ? `"${description.trim()}"` : 'Transaction';
    try {
      const resp = await fetch(`${API}/transactions/manual`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          category_id: activeCategoryId,
          // Loan Advance grows the balance owed → send negative; everything else sends the
          // magnitude (backend signs income/expense; a paydown keeps + → reduces what's owed).
          amount: isLoanAdvance ? -Math.abs(numericAmount) : Math.abs(numericAmount),
          date,
          description: description.trim() || null,
          // Cash → currency (holder created on demand); manual account → its account_id.
          ...(isCashTarget ? { currency } : { account_id: selectedManualAccount.id }),
        }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body?.detail || 'Failed to add transaction');
      }
      if (onCreated) await onCreated();
      // Keep the modal open: reset the entry fields (account + date persist for fast repeat
      // entry) and flash the success screen, which auto-returns to the form.
      setAmount('');
      setCategoryId(null);
      setDescription('');
      setSubmitting(false);
      setSuccessMsg(`${label} has been added to ${acctName}`);
    } catch (e) {
      setError(e?.message || 'Failed to add transaction');
      setSubmitting(false);
    }
  };

  // ---- file import (manual institutions only) ----
  const importAccountOptions = useMemo(
    () => manualAccounts.map((a) => ({
      value: String(a.id),
      label: (
        <span className="add-txn-acct-option">
          <AccountTypeBadge accountType={a.account_type} />
          <span className="add-txn-acct-option-name">{accountLabel(a)}</span>
        </span>
      ),
    })),
    [manualAccounts]
  );
  const [importAccountId, setImportAccountId] = useState('');
  const [importDataset, setImportDataset] = useState('transactions');
  const [importFile, setImportFile] = useState(null);
  const [importResult, setImportResult] = useState(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState(null);
  const [imported, setImported] = useState(false);
  const [showImportHelp, setShowImportHelp] = useState(false);
  const fileInputRef = useRef(null);
  const importAccountValues = useMemo(
    () => importAccountOptions.map((option) => option.value),
    [importAccountOptions]
  );
  const activeImportAccountId = importAccountValues.includes(importAccountId)
    ? importAccountId
    : (importAccountValues[0] || '');

  const resetImportPreview = () => { setImportResult(null); setImported(false); setImportError(null); };

  const runImport = async (commit) => {
    if (!activeImportAccountId || !importFile) { setImportError('Choose an account and a CSV file.'); return; }
    setImportBusy(true);
    setImportError(null);
    try {
      const form = new FormData();
      form.append('file', importFile);
      form.append('account_id', activeImportAccountId);
      form.append('dataset', importDataset);
      const resp = await fetch(`${API}/import/account/${commit ? 'commit' : 'preview'}`, { method: 'POST', body: form });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data?.detail || data?.message || 'Import failed');
      setImportResult(data);
      if (commit && data.committed) {
        setImported(true);
        if (onCreated) await onCreated();
        // Import consumed the file — clear the picker (the result line stays as
        // confirmation); choosing a fresh file resets the result via its onChange.
        setImportFile(null);
        if (fileInputRef.current) fileInputRef.current.value = '';
      }
    } catch (e) {
      setImportError(e?.message || 'Import failed');
    } finally {
      setImportBusy(false);
    }
  };

  // Conflicts/errors block a commit (the backend won't commit them either).
  const importBlocked = importResult
    ? (importResult.currency_conflicts?.length || 0) > 0 || (importResult.errors?.length || 0) > 0
    : false;

  // Leaving the import tab wipes its results + attached file so re-entering is clean.
  const switchMode = (next) => { setMode(next); setError(null); resetImportPreview(); setImportFile(null); };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className={`modal-content add-transaction-modal${mode === 'import' ? ' add-transaction-modal--wide' : ''}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3 className="modal-title">Add Transactions</h3>
          {!successMsg && <button className="modal-close" onClick={onClose}>✕</button>}
        </div>
        <div className="modal-body add-transaction-body">
          {successMsg ? (
            <div className="add-txn-success">
              <div className="modal-success-icon">✓</div>
              <p className="modal-2fa-text">{successMsg}</p>
            </div>
          ) : (
            <>
              <div className="add-txn-modes" role="tablist">
                <button
                  type="button"
                  role="tab"
                  aria-selected={mode === 'manual'}
                  className={`add-txn-mode app-control-root${mode === 'manual' ? ' is-active' : ''}`}
                  onClick={() => switchMode('manual')}
                >
                  <span className="app-control-label">Add manually</span>
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={mode === 'import'}
                  className={`add-txn-mode app-control-root${mode === 'import' ? ' is-active' : ''}`}
                  onClick={() => switchMode('import')}
                >
                  <span className="app-control-label">Import from file</span>
                </button>
              </div>

              {mode === 'manual' && (
                <>
                  <p className="add-txn-mode-hint">Add manual transactions for available accounts.</p>
                  <div className="add-txn-manual-grid">
                    <div className="settings-field add-txn-field-account add-txn-span-full">
                      <label>Account</label>
                      <Dropdown value={target} options={targetOptions} onChange={setTarget} ariaLabel="Account" />
                    </div>

                    <div className="settings-field add-txn-field-amount">
                      <label>Amount</label>
                      <MoneyInput
                        className="add-txn-input add-txn-amount-input amount-input"
                        placeholder="0.00"
                        value={amount}
                        onChange={setAmount}
                      />
                      {selectedCategory && (
                        <span className="add-txn-sign-hint">
                          {classification === 'income' ? 'Money in — counts as income'
                            : classification === 'expense' ? 'Money out — counts as expense'
                            : isLoanAdvance ? 'Increases the balance owed'
                            : 'Reduces the balance owed'}
                        </span>
                      )}
                    </div>

                    <div className="settings-field add-txn-field-currency">
                      <label>Currency</label>
                      <Dropdown
                        className="currency-dropdown"
                        value={activeCurrency}
                        options={isCashTarget ? CURRENCY_OPTIONS : [{ value: activeCurrency, label: activeCurrency }]}
                        onChange={setCurrency}
                        ariaLabel="Currency"
                        disabled={!isCashTarget}
                      />
                    </div>

                    <div className="settings-field add-txn-field-category add-txn-span-full">
                      <label>Category</label>
                      <button
                        type="button"
                        ref={categoryButtonRef}
                        className="add-txn-input add-txn-category-trigger app-control-root"
                        aria-haspopup="dialog"
                        aria-expanded={pickerOpen}
                        onClick={openPicker}
                      >
                        <span className="add-txn-category-value app-control-label">
                          {selectedCategory ? (
                            <span className="add-txn-category-chip">
                              <TwemojiIcon emoji={selectedCategory.icon} set={selectedCategory.icon_set} size={16} />
                              <span>{selectedCategory.name}</span>
                            </span>
                          ) : (
                            <span className="add-txn-category-placeholder">Choose category…</span>
                          )}
                        </span>
                        <span className="add-txn-category-chevron app-control-chevron" aria-hidden="true">
                          <ControlChevron />
                        </span>
                      </button>
                      <CategoryPicker
                        isOpen={pickerOpen}
                        onClose={() => setPickerOpen(false)}
                        currentCategoryId={activeCategoryId}
                        onSelect={(id) => setCategoryId(id)}
                        categories={pickableCategories}
                        anchorRect={pickerAnchor}
                        onCategoriesChanged={fetchCategories}
                        placement="right"
                        crossAlign="center"
                      />
                    </div>

                    <div className="settings-field add-txn-field-date">
                      <label>Date</label>
                      <SingleDatePicker value={date} onChange={setDate} ariaLabel="Date" />
                    </div>

                    <div className="settings-field add-txn-field-description">
                      <label>Description</label>
                      <input
                        className="add-txn-input"
                        type="text"
                        placeholder="e.g. Coffee, cash tip…"
                        value={description}
                        maxLength={140}
                        onChange={(e) => setDescription(e.target.value)}
                      />
                    </div>
                  </div>

                  {error && <p className="modal-error-block">{error}</p>}

                  <button type="button" className="btn-primary add-txn-submit app-control-root" disabled={!canSubmit} onClick={handleSubmit}>
                    <span className="app-control-label">{submitting ? 'Adding…' : 'Add Transaction'}</span>
                  </button>
                </>
              )}

              {mode === 'import' && (
                manualAccounts.length === 0 ? (
                  <p className="add-txn-import-empty">
                    Importing data from a file adds transactions and balance history to a manually added institution. Add one from “Add to your Net Worth” first — cash is entered by hand.
                  </p>
                ) : (
                  <>
                    <div className="add-txn-import-grid">
                      <div className="settings-instructions-dropdown add-txn-span-full">
                        <button
                          type="button"
                          className={`app-instructions ${showImportHelp ? 'is-open' : ''}`.trim()}
                          aria-expanded={showImportHelp}
                          onClick={() => setShowImportHelp((v) => !v)}
                        >
                          <span className="app-instructions-label">Import Instructions</span>
                          <span className={`app-instructions-chevron modal-toggle-icon ${showImportHelp ? 'is-open' : ''}`.trim()}>&#9660;</span>
                        </button>
                        {showImportHelp && (
                          <div className="settings-instructions">
                            <p className="modal-desc">
                              Upload a CSV exported from your bank or built by hand. The first row is the column
                              headers (case-insensitive, any order); one row per record after that.
                            </p>
                            <p className="modal-desc add-txn-csv-example">
                              Example first row (header):
                              <code>Date | Amount | Description | Category</code>
                            </p>
                            <h4>Transactions</h4>
                            <ol>
                              <li><span dangerouslySetInnerHTML={{ __html: '<strong>Date</strong> — YYYY-MM-DD, e.g. 2026-06-14 (required)' }} /></li>
                              <li><span dangerouslySetInnerHTML={{ __html: '<strong>Amount</strong> — positive = money in, negative = money out; up to 2 decimals, commas optional, e.g. 1234.56 (required)' }} /></li>
                              <li><span dangerouslySetInnerHTML={{ __html: '<strong>Description</strong> — transaction information (optional)' }} /></li>
                              <li><strong>Category</strong> — <code>Parent &gt; Child</code> (e.g. Food &amp; Drink &gt; Coffee), matched by name to a category you already have in <BrandName />; unknown or misspelled names fall back to auto-categorization (optional)</li>
                            </ol>
                            <h4>Balance history</h4>
                            <ol>
                              <li><span dangerouslySetInnerHTML={{ __html: '<strong>Date</strong> — YYYY-MM-DD, e.g. 2026-06-14 (required)' }} /></li>
                              <li><span dangerouslySetInnerHTML={{ __html: '<strong>Balance</strong> — the account total on that date, e.g. 1234.56 (required)' }} /></li>
                            </ol>
                            <p className="modal-desc">
                              Amounts must be in the account’s own currency. Unrecognised columns are ignored.
                            </p>
                            <p className="modal-desc">
                              Re-importing the same file updates the matching rows (matched by date + amount + description, not category) instead of duplicating them — so you can fill in categories and re-import to apply them. A category you’ve set by hand in <BrandName /> always wins and is never overwritten by a re-import.
                            </p>
                            <p className="modal-desc add-txn-import-warn">
                              Editing a matched column (date, amount, or description) makes that row import as a new transaction — edit those inside <BrandName /> instead, not in the CSV.
                            </p>
                          </div>
                        )}
                      </div>

                      <div className="settings-field add-txn-import-account">
                        <label>Account</label>
                        <Dropdown
                          value={activeImportAccountId}
                          options={importAccountOptions}
                          onChange={(v) => { setImportAccountId(v); resetImportPreview(); }}
                          ariaLabel="Import account"
                          popoverClassName="add-txn-import-account-dropdown-popover"
                        />
                      </div>

                      <div className="settings-field add-txn-import-data">
                        <label>Data</label>
                        <Dropdown
                          value={importDataset}
                          options={DATASET_OPTIONS}
                          onChange={(v) => { setImportDataset(v); resetImportPreview(); }}
                          ariaLabel="Dataset"
                        />
                      </div>

                      <div className="settings-field add-txn-import-file add-txn-span-full">
                        <input
                          ref={fileInputRef}
                          type="file"
                          accept=".csv,text/csv"
                          className="modal-upload-input-hidden"
                          onChange={(e) => { setImportFile(e.target.files?.[0] || null); resetImportPreview(); }}
                        />
                        <div className="add-txn-file-row">
                          <span className="add-txn-file-label">CSV File:</span>
                          <button type="button" className="file-pick-btn app-control-root" onClick={() => { if (fileInputRef.current) fileInputRef.current.value = ''; fileInputRef.current?.click(); }}>
                            <span className="app-control-label">Choose file</span>
                          </button>
                          {importFile && <span className="add-txn-file-name">{importFile.name}</span>}
                        </div>
                      </div>
                    </div>

                    <div className="add-txn-import-actions">
                      <button type="button" className="btn-secondary app-control-root" disabled={importBusy || !importFile} onClick={() => runImport(false)}>
                        <span className="app-control-label">{importBusy ? 'Checking…' : 'Preview'}</span>
                      </button>
                      <button type="button" className="btn-primary app-control-root" disabled={importBusy || !importFile || importBlocked} onClick={() => runImport(true)}>
                        <span className="app-control-label">{importBusy ? 'Importing…' : 'Import'}</span>
                      </button>
                    </div>

                    {importResult && !imported && (
                      <div className="add-txn-preview">
                        <div className="add-txn-preview-row"><span>To add</span><strong>{importResult.inserted || 0}</strong></div>
                        <div className="add-txn-preview-row"><span>To update</span><strong>{importResult.updated || 0}</strong></div>
                        <div className="add-txn-preview-row"><span>Skipped</span><strong>{importResult.skipped || 0}</strong></div>
                      </div>
                    )}
                    {imported && importResult && (
                      <span className="modal-upload-status is-success">
                        {`Imported ${(importResult.inserted || 0) + (importResult.updated || 0)} · Skipped ${importResult.skipped || 0} · Errors ${(importResult.errors?.length || 0) + (importResult.currency_conflicts?.length || 0)}`}
                      </span>
                    )}
                    {importResult && (importResult.currency_conflicts || []).map((m, i) => <p key={`c${i}`} className="modal-error-block">{m}</p>)}
                    {importResult && (importResult.errors || []).map((m, i) => <p key={`e${i}`} className="modal-error-block">{m}</p>)}
                    {importResult && (importResult.warnings || []).map((m, i) => <p key={`w${i}`} className="add-txn-preview-warn">{m}</p>)}
                    {importError && <p className="modal-error-block">{importError}</p>}
                  </>
                )
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default AddTransactionModal;
