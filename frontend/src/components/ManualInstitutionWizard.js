import React, { useState, useRef, useEffect } from 'react';
import { MdAdd, MdDelete, MdArrowBack } from 'react-icons/md';
import Dropdown from './Dropdown';
import MoneyInput from './MoneyInput';
import SingleDatePicker from './SingleDatePicker';
import { API } from '../config';
import { getInstitutionSuccessMessage } from '../constants/providers';
import { CURRENCY_OPTIONS } from '../constants/currencies';
import { getAppNow } from '../utils/appClock';
import './ManualInstitutionWizard.css';

// Manual institutions are financial-only (assets/debts have their own Add-to-Net-Worth
// tiles), so the institution type is just Banking & Investing vs Crypto — mapped to the
// catalog's bank_brokerage / crypto_wallet categories.
const INSTITUTION_TYPES = [
  { value: 'bank_brokerage', label: 'Banking & Investing' },
  { value: 'crypto_wallet', label: 'Crypto' },
];

// Canadian-primary account types (+ common US). Category + asset/liability sign are
// derived from these on the backend/Accounts page, so the wizard collects only the type.
export const BANKING_ACCOUNT_TYPES = [
  { value: 'cash', label: 'Cash' },
  { value: 'chequing', label: 'Chequing' },
  { value: 'savings', label: 'Savings' },
  { value: 'tfsa', label: 'TFSA' },
  { value: 'rrsp', label: 'RRSP' },
  { value: 'resp', label: 'RESP' },
  { value: 'rdsp', label: 'RDSP' },
  { value: 'fhsa', label: 'FHSA' },
  { value: 'lira', label: 'LIRA' },
  { value: 'lrsp', label: 'LRSP' },
  { value: 'rrif', label: 'RRIF' },
  { value: 'lif', label: 'LIF' },
  { value: 'lrif', label: 'LRIF' },
  { value: 'prif', label: 'PRIF' },
  { value: 'rpp', label: 'RPP' },
  { value: 'dpsp', label: 'DPSP' },
  { value: 'spp', label: 'SPP' },
  { value: 'margin', label: 'Margin' },
  { value: 'nreg', label: 'Non-Registered' },
  { value: '401k', label: '401(k)' },
  { value: 'ira', label: 'IRA' },
  { value: 'roth_ira', label: 'Roth IRA' },
  { value: 'credit_card', label: 'Credit Card' },
  { value: 'line_of_credit', label: 'Line of Credit' },
  { value: 'heloc', label: 'HELOC' },
  { value: 'loan', label: 'Loan' },
  { value: 'mortgage', label: 'Mortgage' },
  { value: 'student_loan', label: 'Student Loan' },
  { value: 'auto_loan', label: 'Auto Loan' },
];

export const CRYPTO_ACCOUNT_TYPES = [
  { value: 'crypto', label: 'Crypto' },
  { value: 'cash', label: 'Cash' },
];

const CURRENCIES = CURRENCY_OPTIONS;
const MANUAL_INSTITUTION_ICON_ACCEPT_TYPES = new Set(['image/png', 'image/svg+xml', 'image/jpeg', 'image/webp']);
export const MANUAL_INSTITUTION_ICON_HINT = 'Use a square PNG, SVG, JPEG, or WebP under 1 MB.';

export const validateManualInstitutionIconFile = (file) => {
  if (!file) return '';
  if (!MANUAL_INSTITUTION_ICON_ACCEPT_TYPES.has(file.type)) return 'Icon must be a PNG, SVG, JPEG, or WebP image.';
  if (file.size > 1024 * 1024) return 'Icon too large (max 1 MB).';
  return '';
};

const typeOptionsFor = (instType) => (instType === 'crypto_wallet' ? CRYPTO_ACCOUNT_TYPES : BANKING_ACCOUNT_TYPES);
const defaultTypeFor = (instType) => (instType === 'crypto_wallet' ? 'crypto' : 'chequing');

// Local-timezone YYYY-MM-DD (the opening date defaults to today and is required).
const todayISO = () => {
  const d = getAppNow();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

let rowSeq = 0;
const newRow = (instType) => ({ key: `r${(rowSeq += 1)}`, name: '', account_type: defaultTypeFor(instType), currency: 'CAD', value: '', opening_date: todayISO() });

// Two-step wizard. Step 1: institution (name, type, optional logo). Step 2: accounts
// (name, type, currency, Account Value). Account Value seeds the account's opening
// balance (anchored today); transaction/balance CSV import lives on the separate Import
// action. Nothing persists until the final Create button.
function ManualInstitutionWizard({ onClose, onBack, onComplete }) {
  const [step, setStep] = useState(1);
  const [name, setName] = useState('');
  const [instType, setInstType] = useState('bank_brokerage');
  const [logoFile, setLogoFile] = useState(null);
  const [logoPreview, setLogoPreview] = useState(null);
  const [rows, setRows] = useState(() => [newRow('bank_brokerage')]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [logoValidationError, setLogoValidationError] = useState('');
  const [postCreateWarning, setPostCreateWarning] = useState('');
  const logoFileInputRef = useRef(null);

  // Auto-dismiss the success screen (step 3) after a brief window, like the other add modals.
  useEffect(() => {
    if (step !== 3) return undefined;
    const timer = setTimeout(() => onClose(), postCreateWarning ? 4000 : 1500);
    return () => clearTimeout(timer);
  }, [step, onClose, postCreateWarning]);
  const submittingRef = useRef(false);

  const trimmedName = name.trim();
  const typeOptions = typeOptionsFor(instType);

  const updateRow = (key, patch) => setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const addRow = () => setRows((rs) => [...rs, newRow(instType)]);
  const removeRow = (key) => setRows((rs) => (rs.length <= 1 ? rs : rs.filter((r) => r.key !== key)));

  const handleInstTypeChange = (next) => {
    setInstType(next);
    const valid = new Set(typeOptionsFor(next).map((o) => o.value));
    setRows((rs) => rs.map((r) => (valid.has(r.account_type) ? r : { ...r, account_type: defaultTypeFor(next) })));
  };

  const handleLogoChange = (file) => {
    if (!file) return;
    const nextError = validateManualInstitutionIconFile(file);
    if (nextError) {
      setLogoValidationError(nextError);
      if (logoFileInputRef.current) logoFileInputRef.current.value = '';
      return;
    }
    if (logoPreview) URL.revokeObjectURL(logoPreview);
    setLogoFile(file);
    setLogoPreview(URL.createObjectURL(file));
    setLogoValidationError('');
    if (logoFileInputRef.current) logoFileInputRef.current.value = '';
  };
  const clearLogo = () => {
    if (logoPreview) URL.revokeObjectURL(logoPreview);
    setLogoFile(null);
    setLogoPreview(null);
    setLogoValidationError('');
    if (logoFileInputRef.current) logoFileInputRef.current.value = '';
  };

  const validateStep1 = () => {
    if (!trimmedName) return 'Institution name is required.';
    return validateManualInstitutionIconFile(logoFile);
  };
  const validateStep2 = () => {
    if (rows.some((r) => !r.name.trim())) return 'Each account needs a name.';
    if (rows.some((r) => r.value === '' || r.value == null)) return 'Each account needs a value.';
    if (rows.some((r) => !r.opening_date)) return 'Each account needs an opening date.';
    const lowered = rows.map((r) => r.name.trim().toLowerCase());
    if (new Set(lowered).size !== lowered.length) return 'Account names must be unique.';
    return '';
  };

  const handleContinue = () => {
    const message = validateStep1();
    if (message) { setError(message); return; }
    setError('');
    setStep(2);
  };

  const handleSave = async () => {
    const m1 = validateStep1();
    if (m1) { setError(m1); setStep(1); return; }
    const m2 = validateStep2();
    if (m2) { setError(m2); return; }
    if (submittingRef.current) return;  // guard against a double-click creating two institutions
    submittingRef.current = true;
    setBusy(true);
    setError('');
    setPostCreateWarning('');
    try {
      const accounts = rows.map((r) => ({
        name: r.name.trim(),
        account_type: r.account_type,
        currency: r.currency,
        opening_balance: (r.value === '' || r.value == null) ? null : Number(r.value),
        opening_balance_date: r.opening_date || null,
      }));
      const resp = await fetch(`${API}/institutions/manual`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: trimmedName, category: instType, accounts }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') {
        throw new Error(data.detail || data.message || 'Could not create institution.');
      }
      const institutionId = data.institution?.id;
      let logoWarning = '';
      if (logoFile && institutionId) {
        const form = new FormData();
        form.append('file', logoFile);
        try {
          const logoResp = await fetch(`${API}/institutions/${institutionId}/logo`, { method: 'POST', body: form });
          const logoData = await logoResp.json().catch(() => ({}));
          if (!logoResp.ok || logoData.status !== 'ok') {
            logoWarning = logoData.detail || logoData.message || `Icon upload failed (${logoResp.status}).`;
          }
        } catch (logoError) {
          logoWarning = logoError.message || 'Icon upload failed.';
        }
      }
      setPostCreateWarning(logoWarning);
      if (onComplete) onComplete();
      setStep(3);
    } catch (e) {
      setError(e.message || 'Could not create institution.');
    } finally {
      setBusy(false);
      submittingRef.current = false;
    }
  };

  return (
    <div className="modal-overlay">
      <div className={`modal-content manual-inst-modal ${step === 1 ? 'manual-inst-modal--intro' : ''}`.trim()} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          {step !== 3 && (
            <button
              className="add-networth-back"
              onClick={() => { if (step === 2) { setError(''); setStep(1); } else { onBack(); } }}
              aria-label="Back"
            >
              <MdArrowBack size={20} />
            </button>
          )}
          <h3 className="modal-title">{step === 1 ? 'Add Manual Institution' : step === 2 ? `Accounts — ${trimmedName}` : 'Added'}</h3>
          {step !== 3 && <button className="modal-close" onClick={onClose}>✕</button>}
        </div>

        <div className="modal-body manual-inst-body">
          {step === 1 && (
            <>
              <label className="manual-inst-field">
                <span className="manual-inst-label">Institution name</span>
                <input
                  className="manual-inst-input"
                  value={name}
                  placeholder="e.g. Scotiabank"
                  onChange={(e) => setName(e.target.value)}
                  autoFocus
                />
              </label>
              <label className="manual-inst-field">
                <span className="manual-inst-label">Type</span>
                <Dropdown
                  className="manual-inst-type-select"
                  value={instType}
                  options={INSTITUTION_TYPES}
                  fitToOptions
                  ariaLabel="Institution type"
                  onChange={handleInstTypeChange}
                />
              </label>
              <div className="manual-inst-field">
                <span className="manual-inst-label">Institution icon</span>
                <div className="manual-inst-logo-row">
                  <span className="manual-inst-logo-preview">
                    {logoPreview
                      ? <img src={logoPreview} alt="Logo preview" />
                      : <span className="manual-inst-logo-initial">{(trimmedName[0] || '?').toUpperCase()}</span>}
                  </span>
                  <label className="file-pick-btn">
                    {logoFile ? 'Replace icon' : 'Add icon'}
                    <input
                      ref={logoFileInputRef}
                      type="file"
                      accept="image/png,image/svg+xml,image/jpeg,image/webp"
                      onChange={(e) => handleLogoChange(e.target.files?.[0] || null)}
                    />
                  </label>
                  {logoFile && (
                    <button type="button" className="manual-inst-logo-remove button-shell-opt-out" onClick={clearLogo}>Remove</button>
                  )}
                  {logoFile && <span className="manual-inst-logo-name">{logoFile.name}</span>}
                </div>
                <span className="manual-inst-logo-hint">{MANUAL_INSTITUTION_ICON_HINT}</span>
                {logoValidationError && <div className="manual-inst-error">{logoValidationError}</div>}
              </div>
              {error && <div className="manual-inst-error">{error}</div>}
            </>
          )}

          {step === 2 && (
            <>
              <div className="manual-inst-accounts">
                <div className="manual-inst-acct-head">
                  <span>Account Name</span>
                  <span>Type</span>
                  <span>Currency</span>
                  <span>Account Value</span>
                  <span className="manual-inst-date-heading">Opening date</span>
                  <span aria-hidden="true" />
                </div>
                {rows.map((r) => (
                  <div className="manual-inst-acct-row" key={r.key}>
                    <input
                      className="manual-inst-input"
                      value={r.name}
                      placeholder="Account name"
                      onChange={(e) => updateRow(r.key, { name: e.target.value })}
                    />
                    <Dropdown
                      className="manual-inst-type"
                      value={r.account_type}
                      options={typeOptions}
                      fitToOptions
                      ariaLabel="Account type"
                      onChange={(v) => updateRow(r.key, { account_type: v })}
                    />
                    <Dropdown
                      className="manual-inst-ccy currency-dropdown"
                      value={r.currency}
                      options={CURRENCIES}
                      ariaLabel="Currency"
                      onChange={(v) => updateRow(r.key, { currency: v })}
                    />
                    <MoneyInput
                      className="manual-inst-input amount-input"
                      value={r.value}
                      placeholder="0.00"
                      aria-label="Account value"
                      onChange={(v) => updateRow(r.key, { value: v })}
                    />
                    <SingleDatePicker
                      className="manual-inst-date-picker"
                      size="compact"
                      value={r.opening_date}
                      onChange={(v) => updateRow(r.key, { opening_date: v })}
                      ariaLabel="Opening date"
                      popoverPlacement="right"
                      popoverCrossAlign="center"
                    />
                    <button
                      type="button"
                      className="settings-asset-delete"
                      aria-label="Remove account"
                      onClick={() => removeRow(r.key)}
                      disabled={rows.length <= 1}
                    >
                      <MdDelete size={18} aria-hidden="true" />
                    </button>
                  </div>
                ))}
                <button type="button" className="manual-inst-add button-shell-opt-out" onClick={addRow}>
                  <MdAdd size={16} /> Add Account
                </button>
              </div>
              <p className="manual-inst-hint">
                Account Value is the current balance. Import transactions or balance history later from the Import action.
              </p>
              {error && <div className="manual-inst-error">{error}</div>}
            </>
          )}

          {step === 3 && (
            <div className="manual-inst-success">
              <div className="modal-success-icon">&#10003;</div>
              <p className="modal-2fa-text">{getInstitutionSuccessMessage({ name: trimmedName }, 'add')}</p>
              {postCreateWarning && <div className="manual-inst-error">{postCreateWarning}</div>}
            </div>
          )}
        </div>

        {step !== 3 && (
          <div className="manual-inst-footer">
            {step === 1 && (
              <button type="button" className="btn-primary app-control-root" onClick={handleContinue}>
                <span className="app-control-label">Continue</span>
              </button>
            )}
            {step === 2 && (
              <button type="button" className="btn-primary app-control-root" onClick={handleSave} disabled={busy}>
                <span className="app-control-label">{busy ? 'Saving…' : `Create ${trimmedName || 'institution'}`}</span>
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default ManualInstitutionWizard;
