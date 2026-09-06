import React, { useState, useRef, useEffect, useMemo } from 'react';
import { MdArrowBack } from 'react-icons/md';
import Dropdown from './Dropdown';
import MoneyInput from './MoneyInput';
import SingleDatePicker from './SingleDatePicker';
import { API } from '../config';
import { CURRENCY_OPTIONS } from '../constants/currencies';
import { getAssetGroupAccountTypeOptions, getInstitutionSuccessMessage } from '../constants/providers';
import './ManualInstitutionWizard.css';
import './NetWorthEntryModal.css';

// Per-bucket copy. Every tile shares ONE flow: Type → account_type, Details → name, a
// required current-value figure + currency, and an optional cost-basis point (amount + date).
// Only the labels and the type options (from getAssetGroupAccountTypeOptions) differ. Private
// investments reuse the flow reworded "Amount invested" / "Invested date"; Debt reuses it as
// "Current balance" / "Amount borrowed" / "Date borrowed" — and because the debt bucket
// creates is_liability accounts, the positive figure entered here is stored as-is and shown
// negative everywhere (the sign is display-only; see AccountBalanceCell in Accounts.js).
const DEFAULT_VALUE_LABEL = 'Current value';
const DEFAULT_COST_LABEL = 'Purchase price';
const DEFAULT_COST_DATE_LABEL = 'Purchase date';
const ASSET_ADD_COPY = {
  real_estate: { title: 'Add Real Estate', typeLabel: 'Property type', namePlaceholder: 'e.g. Primary home', bucket: 'Real Estate', cta: 'Add Property', noun: 'property' },
  vehicles: { title: 'Add Vehicle', typeLabel: 'Type', namePlaceholder: 'e.g. My car', bucket: 'Vehicles', cta: 'Add Vehicle', noun: 'vehicle' },
  valuables: { title: 'Add Valuable', typeLabel: 'Type', namePlaceholder: 'e.g. Art collection', bucket: 'Valuables', cta: 'Add Valuable', noun: 'valuable' },
  private_investments: { title: 'Add Investment', typeLabel: 'Investment type', namePlaceholder: 'Company name', bucket: 'Private Investments', cta: 'Add Investment', noun: 'investment', costLabel: 'Amount invested', costDateLabel: 'Invested date' },
  other_assets: { title: 'Add Asset', typeLabel: 'Type', namePlaceholder: 'e.g. Other asset', bucket: 'Other Assets', cta: 'Add Asset', noun: 'asset' },
  debt: { title: 'Add Debt', typeLabel: 'Debt type', namePlaceholder: 'e.g. Loan from family', bucket: 'Debt', cta: 'Add Debt', noun: 'debt', valueLabel: 'Current balance', costLabel: 'Amount borrowed', costDateLabel: 'Date borrowed' },
};

const CURRENCIES = CURRENCY_OPTIONS;

// Shared entry form for the tangible-asset Add-to-Net-Worth tiles. Creates a manual asset
// inside the connector-less bucket via POST /accounts/group — the revaluation door anchors
// the value as of today — so the asset rides the normal accounts / net-worth / allocation /
// FX surface. An optional cost-basis amount + date seeds an earlier value-timeline point so
// appreciation reads from then. The header ← returns to the launcher; the ✕ cancels.
function TangibleAssetModal({ category, onClose, onBack, onComplete }) {
  const copy = ASSET_ADD_COPY[category] || ASSET_ADD_COPY.other_assets;
  const valueLabel = copy.valueLabel || DEFAULT_VALUE_LABEL;
  const costLabel = copy.costLabel || DEFAULT_COST_LABEL;
  const costDateLabel = copy.costDateLabel || DEFAULT_COST_DATE_LABEL;
  const typeOptions = useMemo(() => getAssetGroupAccountTypeOptions(category), [category]);
  const [accountType, setAccountType] = useState(() => (typeOptions[0] ? typeOptions[0].value : ''));
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [currency, setCurrency] = useState('CAD');
  const [purchasePrice, setPurchasePrice] = useState('');
  const [purchaseDate, setPurchaseDate] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const submittingRef = useRef(false);

  // Auto-dismiss the success screen after a brief window (same as the other add modals).
  useEffect(() => {
    if (!done) return undefined;
    const timer = setTimeout(() => onClose(), 1500);
    return () => clearTimeout(timer);
  }, [done, onClose]);

  const trimmedName = name.trim();

  const handleSave = async () => {
    if (!trimmedName) { setError('A name is required.'); return; }
    const hasValue = value !== '' && value != null;
    const hasPurchasePrice = purchasePrice !== '' && purchasePrice != null;
    const hasPurchaseDate = Boolean(purchaseDate);
    // Current value is required; the cost-basis amount can stand in as today's value.
    if (!hasValue && !hasPurchasePrice) {
      setError(`A ${valueLabel.toLowerCase()} is required.`);
      return;
    }
    // Cost basis (amount + date) is seeded together as one historical point, or not at all.
    if (hasPurchasePrice !== hasPurchaseDate) {
      setError(`Add both ${costLabel.toLowerCase()} and ${costDateLabel.toLowerCase()}, or leave both blank.`);
      return;
    }
    if (submittingRef.current) return;  // guard against a double-click creating two assets
    submittingRef.current = true;
    setBusy(true);
    setError('');
    try {
      const resp = await fetch(`${API}/accounts/group`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          category,
          name: trimmedName,
          account_type: accountType,
          currency,
          value: (value === '' || value == null) ? null : Number(value),
          purchase_value: hasPurchasePrice ? Number(purchasePrice) : null,
          purchase_date: hasPurchaseDate ? purchaseDate : null,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') {
        throw new Error(data.detail || data.message || `Could not add ${copy.noun}.`);
      }
      if (onComplete) onComplete();
      setDone(true);
    } catch (e) {
      setError(e.message || `Could not add ${copy.noun}.`);
    } finally {
      setBusy(false);
      submittingRef.current = false;
    }
  };

  return (
    <div className="modal-overlay">
      <div className="modal-content net-worth-entry-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          {!done && (
            <button className="add-networth-back" onClick={onBack} aria-label="Back">
              <MdArrowBack size={20} />
            </button>
          )}
          <h3 className="modal-title">{done ? 'Added' : copy.title}</h3>
          {!done && <button className="modal-close" onClick={onClose}>✕</button>}
        </div>

        <div className="modal-body tangible-asset-body">
          {!done && (
            <>
              <label className="manual-inst-field">
                <span className="manual-inst-label">{copy.typeLabel}</span>
                <Dropdown
                  className="manual-inst-type-select"
                  value={accountType}
                  options={typeOptions}
                  fitToOptions
                  ariaLabel={copy.typeLabel}
                  onChange={setAccountType}
                />
              </label>
              <label className="manual-inst-field">
                <span className="manual-inst-label">Details</span>
                <input
                  className="manual-inst-input"
                  value={name}
                  placeholder={copy.namePlaceholder}
                  onChange={(e) => setName(e.target.value)}
                  autoFocus
                />
              </label>
              <div className="manual-inst-field">
                <span className="manual-inst-label">{valueLabel}</span>
                <div className="net-worth-entry-value-row">
                  <MoneyInput
                    className="manual-inst-input amount-input"
                    value={value}
                    placeholder="0.00"
                    aria-label={valueLabel}
                    onChange={setValue}
                  />
                  <Dropdown
                    className="manual-inst-ccy currency-dropdown"
                    value={currency}
                    options={CURRENCIES}
                    ariaLabel="Currency"
                    onChange={setCurrency}
                  />
                </div>
              </div>
              <div className="net-worth-entry-purchase-grid">
                <label className="manual-inst-field">
                  <span className="manual-inst-label">{costLabel} (optional)</span>
                  <MoneyInput
                    className="manual-inst-input amount-input"
                    value={purchasePrice}
                    placeholder="0.00"
                    aria-label={costLabel}
                    onChange={setPurchasePrice}
                  />
                </label>
                <div className="manual-inst-field manual-inst-date-field">
                  <span className="manual-inst-label">{costDateLabel} (optional)</span>
                  <SingleDatePicker
                    className="manual-inst-date-picker"
                    size="compact"
                    value={purchaseDate}
                    onChange={setPurchaseDate}
                    placeholder={costDateLabel}
                    ariaLabel={costDateLabel}
                    popoverPlacement="right"
                    popoverCrossAlign="center"
                  />
                </div>
              </div>
              {error && <div className="manual-inst-error">{error}</div>}
              <div className="tangible-asset-footer">
                <button type="button" className="btn-primary app-control-root" onClick={handleSave} disabled={busy}>
                  <span className="app-control-label">{busy ? 'Saving…' : copy.cta}</span>
                </button>
              </div>
            </>
          )}

          {done && (
            <div className="manual-inst-success">
              <div className="modal-success-icon">&#10003;</div>
              <p className="modal-2fa-text">{`${getInstitutionSuccessMessage({ name: trimmedName }, 'add')} into ${copy.bucket}`}</p>
            </div>
          )}
        </div>

      </div>
    </div>
  );
}

export default TangibleAssetModal;
