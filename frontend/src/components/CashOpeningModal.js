import React, { useState, useRef, useEffect, useMemo } from 'react';
import { MdArrowBack } from 'react-icons/md';
import Dropdown from './Dropdown';
import MoneyInput from './MoneyInput';
import { API } from '../config';
import { FIAT_CURRENCY_OPTIONS } from '../constants/currencies';
import './ManualInstitutionWizard.css';
import './NetWorthEntryModal.css';

// Cash opening-balance tile (Add to Net Worth). Physical wallet cash needs only a
// currency + starting amount — fiat-only, no subtypes, no value-history (cash is
// transaction-driven, not revalued). Lazily provisions the per-currency wallet Cash
// holder and writes its `cash_opening:<CCY>` baseline (dated today) via POST /accounts/cash.
// This tile only ADDS a currency you don't track yet — currencies already held are removed
// from the picker; editing an existing currency's opening balance lives in the Cash settings
// modal. Ongoing changes come from Add Transaction.
function CashOpeningModal({ accounts = [], onClose, onBack, onComplete }) {
  const existingCashCurrencies = useMemo(
    () => new Set(
      accounts
        .filter((a) => a.provider === 'manual' && a.account_type === 'cash')
        .map((a) => String(a.currency || '').toUpperCase()),
    ),
    [accounts],
  );
  const availableOptions = useMemo(
    () => FIAT_CURRENCY_OPTIONS.filter((o) => !existingCashCurrencies.has(o.value)),
    [existingCashCurrencies],
  );
  const noneLeft = availableOptions.length === 0;

  const [value, setValue] = useState('');
  const [currency, setCurrency] = useState(() => availableOptions[0]?.value || 'CAD');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const [addedCurrency, setAddedCurrency] = useState('');
  const submittingRef = useRef(false);

  // Auto-dismiss the success screen after a brief window (same as the other add modals).
  useEffect(() => {
    if (!done) return undefined;
    const timer = setTimeout(() => onClose(), 1500);
    return () => clearTimeout(timer);
  }, [done, onClose]);

  const handleSave = async () => {
    if (value === '' || value == null) { setError('A balance is required.'); return; }
    if (submittingRef.current) return;  // guard against a double-click
    submittingRef.current = true;
    setBusy(true);
    setError('');
    try {
      const resp = await fetch(`${API}/accounts/cash`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ currency, value: Number(value) }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') {
        throw new Error(data.detail || data.message || 'Could not add cash.');
      }
      if (onComplete) onComplete();
      setAddedCurrency(String(currency || '').toUpperCase());
      setDone(true);
    } catch (e) {
      setError(e.message || 'Could not add cash.');
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
          <h3 className="modal-title">{done ? 'Added' : 'Add Cash'}</h3>
          {!done && <button className="modal-close" onClick={onClose}>✕</button>}
        </div>

        <div className="modal-body manual-inst-body">
          {!done && (
            noneLeft ? (
              <p className="modal-note-muted modal-note-muted-centered">
                You already track cash in every currency. Edit an opening balance from the Cash account settings.
              </p>
            ) : (
              <>
                <div className="manual-inst-field">
                  <span className="manual-inst-label">Opening Balance</span>
                  <div className="net-worth-entry-value-row">
                    <MoneyInput
                      className="manual-inst-input amount-input"
                      value={value}
                      placeholder="0.00"
                      aria-label="Opening cash balance"
                      onChange={setValue}
                    />
                    <Dropdown
                      className="manual-inst-ccy currency-dropdown"
                      value={currency}
                      options={availableOptions}
                      ariaLabel="Currency"
                      onChange={setCurrency}
                    />
                  </div>
                </div>
                <p className="manual-inst-hint manual-inst-hint-center">
                  The cash you have on hand right now. Create fuller record via "Add Transactions" action.
                </p>
                {error && <div className="manual-inst-error">{error}</div>}
              </>
            )
          )}

          {done && (
            <div className="manual-inst-success">
              <div className="modal-success-icon">&#10003;</div>
              <p className="modal-2fa-text">Cash ({addedCurrency || currency}) added to your Net Worth</p>
            </div>
          )}
        </div>

        {!done && !noneLeft && (
          <div className="manual-inst-footer">
            <button type="button" className="btn-primary app-control-root" onClick={handleSave} disabled={busy}>
              <span className="app-control-label">{busy ? 'Saving…' : 'Add Cash'}</span>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default CashOpeningModal;
