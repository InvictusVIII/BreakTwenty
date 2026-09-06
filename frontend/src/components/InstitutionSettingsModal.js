import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import InstitutionLogo, { AuthenticatedInstitutionImage } from './InstitutionLogo';
import { renderBrandMarkedText, renderBrandText } from './BrandName';
import {
  MdVisibility, MdVisibilityOff, MdDelete, MdAdd, MdArrowBack, MdHistory, MdEdit,
} from 'react-icons/md';
import { API } from '../config';
import { APP_BRAND_NAME } from '../constants/brand';
import {
  API_PROVIDER_CONFIG,
  SCRAPER_ENDPOINTS,
  SCRAPER_FIELD_LABELS,
  getAssetGroupAccountTypeOptions,
  getBackgroundSyncEndpoint,
  isAssetGroupProvider,
} from '../constants/providers';
import { formatMoney, formatCompactMoney } from '../utils/format';
import FitMoney from './FitMoney';
import MoneyInput from './MoneyInput';
import Dropdown from './Dropdown';
import SingleDatePicker from './SingleDatePicker';
import AccountTypeBadge from './AccountTypeBadge';
import {
  BANKING_ACCOUNT_TYPES,
  CRYPTO_ACCOUNT_TYPES,
  MANUAL_INSTITUTION_ICON_HINT,
  validateManualInstitutionIconFile,
} from './ManualInstitutionWizard';
import { CURRENCY_OPTIONS } from '../constants/currencies';
import {
  USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS,
  fetchWithTimeout,
} from '../utils/syncRequests';
import { isPlainObject, readJsonResponse } from '../utils/apiResponse';
import { todayLocalDateValue } from '../utils/date';
import { isPromoDemoActive } from './promoDemoEnvironment';

const SCRAPER_CREDENTIAL_VALIDATION_PROVIDERS = new Set(['wealthsimple']);
const SCRAPER_CREDENTIAL_ACCEPTED_STATUSES = new Set(['ok', '2fa_required']);
const OK_RESPONSE_STATUS = new Set(['ok']);
const CREDENTIAL_READ_RESPONSE_STATUSES = new Set(['ok', 'not_found']);

function semanticStatusFailureMessage(payload, fallback) {
  const detail = payload?.detail || payload?.message;
  if (typeof detail === 'string' && detail.trim()) {
    return detail;
  }
  return fallback;
}

async function readStatusResponse(response, acceptedStatuses, { label, fallback }) {
  const payload = await readJsonResponse(response, { label, validate: isPlainObject });
  if (!acceptedStatuses.has(payload.status)) {
    throw new Error(semanticStatusFailureMessage(payload, fallback));
  }
  return payload;
}

function buildCredentialValues(fields = [], settings = {}) {
  const values = {};
  fields.forEach((field) => {
    values[field.key] = settings[field.key] || field.defaultValue || '';
  });
  return values;
}

// Standard money tone: positive green, negative red, zero white — matching the rest of
// the app's sign coloring. Pair with `signedAssetValue` so liabilities read negative/red.
function moneyTone(value) {
  const n = Number(value) || 0;
  return n > 0 ? 'is-positive' : n < 0 ? 'is-negative' : 'is-zero';
}
// Sign any stored figure for display: a liability's stored value is the net amount owed,
// so negate it — you-owe (positive owed) → negative/red; a credit balance (overpaid, stored
// negative) → positive/green, since the lender then owes you. Mirrors AccountBalanceCell.
function signedAmount(asset, value) {
  const n = Number(value) || 0;
  return asset?.is_liability ? -n : n;
}
function signedAssetValue(asset) {
  return signedAmount(asset, asset.balance);
}

export function formatPurchaseDate(iso) {
  if (!iso) return '';
  const datePrefix = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (datePrefix) {
    const [, year, month, day] = datePrefix;
    const localDate = new Date(Number(year), Number(month) - 1, Number(day));
    return localDate.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

async function fetchInstitutionAccounts(institutionId) {
  const resp = await fetch(`${API}/institutions/${encodeURIComponent(institutionId)}/accounts`);
  return resp.json();
}

function InstitutionSettingsModal({ institution, onClose, onDataChange, onLogoChange, onReconnect, accounts = [], institutions = [], convert = (amount) => amount }) {
  const [validating, setValidating] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [fieldVisibility, setFieldVisibility] = useState({});
  const [showInstructions, setShowInstructions] = useState(false);
  const [credSuccess, setCredSuccess] = useState(false);
  const [credError, setCredError] = useState('');
  const [scraperSaving, setScraperSaving] = useState(false);
  const [scraperSuccess, setScraperSuccess] = useState(false);
  const [scraperError, setScraperError] = useState('');
  const [scraperFieldVisibility, setScraperFieldVisibility] = useState({});
  const [ibkrTab, setIbkrTab] = useState(institution.defaultTab || 'flex');
  const [showImportInstructions, setShowImportInstructions] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importStatus, setImportStatus] = useState(null);
  const fileInputRef = useRef(null);
  const pendingCredentialReplacementRef = useRef('');

  useEffect(() => () => {
    const replacementId = pendingCredentialReplacementRef.current;
    pendingCredentialReplacementRef.current = '';
    if (!replacementId) return;
    void fetch(
      `${API}/settings/credential-replacements/${encodeURIComponent(replacementId)}/rollback`,
      { method: 'POST', keepalive: true },
    ).catch(() => {});
  }, []);

  // Per-asset management for connector-less net-worth buckets (Real Estate, etc.):
  // list each asset, revalue it (revaluation door), or delete it individually.
  const isAssetGroup = isAssetGroupProvider(institution.provider);
  // Private Investments reuse the exact same cost-basis door as other assets, only
  // reworded — "invested" instead of "purchased" everywhere it surfaces.
  const isInvestmentBucket = institution.provider === 'private_investments';
  // Debt reuses the same cost-basis door, reworded "borrowed" — and its accounts are
  // liabilities, so the cost-basis row (gated to assets below) must be re-enabled for it.
  const isDebtBucket = institution.provider === 'debt';
  const costValueLabel = isDebtBucket ? 'borrowed amount' : isInvestmentBucket ? 'invested value' : 'purchase value';
  const costValueLabelCap = isDebtBucket ? 'Amount borrowed' : isInvestmentBucket ? 'Invested value' : 'Purchase value';
  const costDateLabelCap = isDebtBucket ? 'Date borrowed' : isInvestmentBucket ? 'Invested date' : 'Purchase date';
  const costVerbPast = isDebtBucket ? 'Borrowed' : isInvestmentBucket ? 'Invested' : 'Purchased';
  const costPointWord = isDebtBucket ? 'borrow point' : isInvestmentBucket ? 'invest point' : 'purchase point';
  const costUnsetText = isDebtBucket ? 'Amount borrowed and date not set' : isInvestmentBucket ? 'Invested value and date not set' : 'Purchase value and date not set';
  const costBasisClearedText = isDebtBucket ? ', and the borrowed amount will be cleared' : isInvestmentBucket ? ', and the invested cost basis will be cleared' : ', and the purchase cost basis will be cleared';
  const [assets, setAssets] = useState([]);
  const [assetsLoaded, setAssetsLoaded] = useState(false);
  const [editingAssetId, setEditingAssetId] = useState(null);
  const [editValue, setEditValue] = useState('');
  const [editDate, setEditDate] = useState('');
  const [assetBusy, setAssetBusy] = useState(false);
  const [assetError, setAssetError] = useState('');
  const [deletingAsset, setDeletingAsset] = useState(null);
  // Per-asset purchase (cost basis): display + add/backfill, mirrors the value edit.
  const [editingPurchaseId, setEditingPurchaseId] = useState(null);
  const [purchaseDraftValue, setPurchaseDraftValue] = useState('');
  const [purchaseDraftDate, setPurchaseDraftDate] = useState('');
  const [purchaseBusy, setPurchaseBusy] = useState(false);
  const [purchaseError, setPurchaseError] = useState('');
  // Per-asset details edit (currency + property type / badge).
  const [editingDetailsId, setEditingDetailsId] = useState(null);
  const [detailsType, setDetailsType] = useState('');
  const [detailsCurrency, setDetailsCurrency] = useState('CAD');
  const [detailsBusy, setDetailsBusy] = useState(false);
  const [detailsError, setDetailsError] = useState('');
  // Value History sub-view: when set, the modal swaps to the asset's full value timeline
  // (in-place drill-in, like the Add-to-Net-Worth provider picker), where each dated point
  // can be deleted individually.
  const [historyAsset, setHistoryAsset] = useState(null);
  const [historyPoints, setHistoryPoints] = useState([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const [deletingPoint, setDeletingPoint] = useState(null);
  // Cash opening (starting) balance editor (Cash holder only).
  const [editingOpeningId, setEditingOpeningId] = useState(null);
  const [openingValue, setOpeningValue] = useState('');
  const [openingDate, setOpeningDate] = useState('');
  const [openingBusy, setOpeningBusy] = useState(false);
  const [openingError, setOpeningError] = useState('');
  // Asset↔liability linker (asset-group asset accounts only): pick which loans this asset
  // secures. The loan candidate list + linked-loan resolution come from the `accounts` prop
  // (all of the user's accounts); equity = asset value − linked loans (FX-converted).
  const [linkingAssetId, setLinkingAssetId] = useState(null);
  const [linkSelection, setLinkSelection] = useState(() => new Set());
  const [linkBusy, setLinkBusy] = useState(false);
  const [linkError, setLinkError] = useState('');

  // User manual institutions (provider "manual_custom") share the value-managed
  // surface above — list accounts, revalue (builds a value history without
  // transactions), delete — plus an inline add-account form. Synced institutions
  // never reach this section (no apiConfig/scraper => no credentials block either).
  const isManual = institution.provider === 'manual_custom';
  const isValueManaged = isAssetGroup || isManual;
  // The connector-less Cash holder (provider 'manual'): each per-currency cash account gets
  // a lightweight manager — set its opening (starting) balance, or delete the holder.
  const isCash = institution.provider === 'manual';
  const promoDemoActive = import.meta.env.DEV && isPromoDemoActive();
  const addAccountTypes = institution.category === 'crypto_wallet' ? CRYPTO_ACCOUNT_TYPES : BANKING_ACCOUNT_TYPES;
  const [addingAccount, setAddingAccount] = useState(false);
  const [newAccount, setNewAccount] = useState({ name: '', account_type: '', currency: 'CAD', value: '' });
  const [addBusy, setAddBusy] = useState(false);
  const [addError, setAddError] = useState('');
  const [logoOverride, setLogoOverride] = useState(null);
  const [logoBusy, setLogoBusy] = useState(false);
  const [logoError, setLogoError] = useState('');
  const logoFileInputRef = useRef(null);
  const logoHasLogo = logoOverride?.institutionId === institution.id
    ? logoOverride.hasLogo
    : Boolean(institution.has_logo);
  const logoVersion = logoOverride?.institutionId === institution.id ? logoOverride.version : 0;

  const apiConfig = API_PROVIDER_CONFIG[institution.provider];
  const useMoomooCloudOAuth = institution.provider === 'moomoo';
  const hasSavedScraperCredentials = Boolean(SCRAPER_ENDPOINTS[institution.provider]);
  const validatesScraperCredentials = SCRAPER_CREDENTIAL_VALIDATION_PROVIDERS.has(institution.provider);
  const credentialDefaults = useMemo(() => buildCredentialValues(apiConfig?.fields), [apiConfig]);
  const [credentialsState, setCredentialsState] = useState(() => ({
    provider: institution.provider,
    values: buildCredentialValues(apiConfig?.fields),
  }));
  const credentials = credentialsState.provider === institution.provider
    ? credentialsState.values
    : credentialDefaults;
  const setCredentials = useCallback((nextCredentials) => {
    setCredentialsState((previous) => {
      const baseValues = previous.provider === institution.provider
        ? previous.values
        : credentialDefaults;
      const values = typeof nextCredentials === 'function'
        ? nextCredentials(baseValues)
        : nextCredentials;
      return { provider: institution.provider, values };
    });
  }, [credentialDefaults, institution.provider]);
  const emptyScraperCreds = useMemo(() => ({ username: '', password: '' }), []);
  const [scraperCredsState, setScraperCredsState] = useState(() => ({
    provider: institution.provider,
    values: emptyScraperCreds,
  }));
  const scraperCreds = hasSavedScraperCredentials && scraperCredsState.provider === institution.provider
    ? scraperCredsState.values
    : emptyScraperCreds;
  const setScraperCreds = useCallback((nextScraperCreds) => {
    setScraperCredsState((previous) => {
      const baseValues = previous.provider === institution.provider
        ? previous.values
        : emptyScraperCreds;
      const values = typeof nextScraperCreds === 'function'
        ? nextScraperCreds(baseValues)
        : nextScraperCreds;
      return { provider: institution.provider, values };
    });
  }, [emptyScraperCreds, institution.provider]);

  useEffect(() => {
    let cancelled = false;

    if (!apiConfig || useMoomooCloudOAuth) {
      return undefined;
    }

    if (institution.provider === 'ibkr' && ibkrTab !== 'flex') {
      return undefined;
    }

    const fetchCreds = async () => {
      try {
        const resp = await fetch(`${API}/settings?institution_id=${encodeURIComponent(institution.id)}`);
        const data = await readJsonResponse(resp, {
          label: 'Saved settings',
          validate: isPlainObject,
        });
        if (cancelled) {
          return;
        }

        // Settings inputs load the saved values masked. The existing field toggle is
        // the only action that reveals a secret as plain text.
        setCredentials(buildCredentialValues(apiConfig.fields, data));
      } catch (err) {
        console.error('Failed to fetch settings:', err);
        if (!cancelled) setCredError(err?.message || 'Failed to fetch saved settings.');
      }
    };

    fetchCreds();

    return () => {
      cancelled = true;
    };
  }, [apiConfig, ibkrTab, institution.id, institution.provider, setCredentials, useMoomooCloudOAuth]);

  useEffect(() => {
    let cancelled = false;

    if (!hasSavedScraperCredentials) {
      return undefined;
    }

    (async () => {
      try {
        const response = await fetch(`${API}/credentials/${institution.provider}/status?institution_id=${encodeURIComponent(institution.id)}`);
        const data = await readStatusResponse(
          response,
          CREDENTIAL_READ_RESPONSE_STATUSES,
          {
            label: 'Saved credentials',
            fallback: 'Failed to fetch saved credentials.',
          },
        );
        if (cancelled) return;
        setScraperCreds(data.status === 'ok' && data.has_saved_credentials === true
          ? {
              username: String(data.username || ''),
              password: String(data.password || ''),
            }
          : emptyScraperCreds);
      } catch (error) {
        if (!cancelled) {
          setScraperError(error?.message || 'Failed to fetch saved credentials.');
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [emptyScraperCreds, hasSavedScraperCredentials, institution.id, institution.provider, setScraperCreds]);

  useEffect(() => {
    // Fetch the institution's accounts for any provider — value-managed/cash render their
    // account lists, and synced providers may hold is_imported (statement) accounts to manage.
    let cancelled = false;
    fetchInstitutionAccounts(institution.id)
      .then((data) => {
        if (cancelled) return;
        setAssets(Array.isArray(data) ? data : []);
        setAssetsLoaded(true);
      })
      .catch(() => { if (!cancelled) setAssetsLoaded(true); });
    return () => { cancelled = true; };
  }, [institution.id]);

  const handleSaveCredentials = async () => {
    const apiFields = API_PROVIDER_CONFIG[institution.provider]?.fields || [];
    const emptyFields = apiFields.filter(f => !credentials[f.key]?.trim());
    if (emptyFields.length > 0) {
      if (emptyFields.length === apiFields.length) {
        setCredError("Credentials cannot be empty.");
      } else {
        setCredError(`${emptyFields[0].label} cannot be empty.`);
      }
      return;
    }
    setValidating(true);
    setCredError("");
    setCredSuccess(false);
    let credentialReplacementId = '';
    const settleCredentialReplacement = async (action) => {
      const replacementId = credentialReplacementId || pendingCredentialReplacementRef.current;
      if (!replacementId) return;
      const response = await fetch(
        `${API}/settings/credential-replacements/${encodeURIComponent(replacementId)}/${action}`,
        { method: 'POST' },
      );
      await readStatusResponse(
        response,
        OK_RESPONSE_STATUS,
        {
          label: `Credential ${action}`,
          fallback: `Credential replacement could not be ${action === 'commit' ? 'committed' : 'rolled back'}.`,
        },
      );
      credentialReplacementId = '';
      if (pendingCredentialReplacementRef.current === replacementId) {
        pendingCredentialReplacementRef.current = '';
      }
    };
    try {
      const saveNewCreds = async () => {
        const saveResponse = await fetch(`${API}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...credentials, institution_id: institution.id }),
        });
        const saveResult = await readStatusResponse(
          saveResponse,
          OK_RESPONSE_STATUS,
          {
            label: 'Credential save',
            fallback: 'Failed to save credentials.',
          },
        );
        credentialReplacementId = String(saveResult.credential_replacement_id || '');
        pendingCredentialReplacementRef.current = credentialReplacementId;
      };

      if (promoDemoActive) {
        await saveNewCreds();
        await settleCredentialReplacement('commit');
        if (onDataChange) onDataChange();
        setCredSuccess(true);
        setTimeout(() => { setCredSuccess(false); }, 2000);
        return;
      }

      // Validate by running a sync
      const syncEndpoint = getBackgroundSyncEndpoint(institution.provider);
      if (syncEndpoint) {
        await saveNewCreds();
        const syncResp = await fetchWithTimeout(
          `${API}${syncEndpoint}`,
          { method: "POST" },
          USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS,
        );
        await readStatusResponse(
          syncResp,
          OK_RESPONSE_STATUS,
          {
            label: 'Credential validation',
            fallback: 'Sync failed — please check your credentials.',
          },
        );
      } else {
        await saveNewCreds();
      }
      await settleCredentialReplacement('commit');
      if (onDataChange) onDataChange();
      setCredSuccess(true);
      setTimeout(() => { setCredSuccess(false); }, 2000);
    } catch (err) {
      let rollbackError = '';
      if (credentialReplacementId) {
        try {
          await settleCredentialReplacement('rollback');
        } catch (restoreError) {
          rollbackError = ` ${restoreError?.message || 'Previous credentials could not be restored.'}`;
        }
      }
      const message = `${err?.message || "Failed to save or validate credentials."}${rollbackError}`;
      setCredError(message);
    } finally {
      setValidating(false);
    }
  };

  const handleSaveScraperCreds = async () => {
    if (!scraperCreds.username.trim() || !scraperCreds.password.trim()) {
      setScraperError('Both fields are required.');
      return;
    }
    setScraperSaving(true);
    setScraperError('');
    setScraperSuccess(false);
    let validationStatus = null;
    const restoreSyncStatus = async () => {
      if (!institution.id) return;
      const response = await fetch(`${API}/institutions/${institution.id}/sync-status`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sync_status: institution.sync_status || "ok" }),
      });
      await readStatusResponse(
        response,
        OK_RESPONSE_STATUS,
        {
          label: 'Sync-status rollback',
          fallback: 'Previous sync status could not be restored.',
        },
      );
    };
    const saveScraperCredentials = async ({ preservePendingSession = false } = {}) => {
      const resp = await fetch(`${API}/credentials/${institution.provider}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: scraperCreds.username,
          password: scraperCreds.password,
          preserve_pending_session: preservePendingSession,
          institution_id: institution.id,
        }),
      });
      return readStatusResponse(
        resp,
        OK_RESPONSE_STATUS,
        {
          label: 'Credential save',
          fallback: 'Failed to save credentials.',
        },
      );
    };
    try {
      if (validatesScraperCredentials) {
        const endpoints = SCRAPER_ENDPOINTS[institution.provider];
        const validationResp = await fetchWithTimeout(
          `${API}${endpoints.login}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              username: scraperCreds.username,
              password: scraperCreds.password,
              institution_id: institution.id || undefined,
            }),
          },
          USER_INITIATED_SYNC_REQUEST_TIMEOUT_MS,
        );
        const validationData = await readJsonResponse(validationResp, {
          label: 'Credential validation',
          validate: isPlainObject,
        });
        validationStatus = validationData.status;
        if (!SCRAPER_CREDENTIAL_ACCEPTED_STATUSES.has(validationData.status)) {
          await restoreSyncStatus();
          if (onDataChange) onDataChange();
          setScraperError(`${validationData.message || 'Login failed'} Previous credentials were kept.`);
          return;
        }
      }

      if (validationStatus === '2fa_required') {
        await restoreSyncStatus();
      }
      await saveScraperCredentials({ preservePendingSession: validatesScraperCredentials });
      if (onDataChange) onDataChange();
      setScraperSuccess(true);
      setTimeout(() => setScraperSuccess(false), 2000);
    } catch (err) {
      try {
        await restoreSyncStatus();
        if (onDataChange) onDataChange();
      } catch (restoreErr) {
        console.error('Failed to restore previous credentials:', restoreErr);
      }
      setScraperError(`${err?.message || 'Failed to save credentials.'} Previous credentials were kept.`);
    } finally {
      setScraperSaving(false);
    }
  };

  const refreshAssets = async () => {
    try {
      const data = await fetchInstitutionAccounts(institution.id);
      setAssets(Array.isArray(data) ? data : []);
    } catch { /* keep the prior list on a transient fetch failure */ }
  };

  const notifyDataChanged = () => {
    if (!onDataChange) return;
    Promise.resolve(onDataChange()).catch(() => {});
  };

  // Only one inline editor (value / purchase / details / opening) is ever open at a time across
  // every settings-modal section — opening one closes any other without saving.
  const closeAllEdits = () => {
    setEditingAssetId(null);
    setEditingPurchaseId(null);
    setEditingDetailsId(null);
    setEditingOpeningId(null);
    setLinkingAssetId(null);
    setAssetError('');
    setPurchaseError('');
    setDetailsError('');
    setOpeningError('');
    setLinkError('');
  };

  const startEditAsset = (asset) => {
    closeAllEdits();
    setEditingAssetId(asset.id);
    setEditValue(asset.balance != null ? String(asset.balance) : '');
    setEditDate(todayLocalDateValue());
  };

  const handleUpdateAssetValue = async (asset) => {
    if (editValue === '' || editValue == null || !editDate) { setAssetError('Enter a value and a date.'); return; }
    // A revaluation can't predate the declared origin — a bucket's purchase/invested/borrowed
    // point, or a manual account's opening balance. That's the timeline floor; steer the user
    // to the right editor to move the origin instead.
    const floorISO = asset.purchase
      ? String(asset.purchase.date).slice(0, 10)
      : (asset.opening_balance_date ? String(asset.opening_balance_date).slice(0, 10) : null);
    if (floorISO && editDate < floorISO) {
      let dateWord; let anchorTool; let floorDisplay;
      if (asset.purchase) {
        dateWord = isDebtBucket ? 'borrowed date' : isInvestmentBucket ? 'invested date' : 'purchase date';
        anchorTool = costValueLabel;
        floorDisplay = formatPurchaseDate(asset.purchase.date);
      } else {
        dateWord = asset.is_liability ? 'borrowed date' : 'opening date';
        anchorTool = asset.is_liability ? 'amount borrowed' : 'opening balance';
        floorDisplay = formatPurchaseDate(asset.opening_balance_date);
      }
      setAssetError(`Value date can't be before the ${dateWord} (${floorDisplay}). Update the ${anchorTool} to record an earlier value.`);
      return;
    }
    setAssetBusy(true);
    setAssetError('');
    try {
      const resp = await fetch(`${API}/accounts/${asset.id}/value`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: Number(editValue), date: editDate }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data.message || 'Could not update value.');
      setEditingAssetId(null);
      await refreshAssets();
      notifyDataChanged();
    } catch (err) {
      setAssetError(err?.message || 'Could not update value.');
    } finally {
      setAssetBusy(false);
    }
  };

  const startEditPurchase = (asset) => {
    closeAllEdits();
    setEditingPurchaseId(asset.id);
    setPurchaseDraftValue(asset.purchase ? String(asset.purchase.value) : '');
    setPurchaseDraftDate(asset.purchase ? String(asset.purchase.date).slice(0, 10) : todayLocalDateValue());
  };

  const handleSetPurchase = async (asset) => {
    if (purchaseDraftValue === '' || purchaseDraftValue == null || !purchaseDraftDate) {
      setPurchaseError('Enter a value and date.');
      return;
    }
    setPurchaseBusy(true);
    setPurchaseError('');
    try {
      const resp = await fetch(`${API}/accounts/${asset.id}/purchase`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: Number(purchaseDraftValue), date: purchaseDraftDate }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data.message || 'Could not save purchase.');
      setEditingPurchaseId(null);
      await refreshAssets();
      notifyDataChanged();
    } catch (err) {
      setPurchaseError(err?.message || 'Could not save purchase.');
    } finally {
      setPurchaseBusy(false);
    }
  };

  const startEditDetails = (asset) => {
    closeAllEdits();
    setEditingDetailsId(asset.id);
    setDetailsType(asset.account_type || '');
    setDetailsCurrency(asset.currency || 'CAD');
  };

  const handleSaveDetails = async (asset) => {
    setDetailsBusy(true);
    setDetailsError('');
    try {
      const resp = await fetch(`${API}/accounts/${asset.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_type: detailsType, currency: detailsCurrency }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data.message || 'Could not save changes.');
      setEditingDetailsId(null);
      await refreshAssets();
      notifyDataChanged();
    } catch (err) {
      setDetailsError(err?.message || 'Could not save changes.');
    } finally {
      setDetailsBusy(false);
    }
  };

  const loadHistory = async (assetId) => {
    setHistoryLoaded(false);
    setHistoryError('');
    try {
      const resp = await fetch(`${API}/accounts/${assetId}/value-history`);
      const data = await resp.json().catch(() => ({}));
      if (data.status !== 'ok') throw new Error(data.message || 'Could not load value history.');
      setHistoryPoints(Array.isArray(data.points) ? data.points : []);
    } catch (err) {
      setHistoryError(err?.message || 'Could not load value history.');
      setHistoryPoints([]);
    } finally {
      setHistoryLoaded(true);
    }
  };

  const openHistory = (asset) => {
    setEditingAssetId(null);
    setEditingPurchaseId(null);
    setEditingDetailsId(null);
    setDeletingPoint(null);
    setHistoryAsset(asset);
    setHistoryPoints([]);
    loadHistory(asset.id);
  };

  const handleDeletePoint = async () => {
    if (!deletingPoint || !historyAsset) return;
    setHistoryBusy(true);
    setHistoryError('');
    try {
      const resp = await fetch(`${API}/accounts/${historyAsset.id}/value-history/${deletingPoint.id}`, { method: 'DELETE' });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data.message || 'Could not delete value point.');
      setDeletingPoint(null);
      await loadHistory(historyAsset.id);
      await refreshAssets();
      notifyDataChanged();
    } catch (err) {
      setHistoryError(err?.message || 'Could not delete value point.');
    } finally {
      setHistoryBusy(false);
    }
  };

  const startEditOpening = (asset) => {
    closeAllEdits();
    setEditingOpeningId(asset.id);
    setOpeningValue(asset.cash_opening?.amount != null ? String(asset.cash_opening.amount) : '');
    setOpeningDate(asset.cash_opening?.date ? String(asset.cash_opening.date).slice(0, 10) : todayLocalDateValue());
  };

  const handleSetOpening = async (asset) => {
    if (openingValue === '' || openingValue == null || !openingDate) { setOpeningError('Enter a value and a date.'); return; }
    setOpeningBusy(true);
    setOpeningError('');
    try {
      const resp = await fetch(`${API}/accounts/${asset.id}/cash-opening`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: Number(openingValue), date: openingDate }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data.message || 'Could not set opening balance.');
      setEditingOpeningId(null);
      await refreshAssets();
      notifyDataChanged();
    } catch (err) {
      setOpeningError(err?.message || 'Could not set opening balance.');
    } finally {
      setOpeningBusy(false);
    }
  };

  // Manual-institution accounts: set the opening balance + its date (the anchor the
  // balance derivation and any later transaction import build forward from). Reuses the
  // opening* state (a settings modal is one institution, so cash and manual never overlap).
  const startEditManualOpening = (asset) => {
    closeAllEdits();
    setEditingOpeningId(asset.id);
    setOpeningValue(asset.opening_balance != null ? String(asset.opening_balance) : '');
    setOpeningDate(asset.opening_balance_date ? String(asset.opening_balance_date).slice(0, 10) : todayLocalDateValue());
  };

  const handleSetManualOpening = async (asset) => {
    if (openingValue === '' || openingValue == null || !openingDate) { setOpeningError('Enter a value and a date.'); return; }
    setOpeningBusy(true);
    setOpeningError('');
    try {
      const resp = await fetch(`${API}/accounts/${asset.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ opening_balance: Number(openingValue), opening_balance_date: openingDate }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data.message || 'Could not set opening balance.');
      setEditingOpeningId(null);
      await refreshAssets();
      notifyDataChanged();
    } catch (err) {
      setOpeningError(err?.message || 'Could not set opening balance.');
    } finally {
      setOpeningBusy(false);
    }
  };

  // Asset↔liability linking. Candidates + current links come from the `accounts` prop.
  const allLiabilities = accounts.filter((a) => a.is_liability);
  const accountsById = new Map(accounts.map((a) => [a.id, a]));
  const institutionsById = new Map(institutions.map((i) => [i.id, i]));
  const linkedLoansFor = (assetId) => allLiabilities.filter((loan) => loan.secured_asset_account_id === assetId);
  // Equity = asset value − Σ linked loans, each converted into the asset's currency.
  const equityFor = (asset) => linkedLoansFor(asset.id).reduce(
    (eq, loan) => eq - convert(Number(loan.balance) || 0, loan.currency, asset.currency),
    Number(asset.balance) || 0,
  );

  const startEditLinks = (asset) => {
    closeAllEdits();
    setLinkingAssetId(asset.id);
    setLinkSelection(new Set(linkedLoansFor(asset.id).map((loan) => loan.id)));
  };
  const toggleLinkSelection = (loanId) => {
    setLinkSelection((prev) => {
      const next = new Set(prev);
      if (next.has(loanId)) next.delete(loanId); else next.add(loanId);
      return next;
    });
  };
  const handleSaveLinks = async (asset) => {
    setLinkBusy(true);
    setLinkError('');
    try {
      const resp = await fetch(`${API}/accounts/${asset.id}/linked-loans`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ liability_account_ids: Array.from(linkSelection) }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data.message || 'Could not save linked loans.');
      setLinkingAssetId(null);
      await refreshAssets();
      notifyDataChanged();
    } catch (err) {
      setLinkError(err?.message || 'Could not save linked loans.');
    } finally {
      setLinkBusy(false);
    }
  };

  const handleDeleteAsset = async () => {
    if (!deletingAsset) return;
    setAssetBusy(true);
    setAssetError('');
    try {
      const resp = await fetch(`${API}/accounts/${deletingAsset.id}`, { method: 'DELETE' });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data.message || 'Could not delete asset.');
      const bucketGone = data.institution_removed || assets.length <= 1;
      setDeletingAsset(null);
      if (onDataChange) onDataChange();
      // Deleting the last asset prunes the bucket institution — nothing left to manage.
      if (bucketGone) { onClose(); return; }
      await refreshAssets();
    } catch (err) {
      setAssetError(err?.message || 'Could not delete asset.');
    } finally {
      setAssetBusy(false);
    }
  };

  const openAddAccount = () => {
    setAddError('');
    setNewAccount({
      name: '',
      account_type: (addAccountTypes[0] && addAccountTypes[0].value) || 'chequing',
      currency: 'CAD',
      value: '',
    });
    setAddingAccount(true);
  };

  const handleAddAccount = async () => {
    if (!newAccount.name.trim()) { setAddError('Enter an account name.'); return; }
    setAddBusy(true);
    setAddError('');
    try {
      const resp = await fetch(`${API}/institutions/${institution.id}/accounts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          accounts: [{
            name: newAccount.name.trim(),
            account_type: newAccount.account_type,
            currency: newAccount.currency,
            opening_balance: newAccount.value === '' ? null : Number(newAccount.value),
          }],
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') throw new Error(data.message || 'Could not add account.');
      setAddingAccount(false);
      await refreshAssets();
      if (onDataChange) onDataChange();
    } catch (err) {
      setAddError(err?.message || 'Could not add account.');
    } finally {
      setAddBusy(false);
    }
  };

  const handleLogoFileChange = async (event) => {
    const file = event.target.files?.[0] || null;
    if (!file) return;
    const validationError = validateManualInstitutionIconFile(file);
    if (validationError) {
      setLogoError(validationError);
      if (event.target) event.target.value = '';
      return;
    }
    setLogoBusy(true);
    setLogoError('');
    try {
      const form = new FormData();
      form.append('file', file);
      const resp = await fetch(`${API}/institutions/${institution.id}/logo`, { method: 'POST', body: form });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') {
        throw new Error(data.detail || data.message || 'Could not save logo.');
      }
      const version = Date.now();
      setLogoOverride({ institutionId: institution.id, hasLogo: true, version });
      if (onLogoChange) onLogoChange(institution.id, true, version);
      if (onDataChange) onDataChange();
    } catch (err) {
      setLogoError(err?.message || 'Could not save logo.');
    } finally {
      setLogoBusy(false);
      if (event.target) event.target.value = '';
    }
  };

  const handleRemoveLogo = async () => {
    setLogoBusy(true);
    setLogoError('');
    try {
      const resp = await fetch(`${API}/institutions/${institution.id}/logo`, { method: 'DELETE' });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.status !== 'ok') {
        throw new Error(data.detail || data.message || 'Could not remove logo.');
      }
      const version = Date.now();
      setLogoOverride({ institutionId: institution.id, hasLogo: false, version });
      if (onLogoChange) onLogoChange(institution.id, false, version);
      if (onDataChange) onDataChange();
    } catch (err) {
      setLogoError(err?.message || 'Could not remove logo.');
    } finally {
      setLogoBusy(false);
      if (logoFileInputRef.current) logoFileInputRef.current.value = '';
    }
  };

  const handleDelete = async () => {
    setDeleting(true);
    setDeleteError('');
    try {
      const resp = await fetch(`${API}/institutions/${institution.id}`, { method: 'DELETE' });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok || payload.status !== 'ok') {
        throw new Error(payload.message || `Delete failed (${resp.status})`);
      }
      if (onDataChange) onDataChange();
      onClose();
    } catch (err) {
      console.error('Failed to delete institution:', err);
      setDeleteError(err?.message || 'Failed to delete institution.');
    } finally {
      setDeleting(false);
    }
  };

  const handleImportFile = async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!files.length) return;
    setImporting(true);
    setImportStatus(null);
    try {
      const formData = new FormData();
      formData.append('institution_id', String(institution.id));
      files.forEach((file) => formData.append('files', file));
      const resp = await fetch(`${API}/import/ibkr-flex`, {
        method: 'POST',
        body: formData,
      });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const msg = payload?.detail || `Upload failed (${resp.status})`;
        setImportStatus({ ok: false, message: msg });
      } else {
        const created = payload.accounts_created ?? 0;
        const createdNote = created ? ` · ${created} account${created === 1 ? '' : 's'} added` : '';
        setImportStatus({
          ok: true,
          message: `Imported ${payload.imported ?? 0} · Skipped ${payload.skipped ?? 0} · Errors ${payload.errors ?? 0}${createdNote}`,
        });
        if (onDataChange) onDataChange();
      }
    } catch (err) {
      console.error('IBKR Flex import failed:', err);
      setImportStatus({ ok: false, message: 'Upload failed' });
    } finally {
      setImporting(false);
      setTimeout(() => setImportStatus(null), 10000);
    }
  };

  const handleQuestradeImport = async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!files.length) return;
    setImporting(true);
    setImportStatus(null);
    try {
      const formData = new FormData();
      formData.append('institution_id', String(institution.id));
      files.forEach((file) => formData.append('files', file));
      const resp = await fetch(`${API}/import/questrade-statement`, {
        method: 'POST',
        body: formData,
      });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        setImportStatus({ ok: false, message: payload?.detail || `Upload failed (${resp.status})` });
      } else {
        const created = payload.accounts_created ?? 0;
        const createdNote = created ? ` · ${created} account${created === 1 ? '' : 's'} added` : '';
        const txns = payload.transactions_imported ?? 0;
        const txnNote = txns ? ` · ${txns} transaction${txns === 1 ? '' : 's'}` : '';
        setImportStatus({
          ok: true,
          message: `Imported ${payload.imported ?? 0} · Skipped ${payload.skipped ?? 0} · Errors ${payload.errors ?? 0}${createdNote}${txnNote}`,
        });
        if (onDataChange) onDataChange();
      }
    } catch (err) {
      console.error('Questrade statement import failed:', err);
      setImportStatus({ ok: false, message: 'Upload failed' });
    } finally {
      setImporting(false);
      setTimeout(() => setImportStatus(null), 12000);
    }
  };

  // In-place Value History sub-view: swaps the whole modal to the selected asset's
  // value timeline (newest first), each dated point individually deletable.
  if (historyAsset) {
    const points = [...historyPoints].reverse();
    // Liability values display negative (debt owed), matching the rest of the app, so a
    // shrinking debt reads as the improvement (a +change) it is.
    const signedValue = (p) => signedAmount(historyAsset, p?.value);
    // Origin-point badge, reworded per kind: bucket purchase → Purchased / Invested / Borrowed,
    // a manual account's opening point → Opened.
    const purchaseTag = isDebtBucket ? 'Borrowed' : isInvestmentBucket ? 'Invested' : 'Purchased';
    return (
      <div className="modal-overlay">
        <div className={`modal-content settings-modal${isManual ? ' is-manual' : ' is-asset-group'}`} onClick={(e) => e.stopPropagation()}>
          <div className="modal-header">
            <button className="add-networth-back" onClick={() => setHistoryAsset(null)} aria-label="Back">
              <MdArrowBack size={20} />
            </button>
            <h3 className="modal-title">{historyAsset.name} · Value history</h3>
            <button className="modal-close" onClick={onClose}>✕</button>
          </div>
          <div className="modal-body">
            <p className="settings-asset-hint settings-value-history-hint">
              Every record behind this {isDebtBucket ? 'debt’s balance' : 'asset’s worth'}. Deleting a row removes that record from the value timeline; deleting the {costPointWord} also clears the initial cost basis.
            </p>
            {!historyLoaded ? (
              <p className="modal-note-muted modal-note-muted-centered">Loading…</p>
            ) : points.length === 0 ? (
              <p className="modal-note-muted modal-note-muted-centered">No value points yet.</p>
            ) : (
              <div className="settings-value-history">
                {points.map((point) => (
                  <div key={point.id} className="settings-value-history-row">
                    <span className="settings-value-history-date">{formatPurchaseDate(point.date)}</span>
                    {point.is_purchase && <span className="settings-value-history-tag">{purchaseTag}</span>}
                    {point.is_opening && <span className="settings-value-history-tag">Opened</span>}
                    <span className={`settings-value-history-value ${moneyTone(signedValue(point))}`}>
                      <FitMoney
                        full={`${formatMoney(signedValue(point), historyAsset.currency)} ${historyAsset.currency}`}
                        compact={`${formatCompactMoney(signedValue(point), historyAsset.currency)} ${historyAsset.currency}`}
                      />
                    </span>
                    <button
                      type="button"
                      className="settings-asset-delete settings-value-history-delete"
                      aria-label="Delete value point"
                      data-tooltip="Delete"
                      onClick={() => { setHistoryError(''); setDeletingPoint(point); }}
                    >
                      <MdDelete size={18} />
                    </button>
                  </div>
                ))}
              </div>
            )}
            {historyError && !deletingPoint && <p className="modal-error-block">{historyError}</p>}
          </div>
        </div>

        {deletingPoint && (
          <div className="modal-overlay modal-overlay-elevated">
            <div className="modal-content modal-dialog-card modal-dialog-wide">
              <p className="modal-2fa-text modal-dialog-title">Delete this value point?</p>
              <p className="modal-dialog-copy">
                The {formatPurchaseDate(deletingPoint.date)} value of {formatMoney(signedValue(deletingPoint), historyAsset.currency)} {historyAsset.currency} will be removed from the timeline{deletingPoint.is_purchase ? costBasisClearedText : ''}.
              </p>
              {historyError && <p className="modal-error-block">{historyError}</p>}
              <div className="modal-dialog-actions">
                <button className="btn-danger modal-dialog-action app-control-root" onClick={handleDeletePoint} disabled={historyBusy}>
                  <span className="app-control-label">{historyBusy ? 'Deleting…' : 'Yes, delete'}</span>
                </button>
                <button className="btn-secondary modal-dialog-action app-control-root" onClick={() => { setHistoryError(''); setDeletingPoint(null); }} disabled={historyBusy}>
                  <span className="app-control-label">Cancel</span>
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="modal-overlay">
      <div className={`modal-content settings-modal${isAssetGroup ? ' is-asset-group' : ''}${isManual ? ' is-manual' : ''}${isCash ? ' is-cash' : ''}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <InstitutionLogo name={institution.name} provider={institution.provider} logoUrl={logoHasLogo ? `${API}/institutions/${institution.id}/logo?v=${logoVersion}` : undefined} />
          <h3 className="modal-title">{institution.name} Settings</h3>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">
          {isManual && (
            <div className="settings-manual-logo-manager">
              <span className="subsection-title">Institution icon</span>
              <div className="settings-manual-logo-row">
                <span className="settings-manual-logo-preview">
                  {logoHasLogo
                    ? <AuthenticatedInstitutionImage src={`${API}/institutions/${institution.id}/logo?v=${logoVersion}`} alt="" />
                    : <span className="manual-inst-logo-initial">?</span>}
                </span>
                <button type="button" className="file-pick-btn app-control-root" disabled={logoBusy} onClick={() => logoFileInputRef.current?.click()}>
                  <span className="app-control-label">{logoBusy ? 'Saving…' : logoHasLogo ? 'Replace icon' : 'Add icon'}</span>
                </button>
                {logoHasLogo && (
                  <button type="button" className="manual-inst-logo-remove button-shell-opt-out" onClick={handleRemoveLogo} disabled={logoBusy}>
                    Remove
                  </button>
                )}
                <input
                  ref={logoFileInputRef}
                  type="file"
                  accept="image/png,image/svg+xml,image/jpeg,image/webp"
                  onChange={handleLogoFileChange}
                />
              </div>
              <span className="settings-manual-logo-hint">{MANUAL_INSTITUTION_ICON_HINT}</span>
              {logoError && <span className="settings-manual-logo-error">{logoError}</span>}
            </div>
          )}

          {/* Credentials Section */}
          {(apiConfig || hasSavedScraperCredentials) && (() => {
            const isIbkr = institution.provider === 'ibkr';
            const isQuestrade = institution.provider === 'questrade';
            const hasScraper = hasSavedScraperCredentials;
            const scraperLabels = SCRAPER_FIELD_LABELS[institution.provider] || { username: 'Username', password: 'Password' };
            const ibkrTabs = [
              { key: 'flex', label: 'Flex Query' },
              { key: 'credentials', label: 'Credentials' },
            ];
            const renderMoomooCloudAuthorization = () => (
              <>
                <div className="settings-instructions">
                  <p className="modal-desc">{renderBrandText('Your Moomoo account is connected to BreakTwenty through secure, read-only authorization. Balances, holdings, and transaction history can resync without another login.', 'moomoo-settings-intro')}</p>
                  <h4>How Moomoo authorization works:</h4>
                  <ol>
                    <li>{renderBrandMarkedText('**No Moomoo credentials to manage.** BreakTwenty never receives or stores your Moomoo email or password.', 'moomoo-settings-credentials')}</li>
                    <li>{renderBrandMarkedText('**Authorize once.** Moomoo issues BreakTwenty an encrypted refresh token after you approve account access.', 'moomoo-settings-authorize')}</li>
                    <li>{renderBrandMarkedText('**Login changes normally do not interrupt syncing.** Changing your Moomoo email or password does not usually invalidate this separate authorization.', 'moomoo-settings-login-change')}</li>
                    <li>{renderBrandMarkedText('Use **Reconnect Moomoo** only if authorization expires or is revoked, or if you want to change the authorized account or permissions.', 'moomoo-settings-reconnect')}</li>
                  </ol>
                  <p className="modal-desc">{renderBrandText('The refresh token is exchanged privately between Moomoo and BreakTwenty. You never need to find, copy, or paste it.', 'moomoo-settings-token')}</p>
                </div>
                <button
                  type="button"
                  className="btn-primary modal-section-action app-control-root"
                  onClick={() => onReconnect?.(institution)}
                >
                  <span className="app-control-label">Reconnect Moomoo</span>
                </button>
              </>
            );
            const renderApiFields = () => (
              <>
                {API_PROVIDER_CONFIG[institution.provider] && (
                  <div className="settings-instructions-dropdown">
                    <button
                      type="button"
                      className={`app-instructions ${showInstructions ? 'is-open' : ''}`.trim()}
                      aria-expanded={showInstructions}
                      onClick={() => setShowInstructions(prev => !prev)}
                    >
                      <span className="app-instructions-label">Setup Instructions</span>
                      <span className={`app-instructions-chevron modal-toggle-icon ${showInstructions ? 'is-open' : ''}`.trim()}>&#9660;</span>
                    </button>
                    {showInstructions && (
                      <div className="settings-instructions">
                        <p className="modal-desc">
                          {renderBrandText(API_PROVIDER_CONFIG[institution.provider].description, `${institution.provider}-settings-description`)}
                        </p>
                        <h4>{renderBrandText(API_PROVIDER_CONFIG[institution.provider].instructionsTitle, `${institution.provider}-settings-instructions-title`)}</h4>
                        <ol>
                          {API_PROVIDER_CONFIG[institution.provider].instructionsSteps.map((step, i) => (
                            <li key={i}>
                              {step.link ? (
                                <>
                                  {renderBrandMarkedText(step.text, `${institution.provider}-settings-step-${i}`)}
                                  <a href={step.link} target="_blank" rel="noreferrer" className="modal-inline-link">
                                    {renderBrandMarkedText(step.linkText, `${institution.provider}-settings-step-${i}-link`)}
                                  </a>
                                </>
                              ) : (
                                renderBrandMarkedText(step.text, `${institution.provider}-settings-step-${i}`)
                              )}
                            </li>
                          ))}
                        </ol>
                      </div>
                    )}
                  </div>
                )}
                {apiConfig.fields.map(field => {
                  const isSecret = field.secret !== false;
                  const isVisible = !isSecret || fieldVisibility[field.key] || false;
                  return (
                    <div key={field.key} className="settings-field">
                      <label>{field.label}</label>
                      <div className="modal-field-shell">
                        {field.multiline ? (
                          <textarea
                            className={`modal-mono-field modal-input-with-toggle ${isSecret ? '' : 'modal-input-no-toggle'}`.trim()}
                            value={credentials[field.key] || ""}
                            onChange={(e) => setCredentials((prev) => ({ ...prev, [field.key]: e.target.value }))}
                            rows={6}
                            style={{ WebkitTextSecurity: isVisible ? "none" : "disc" }}
                          />
                        ) : (
                          <input
                            className={`modal-input-with-toggle ${isSecret ? '' : 'modal-input-no-toggle'}`.trim()}
                            type={isVisible ? (field.type === "number" ? "number" : "text") : "password"}
                            value={credentials[field.key] || ""}
                            onChange={(e) => setCredentials((prev) => ({ ...prev, [field.key]: e.target.value }))}
                          />
                        )}
                        {isSecret && (
                          <button
                            type="button"
                            onClick={() => setFieldVisibility(prev => ({ ...prev, [field.key]: !isVisible }))}
                            className="modal-secret-toggle"
                            data-tooltip={isVisible ? "Hide" : "Show"}
                          >
                            {isVisible ? <MdVisibility size={18} /> : <MdVisibilityOff size={18} />}
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
                <p className="modal-note-muted modal-note-muted-centered">
                  {apiConfig.privacyText || (apiConfig.fields.some(field => field.secret !== false)
                    ? 'Your API keys are stored locally, are not sent to the developer, and are used only for syncing.'
                    : 'Your connection settings are stored locally, are not sent to the developer, and are used only for syncing.')}
                </p>
                {credError && (
                  <p className="modal-error-block">{credError}</p>
                )}
                <button className="btn-primary modal-section-action app-control-root" onClick={handleSaveCredentials} disabled={validating}>
                  <span className="app-control-label">
                    {validating ? "Validating..." : (apiConfig.fields.some(field => field.secret !== false) ? "Update Credentials" : "Update Settings")}
                  </span>
                </button>
                {credSuccess && (
                  <div className="modal-overlay modal-overlay-elevated">
                    <div className="modal-content modal-dialog-card">
                      <div className="modal-success-icon">✓</div>
                      <p className="modal-2fa-text">Credentials verified</p>
                    </div>
                  </div>
                )}
              </>
            );

            const renderScraperFields = () => (
              <>
                {['username', 'password'].map(field => {
                  const isVisible = scraperFieldVisibility[field] || false;
                  return (
                    <div key={field} className="settings-field">
                      <label>{scraperLabels[field]}</label>
                      <div className="modal-field-shell">
                        <input
                          className="modal-input-with-toggle"
                          type={isVisible ? "text" : "password"}
                          value={scraperCreds[field] || ""}
                          onChange={(e) => setScraperCreds(prev => ({ ...prev, [field]: e.target.value }))}
                        />
                        <button
                          type="button"
                          onClick={() => setScraperFieldVisibility(prev => ({ ...prev, [field]: !isVisible }))}
                          className="modal-secret-toggle"
                          data-tooltip={isVisible ? "Hide" : "Show"}
                        >
                          {isVisible ? <MdVisibility size={18} /> : <MdVisibilityOff size={18} />}
                        </button>
                      </div>
                    </div>
                  );
                })}
                {scraperError && (
                  <p className="modal-error-block">{scraperError}</p>
                )}
                <button className="btn-primary modal-section-action app-control-root" onClick={handleSaveScraperCreds} disabled={scraperSaving}>
                  <span className="app-control-label">{scraperSaving ? (validatesScraperCredentials ? "Validating..." : "Saving...") : "Save Credentials"}</span>
                </button>
                {scraperSuccess && (
                  <div className="modal-overlay modal-overlay-elevated">
                    <div className="modal-content modal-dialog-card">
                      <div className="modal-success-icon">✓</div>
                      <p className="modal-2fa-text">{validatesScraperCredentials ? "Credentials verified" : "Credentials saved"}</p>
                    </div>
                  </div>
                )}
              </>
            );

            return (
              <div className="settings-modal-section">
                {isIbkr ? (
                  <>
                    <div className="settings-modal-mode-buttons" role="tablist" aria-label="IBKR settings section">
                      {ibkrTabs.map((tab) => {
                        const isSelected = ibkrTab === tab.key;
                        return (
                          <button
                            key={tab.key}
                            type="button"
                            role="tab"
                            className={`settings-modal-mode-button app-control-root ${isSelected ? 'is-active' : ''}`.trim()}
                            aria-selected={isSelected}
                            onClick={() => setIbkrTab(tab.key)}
                          >
                            <span className="app-control-label">{tab.label}</span>
                          </button>
                        );
                      })}
                    </div>
                    {ibkrTab === 'flex' ? (
                      <>
                        {renderApiFields()}
                        <div className="settings-instructions-dropdown modal-section-action-full">
                          <button
                            type="button"
                            className={`app-instructions ${showImportInstructions ? 'is-open' : ''}`.trim()}
                            aria-expanded={showImportInstructions}
                            onClick={() => setShowImportInstructions(prev => !prev)}
                          >
                            <span className="app-instructions-label">Import history</span>
                            <span className={`app-instructions-chevron modal-toggle-icon ${showImportInstructions ? 'is-open' : ''}`.trim()}>&#9660;</span>
                          </button>
                          {showImportInstructions && (
                            <div className="settings-instructions">
                              <p className="modal-desc">The IBKR Flex Query daily sync covers up to the last 365 days of activity. To access your full account history from the date each account was opened — including all transactions and dividends, plus the daily account balances that extend your net-worth graph — you'll need to manually export and import the data below. A Flex Query run over a closed or transferred account is added here as an imported account.</p>
                              <h4>How to export historical data from IBKR:</h4>
                              <ol>
                                <li>Log into <a href="https://www.interactivebrokers.com/portal" target="_blank" rel="noreferrer" className="modal-inline-link">IBKR Account Management</a></li>
                                <li><span dangerouslySetInnerHTML={{ __html: 'Go to <strong>Performance &amp; Reports → Flex Queries</strong>' }} /></li>
                                <li><span dangerouslySetInnerHTML={{ __html: 'Click the <strong>Run</strong> (arrow) button next to your Activity Flex Query' }} /></li>
                                <li>Set the date range to cover the period before your daily sync window</li>
                                <li><span dangerouslySetInnerHTML={{ __html: 'Select <strong>XML</strong> as the output format and click <strong>Run</strong>' }} /></li>
                                <li>Save the downloaded files and upload them below — you can select multiple at once</li>
                              </ol>
                              <div className="modal-upload-group">
                                <input
                                  ref={fileInputRef}
                                  type="file"
                                  accept=".xml,application/xml,text/xml"
                                  multiple
                                  className="modal-upload-input-hidden"
                                  onChange={handleImportFile}
                                />
                                <button
                                  className="btn-primary app-control-root"
                                  onClick={() => { setImportStatus(null); fileInputRef.current?.click(); }}
                                  disabled={importing}
                                >
                                  <span className="app-control-label">{importing ? 'Importing…' : 'Upload Flex Reports'}</span>
                                </button>
                                {importStatus && (
                                  <span className={`modal-upload-status ${importStatus.ok ? 'is-success' : 'is-error'}`.trim()}>
                                    {importStatus.message}
                                  </span>
                                )}
                              </div>
                            </div>
                          )}
                        </div>
                      </>
                    ) : (
                      <>
                        <p className="modal-note-muted modal-note-muted-centered">Saved credentials remain on this device, are not sent to the developer, and are used to prefill and speed up login during manual syncing.</p>
                        {renderScraperFields()}
                      </>
                    )}
                  </>
                ) : useMoomooCloudOAuth ? (
                  renderMoomooCloudAuthorization()
                ) : apiConfig && !hasScraper ? (
                  <>
                    {renderApiFields()}
                    {isQuestrade && (
                      <div className="settings-instructions-dropdown modal-section-action-full">
                        <button
                          type="button"
                          className={`app-instructions ${showImportInstructions ? 'is-open' : ''}`.trim()}
                          aria-expanded={showImportInstructions}
                          onClick={() => setShowImportInstructions(prev => !prev)}
                        >
                          <span className="app-instructions-label">Import history</span>
                          <span className={`app-instructions-chevron modal-toggle-icon ${showImportInstructions ? 'is-open' : ''}`.trim()}>&#9660;</span>
                        </button>
                        {showImportInstructions && (
                          <div className="settings-instructions">
                            <p className="modal-desc">Questrade's API only reports current balances, so it can't backfill history on its own. Upload your monthly account statements (PDF) to extend the net-worth graph with month-end balances, back to when each account was opened. Statements for a closed or transferred account are added here as an imported account.</p>
                            <h4>How to download your statements from Questrade:</h4>
                            <ol>
                              <li>Log into <a href="https://login.questrade.com" target="_blank" rel="noreferrer" className="modal-inline-link">Questrade</a></li>
                              <li><span dangerouslySetInnerHTML={{ __html: 'Go to <strong>Reports → Documents</strong>' }} /></li>
                              <li>Download the monthly <strong>account statements</strong> (PDF) for each account and year</li>
                              <li>Upload them all below — you can select multiple files at once</li>
                            </ol>
                            <div className="modal-upload-group">
                              <input
                                ref={fileInputRef}
                                type="file"
                                accept=".pdf,application/pdf"
                                multiple
                                className="modal-upload-input-hidden"
                                onChange={handleQuestradeImport}
                              />
                              <button
                                className="btn-primary app-control-root"
                                onClick={() => { setImportStatus(null); fileInputRef.current?.click(); }}
                                disabled={importing}
                              >
                                <span className="app-control-label">{importing ? 'Importing…' : 'Upload Statements'}</span>
                              </button>
                              {importStatus && (
                                <span className={`modal-upload-status ${importStatus.ok ? 'is-success' : 'is-error'}`.trim()}>
                                  {importStatus.message}
                                </span>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </>
                ) : (
                  <>
                    <p className="modal-note-muted modal-note-muted-centered">Saved credentials remain on this device, are not sent to the developer, and are used to prefill and speed up login during manual syncing.</p>
                    {renderScraperFields()}
                  </>
                )}
              </div>
            );
          })()}

          {/* Imported accounts (e.g. closed/transferred Questrade accounts rebuilt from PDF
              statements) — reuse the manual value-timeline tools: review/delete monthly points,
              anchor a value at a date, or delete the account. */}
          {(() => {
            const importedAccounts = assets.filter((a) => a.is_imported);
            if (importedAccounts.length === 0) return null;
            return (
              <div className="settings-modal-section">
                <div className="settings-asset-header">
                  <h4 className="subsection-title">Imported accounts</h4>
                  <span className="settings-asset-hint">Reconstructed from imported statements. Open the value history to review or delete individual monthly points, set a value to anchor a date, or remove the account.</span>
                </div>
                <div className="settings-assets">
                  {importedAccounts.map((acc) => (
                    <div key={acc.id} className="settings-asset">
                      <div className="settings-asset-main">
                        <span className="settings-asset-name">{acc.name}</span>
                        {acc.account_type && (
                          <AccountTypeBadge accountType={acc.account_type} />
                        )}
                        {editingAssetId !== acc.id && (
                          <>
                            <span className={`settings-asset-value ${moneyTone(signedAssetValue(acc))}`}>
                              <FitMoney full={formatMoney(signedAssetValue(acc), acc.currency)} compact={formatCompactMoney(signedAssetValue(acc), acc.currency)} className="settings-asset-value-money" />
                              <span className="account-currency">{acc.currency}</span>
                            </span>
                            <div className="settings-asset-actions">
                              <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => startEditAsset(acc)}>
                                <span className="app-control-label">Update value</span>
                              </button>
                              <button type="button" className="settings-asset-icon-btn" aria-label="Value history" data-tooltip="Value history" onClick={() => openHistory(acc)}>
                                <MdHistory size={18} />
                              </button>
                              <button type="button" className="settings-asset-delete" aria-label="Delete account" data-tooltip="Delete" onClick={() => { setAssetError(''); setDeletingAsset(acc); }}>
                                <MdDelete size={18} />
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                      {editingAssetId === acc.id && (
                        <>
                          <div className="settings-asset-edit">
                            <MoneyInput className="settings-asset-input amount-input" value={editValue} placeholder="0.00" aria-label="New value" onChange={setEditValue} autoFocus />
                            <SingleDatePicker className="settings-asset-date" size="compact" value={editDate} onChange={setEditDate} placeholder="As-of date" ariaLabel="As-of date" />
                            <div className="settings-asset-edit-actions">
                              <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => { setEditingAssetId(null); setAssetError(''); }} disabled={assetBusy}>
                                <span className="app-control-label">Cancel</span>
                              </button>
                              <button type="button" className="btn-primary settings-asset-btn app-control-root" onClick={() => handleUpdateAssetValue(acc)} disabled={assetBusy}>
                                <span className="app-control-label">{assetBusy ? 'Saving…' : 'Save'}</span>
                              </button>
                            </div>
                          </div>
                          {assetError && <p className="modal-error-block">{assetError}</p>}
                        </>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}

          {/* Value-managed accounts: asset-group buckets (Real Estate, etc.) AND user
              manual institutions — list each, revalue (the revaluation door builds a
              value history), delete; manual institutions also get an add-account form. */}
          {isValueManaged && (
            <div className="settings-modal-section">
              {(isManual || (assetsLoaded && assets.length > 0)) && (
                <div className="settings-asset-header">
                  {isManual && <h4 className="subsection-title">Accounts</h4>}
                  {assetsLoaded && assets.length > 0 && (
                    <span className="settings-asset-hint">
                      {isManual
                        ? 'Value updates build account’s balance history, until transactions import overwrites it with its own data.'
                        : isDebtBucket
                          ? 'Updating debt’s value creates record of it at the chosen date, thus building the debt’s value history.'
                          : 'Updating asset’s value creates record of it at the chosen date, thus building the asset’s value history.'}
                    </span>
                  )}
                </div>
              )}
              {!assetsLoaded ? (
                <p className="modal-note-muted modal-note-muted-centered">Loading…</p>
              ) : assets.length === 0 ? (
                <p className="modal-note-muted modal-note-muted-centered">{isManual ? 'No accounts yet.' : 'No assets yet.'}</p>
              ) : (
                <div className="settings-assets">
                  {assets.map((asset) => (
                    <div key={asset.id} className="settings-asset">
                      <div className="settings-asset-main">
                        <span className="settings-asset-name">{asset.name}</span>
                        <AccountTypeBadge accountType={asset.account_type} />
                        {editingAssetId !== asset.id && (
                          <>
                            <span className={`settings-asset-value ${moneyTone(signedAssetValue(asset))}`}>
                              <FitMoney full={formatMoney(signedAssetValue(asset), asset.currency)} compact={formatCompactMoney(signedAssetValue(asset), asset.currency)} className="settings-asset-value-money" />
                              <span className="account-currency">{asset.currency}</span>
                            </span>
                            <div className="settings-asset-actions">
                              <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => startEditAsset(asset)}>
                                <span className="app-control-label">{isDebtBucket ? 'Update balance' : 'Update value'}</span>
                              </button>
                              <button
                                type="button"
                                className="settings-asset-icon-btn"
                                aria-label="Value history"
                                data-tooltip="Value history"
                                onClick={() => openHistory(asset)}
                              >
                                <MdHistory size={18} />
                              </button>
                              <button
                                type="button"
                                className="settings-asset-icon-btn"
                                aria-label="Edit currency and type"
                                data-tooltip={isManual && asset.has_transactions ? 'Edit type' : 'Edit currency & type'}
                                onClick={() => startEditDetails(asset)}
                              >
                                <MdEdit size={16} />
                              </button>
                              <button
                                type="button"
                                className="settings-asset-delete"
                                aria-label="Delete asset"
                                data-tooltip="Delete"
                                onClick={() => { setAssetError(''); setDeletingAsset(asset); }}
                              >
                                <MdDelete size={18} />
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                      {editingAssetId === asset.id && (
                        <>
                        <div className="settings-asset-edit">
                          <MoneyInput
                            className="settings-asset-input amount-input"
                            value={editValue}
                            placeholder="0.00"
                            aria-label="New value"
                            onChange={setEditValue}
                            autoFocus
                          />
                            <SingleDatePicker
                              className="settings-asset-date"
                              size="compact"
                              value={editDate}
                              onChange={setEditDate}
                            placeholder="As-of date"
                            ariaLabel="As-of date"
                          />
                          <div className="settings-asset-edit-actions">
                            <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => { setEditingAssetId(null); setAssetError(''); }} disabled={assetBusy}>
                              <span className="app-control-label">Cancel</span>
                            </button>
                            <button type="button" className="btn-primary settings-asset-btn app-control-root" onClick={() => handleUpdateAssetValue(asset)} disabled={assetBusy}>
                              <span className="app-control-label">{assetBusy ? 'Saving…' : 'Save'}</span>
                            </button>
                          </div>
                        </div>
                        {assetError && <p className="modal-error-block">{assetError}</p>}
                        </>
                      )}
                      {editingDetailsId === asset.id && (
                        <>
                          <div className="settings-asset-edit settings-asset-details-edit">
                            <Dropdown
                              className="settings-account-add-type"
                              value={detailsType}
                              options={isAssetGroup ? getAssetGroupAccountTypeOptions(institution.provider) : addAccountTypes}
                              fitToOptions
                              ariaLabel="Account type"
                              onChange={setDetailsType}
                            />
                            {!(isManual && asset.has_transactions) && (
                              <Dropdown
                                className="settings-account-add-ccy currency-dropdown"
                                value={detailsCurrency}
                                options={CURRENCY_OPTIONS}
                                ariaLabel="Currency"
                                onChange={setDetailsCurrency}
                              />
                            )}
                            <div className="settings-asset-edit-actions">
                              <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => { setEditingDetailsId(null); setDetailsError(''); }} disabled={detailsBusy}>
                                <span className="app-control-label">Cancel</span>
                              </button>
                              <button type="button" className="btn-primary settings-asset-btn app-control-root" onClick={() => handleSaveDetails(asset)} disabled={detailsBusy}>
                                <span className="app-control-label">{detailsBusy ? 'Saving…' : 'Save'}</span>
                              </button>
                            </div>
                          </div>
                          {detailsError && <p className="modal-error-block">{detailsError}</p>}
                        </>
                      )}
                      {isManual && (() => {
                        const acctIsDebt = asset.is_liability;
                        const openNoun = acctIsDebt ? 'amount borrowed' : 'opening balance';
                        const openAmtLabel = acctIsDebt ? 'Amount borrowed' : 'Opening balance';
                        const openDateLabel = acctIsDebt ? 'Borrowed date' : 'Opening date';
                        return (
                          <div className="settings-asset-purchase">
                            {editingOpeningId === asset.id ? (
                              <div className="settings-asset-edit">
                                <MoneyInput className="settings-asset-input amount-input" value={openingValue} placeholder="0.00" aria-label={openAmtLabel} onChange={setOpeningValue} autoFocus />
                                <SingleDatePicker className="settings-asset-date" size="compact" value={openingDate} onChange={setOpeningDate} placeholder={openDateLabel} ariaLabel={openDateLabel} />
                                <div className="settings-asset-edit-actions">
                                  <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => { setEditingOpeningId(null); setOpeningError(''); }} disabled={openingBusy}>
                                    <span className="app-control-label">Cancel</span>
                                  </button>
                                  <button type="button" className="btn-primary settings-asset-btn app-control-root" onClick={() => handleSetManualOpening(asset)} disabled={openingBusy}>
                                    <span className="app-control-label">{openingBusy ? 'Saving…' : 'Save'}</span>
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <div className="settings-asset-purchase-row">
                                <span className="settings-asset-purchase-info">
                                  {asset.opening_balance != null ? (
                                    <>
                                      {acctIsDebt ? 'Borrowed' : 'Opened'} {asset.opening_balance_date ? formatPurchaseDate(asset.opening_balance_date) : 'date not set'} ·{' '}
                                      <span className={`settings-asset-purchase-amount ${moneyTone(signedAmount(asset, asset.opening_balance))}`}>
                                        <FitMoney full={`${formatMoney(signedAmount(asset, asset.opening_balance), asset.currency)} ${asset.currency}`} compact={`${formatCompactMoney(signedAmount(asset, asset.opening_balance), asset.currency)} ${asset.currency}`} />
                                      </span>
                                    </>
                                  ) : (
                                    `${openAmtLabel} and date not set`
                                  )}
                                </span>
                                <button type="button" className="btn-primary settings-asset-add-purchase app-control-root" onClick={() => startEditManualOpening(asset)}>
                                  {asset.opening_balance != null ? (
                                    <span className="app-control-label">{`Update ${openNoun}`}</span>
                                  ) : (
                                    <>
                                      <span className="app-control-icon" aria-hidden="true"><MdAdd /></span>
                                      <span className="app-control-label">{`Add ${openNoun}`}</span>
                                    </>
                                  )}
                                </button>
                              </div>
                            )}
                            {openingError && editingOpeningId === asset.id && <p className="modal-error-block">{openingError}</p>}
                          </div>
                        );
                      })()}
                      {isAssetGroup && (!asset.is_liability || isDebtBucket) && (
                      <div className="settings-asset-purchase">
                        {editingPurchaseId === asset.id ? (
                          <div className="settings-asset-edit">
                            <MoneyInput
                              className="settings-asset-input amount-input"
                              value={purchaseDraftValue}
                              placeholder="0.00"
                              aria-label={costValueLabelCap}
                              onChange={setPurchaseDraftValue}
                              autoFocus
                            />
                            <SingleDatePicker
                              className="settings-asset-date"
                              size="compact"
                              value={purchaseDraftDate}
                              onChange={setPurchaseDraftDate}
                              placeholder={costDateLabelCap}
                              ariaLabel={costDateLabelCap}
                            />
                            <div className="settings-asset-edit-actions">
                              <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => { setEditingPurchaseId(null); setPurchaseError(''); }} disabled={purchaseBusy}>
                                <span className="app-control-label">Cancel</span>
                              </button>
                              <button type="button" className="btn-primary settings-asset-btn app-control-root" onClick={() => handleSetPurchase(asset)} disabled={purchaseBusy}>
                                <span className="app-control-label">{purchaseBusy ? 'Saving…' : 'Save'}</span>
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="settings-asset-purchase-row">
                            <span className="settings-asset-purchase-info">
                              {asset.purchase ? (
                                <>
                                  {costVerbPast} {formatPurchaseDate(asset.purchase.date)} ·{' '}
                                  <span className={`settings-asset-purchase-amount ${moneyTone(signedAmount(asset, asset.purchase.value))}`}>
                                    <FitMoney full={`${formatMoney(signedAmount(asset, asset.purchase.value), asset.currency)} ${asset.currency}`} compact={`${formatCompactMoney(signedAmount(asset, asset.purchase.value), asset.currency)} ${asset.currency}`} />
                                  </span>
                                </>
                              ) : (
                                costUnsetText
                              )}
                            </span>
                            <button type="button" className="btn-primary settings-asset-add-purchase app-control-root" onClick={() => startEditPurchase(asset)}>
                              {asset.purchase ? (
                                <span className="app-control-label">{`Update ${costValueLabel}`}</span>
                              ) : (
                                <>
                                  <span className="app-control-icon" aria-hidden="true"><MdAdd /></span>
                                  <span className="app-control-label">{`Add ${costValueLabel}`}</span>
                                </>
                              )}
                            </button>
                          </div>
                        )}
                        {purchaseError && editingPurchaseId === asset.id && (
                          <p className="modal-error-block">{purchaseError}</p>
                        )}
                      </div>
                      )}
                      {isAssetGroup && !asset.is_liability && (
                      <div className="settings-asset-purchase">
                        {linkingAssetId === asset.id ? (
                          <>
                            {allLiabilities.length === 0 ? (
                              <p className="modal-note-muted">No loans or debts yet — add one to link it here.</p>
                            ) : (
                              <div className="settings-asset-link-list">
                                {allLiabilities.map((loan) => {
                                  const linkedElsewhere = loan.secured_asset_account_id && loan.secured_asset_account_id !== asset.id;
                                  const otherName = linkedElsewhere ? (accountsById.get(loan.secured_asset_account_id)?.name || 'another asset') : null;
                                  const loanInst = institutionsById.get(loan.institution_id);
                                  const signed = signedAmount(loan, loan.balance);
                                  return (
                                    <label key={loan.id} className="settings-asset-link-row">
                                      <input type="checkbox" checked={linkSelection.has(loan.id)} onChange={() => toggleLinkSelection(loan.id)} />
                                      <span className="settings-asset-link-logo">
                                        <InstitutionLogo
                                          name={loan.institution || ''}
                                          provider={loanInst?.provider}
                                          logoUrl={loanInst?.has_logo ? `${API}/institutions/${loanInst.id}/logo` : undefined}
                                          size={18}
                                        />
                                      </span>
                                      <span className="settings-asset-link-name">{loan.name}</span>
                                      <AccountTypeBadge accountType={loan.account_type} />
                                      <span className={`settings-asset-link-amount ${moneyTone(signed)}`}>
                                        {formatMoney(signed, loan.currency)} {loan.currency}
                                      </span>
                                      {linkedElsewhere && <span className="settings-asset-link-to">linked to {otherName}</span>}
                                    </label>
                                  );
                                })}
                              </div>
                            )}
                            <div className="settings-asset-edit">
                              <div className="settings-asset-edit-actions">
                                <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => { setLinkingAssetId(null); setLinkError(''); }} disabled={linkBusy}>
                                  <span className="app-control-label">Cancel</span>
                                </button>
                                <button type="button" className="btn-primary settings-asset-btn app-control-root" onClick={() => handleSaveLinks(asset)} disabled={linkBusy}>
                                  <span className="app-control-label">{linkBusy ? 'Saving…' : 'Save'}</span>
                                </button>
                              </div>
                            </div>
                            {linkError && <p className="modal-error-block">{linkError}</p>}
                          </>
                        ) : (
                          <div className="settings-asset-purchase-row">
                            <span className="settings-asset-purchase-info">
                              {linkedLoansFor(asset.id).length === 0 ? 'No loans linked' : (
                                <>
                                  Equity{' '}
                                  <span className={`settings-asset-purchase-amount ${moneyTone(equityFor(asset))}`}>
                                    <FitMoney full={`${formatMoney(equityFor(asset), asset.currency)} ${asset.currency}`} compact={`${formatCompactMoney(equityFor(asset), asset.currency)} ${asset.currency}`} />
                                  </span>
                                  {' '}· secured by {linkedLoansFor(asset.id).map((loan) => loan.name).join(', ')}
                                </>
                              )}
                            </span>
                            <button type="button" className="btn-primary settings-asset-add-purchase app-control-root" onClick={() => startEditLinks(asset)}>
                              {linkedLoansFor(asset.id).length > 0 ? (
                                <span className="app-control-label">Manage loans</span>
                              ) : (
                                <>
                                  <span className="app-control-icon" aria-hidden="true"><MdAdd /></span>
                                  <span className="app-control-label">Link loans</span>
                                </>
                              )}
                            </button>
                          </div>
                        )}
                      </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {isManual && (
                addingAccount ? (
                  <div className="settings-account-add">
                    <input
                      className="settings-asset-input settings-account-add-name"
                      value={newAccount.name}
                      placeholder="Account name"
                      aria-label="Account name"
                      onChange={(e) => setNewAccount((n) => ({ ...n, name: e.target.value }))}
                      autoFocus
                    />
                    <Dropdown
                      className="settings-account-add-type"
                      value={newAccount.account_type}
                      options={addAccountTypes}
                      fitToOptions
                      ariaLabel="Account type"
                      onChange={(v) => setNewAccount((n) => ({ ...n, account_type: v }))}
                    />
                    <Dropdown
                      className="settings-account-add-ccy currency-dropdown"
                      value={newAccount.currency}
                      options={CURRENCY_OPTIONS}
                      ariaLabel="Currency"
                      onChange={(v) => setNewAccount((n) => ({ ...n, currency: v }))}
                    />
                    <MoneyInput
                      className="settings-asset-input settings-account-add-value amount-input"
                      value={newAccount.value}
                      placeholder="0.00"
                      aria-label="Balance"
                      onChange={(v) => setNewAccount((n) => ({ ...n, value: v }))}
                    />
                    <div className="settings-account-add-actions">
                      <button type="button" className="btn-primary settings-asset-btn app-control-root" onClick={handleAddAccount} disabled={addBusy}>
                        <span className="app-control-label">{addBusy ? 'Adding…' : 'Add'}</span>
                      </button>
                      <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => { setAddingAccount(false); setAddError(''); }} disabled={addBusy}>
                        <span className="app-control-label">Cancel</span>
                      </button>
                    </div>
                  </div>
                ) : (
                  <button type="button" className="settings-account-add-btn button-shell-opt-out" onClick={openAddAccount}>+ Add Account</button>
                )
              )}
              {addError && !deletingAsset && <p className="modal-error-block">{addError}</p>}
            </div>
          )}

          {/* Cash holder: per-currency cash accounts — set the opening (starting) balance
              or delete a currency holder. Currency/type/value-history don't apply. */}
          {isCash && (
            <div className="settings-modal-section">
              <div className="settings-asset-header">
                <h4 className="subsection-title">Cash</h4>
                {assetsLoaded && assets.length > 0 && (
                  <span className="settings-asset-hint">
                    Set the starting cash you held before tracking — day-to-day changes come from transactions.
                  </span>
                )}
              </div>
              {!assetsLoaded ? (
                <p className="modal-note-muted modal-note-muted-centered">Loading…</p>
              ) : assets.length === 0 ? (
                <p className="modal-note-muted modal-note-muted-centered">No cash yet. Add a cash transaction to start.</p>
              ) : (
                <div className="settings-assets">
                  {assets.map((asset) => (
                    <div key={asset.id} className="settings-asset">
                      <div className="settings-asset-main">
                        <span className="settings-asset-name">{asset.name}</span>
                        {editingOpeningId !== asset.id && (
                          <>
                            <span className={`settings-asset-value ${moneyTone(signedAssetValue(asset))}`}>
                              <FitMoney full={formatMoney(signedAssetValue(asset), asset.currency)} compact={formatCompactMoney(signedAssetValue(asset), asset.currency)} className="settings-asset-value-money" />
                              <span className="account-currency">{asset.currency}</span>
                            </span>
                            <div className="settings-asset-actions">
                              <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => startEditOpening(asset)}>
                                <span className="app-control-label">{asset.cash_opening ? 'Edit opening balance' : 'Set opening balance'}</span>
                              </button>
                              <button
                                type="button"
                                className="settings-asset-delete"
                                aria-label="Delete cash holder"
                                data-tooltip="Delete"
                                onClick={() => { setAssetError(''); setDeletingAsset(asset); }}
                              >
                                <MdDelete size={18} />
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                      {editingOpeningId === asset.id && (
                        <>
                          <div className="settings-asset-edit">
                            <MoneyInput
                              className="settings-asset-input amount-input"
                              value={openingValue}
                              placeholder="0.00"
                              aria-label="Opening balance"
                              onChange={setOpeningValue}
                            />
                            <SingleDatePicker
                              className="settings-asset-date"
                              size="compact"
                              value={openingDate}
                              onChange={setOpeningDate}
                              placeholder="As-of date"
                              ariaLabel="As-of date"
                            />
                            <div className="settings-asset-edit-actions">
                              <button type="button" className="btn-secondary settings-asset-btn app-control-root" onClick={() => { setEditingOpeningId(null); setOpeningError(''); }} disabled={openingBusy}>
                                <span className="app-control-label">Cancel</span>
                              </button>
                              <button type="button" className="btn-primary settings-asset-btn app-control-root" onClick={() => handleSetOpening(asset)} disabled={openingBusy}>
                                <span className="app-control-label">{openingBusy ? 'Saving…' : 'Save'}</span>
                              </button>
                            </div>
                          </div>
                          {openingError && <p className="modal-error-block">{openingError}</p>}
                        </>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {assetError && !deletingAsset && <p className="modal-error-block">{assetError}</p>}
            </div>
          )}

          {/* Danger Zone — hidden for the Cash singleton (it's managed per-currency above,
              and re-creates itself on the next cash transaction anyway). */}
          {!isCash && (
            <div className="settings-modal-section">
              <div className="modal-danger-zone">
                <button className="btn-danger app-control-root" onClick={() => {
                  setDeleteError('');
                  setDeleteConfirm(true);
                }}>
                  <span className="app-control-label">{isValueManaged ? `Delete ${institution.name}` : 'Delete Institution'}</span>
                </button>
              </div>
            </div>
          )}

          {deletingAsset && (
            <div className="modal-overlay modal-overlay-elevated">
              <div className="modal-content modal-dialog-card modal-dialog-wide">
                <p className="modal-2fa-text modal-dialog-title">Delete {deletingAsset.name}?</p>
                <p className="modal-dialog-copy">{isCash ? 'Its logged cash transactions and opening balance will be removed.' : 'Its value history will be lost.'}</p>
                {assetError && (
                  <p className="modal-error-block">{assetError}</p>
                )}
                <div className="modal-dialog-actions">
                  <button className="btn-danger modal-dialog-action app-control-root" onClick={handleDeleteAsset} disabled={assetBusy}>
                    <span className="app-control-label">{assetBusy ? 'Deleting...' : 'Yes, delete'}</span>
                  </button>
                  <button className="btn-secondary modal-dialog-action app-control-root" onClick={() => { setAssetError(''); setDeletingAsset(null); }} disabled={assetBusy}>
                    <span className="app-control-label">Cancel</span>
                  </button>
                </div>
              </div>
            </div>
          )}

          {deleteConfirm && (
            <div className="modal-overlay modal-overlay-elevated">
              <div className="modal-content modal-dialog-card modal-dialog-wide">
                <p className="modal-2fa-text modal-dialog-title">Are you sure you want to delete {institution.name}?</p>
                <p className="modal-dialog-copy institution-delete-warning">
                  This permanently wipes all {institution.name} data from {APP_BRAND_NAME}. This cannot be undone.
                </p>
                {deleteError && (
                  <p className="modal-error-block">{deleteError}</p>
                )}
                <div className="modal-dialog-actions">
                  <button className="btn-danger modal-dialog-action app-control-root" onClick={handleDelete} disabled={deleting}>
                    <span className="app-control-label">{deleting ? 'Deleting...' : 'Yes, delete'}</span>
                  </button>
                  <button className="btn-secondary modal-dialog-action app-control-root" onClick={() => {
                    setDeleteError('');
                    setDeleteConfirm(false);
                  }}>
                    <span className="app-control-label">Cancel</span>
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default InstitutionSettingsModal;
