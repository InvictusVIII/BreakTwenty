import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MdWarningAmber } from 'react-icons/md';
import { API } from '../config';
import ControlChevron from '../components/ControlChevron';
import { downloadAllData, downloadCsv } from '../utils/exportCsv';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import {
  checkDesktopUpdates,
  clearDesktopUpdateToken,
  downloadDesktopUpdate,
  getDesktopUpdateStatus,
  installDesktopUpdate,
  isDesktopShell,
  saveDesktopUpdateToken,
  subscribeDesktopUpdateStatus,
} from '../utils/desktopBridge';
import {
  DEFAULT_USER_TIME_FORMAT,
  DEFAULT_USER_TIMEZONE,
  USER_TIME_FORMAT_OPTIONS,
  getAvailableTimezones,
  normalizeUserTimeFormat,
} from '../utils/timezone';
import { APP_BRAND_NAME } from '../constants/brand';
import HorizontalScrollProxy from '../components/HorizontalScrollProxy';
import './Settings.css';

const DEFAULT_MARKET_DATA_PROVIDERS = [
  { key: 'fmp', label: 'FMP' },
  { key: 'polygon_massive', label: 'Polygon / Massive' },
  { key: 'twelve_data', label: 'Twelve Data' },
];
const SETTINGS_CARD_HORIZONTAL_SCROLL_PROXY_OPTIONS = {
  className: 'settings-card-horizontal-scrollbar app-horizontal-scroll-proxy',
  innerClassName: 'settings-card-horizontal-scrollbar-inner app-horizontal-scroll-proxy-inner',
  contentWidthProperty: '--app-horizontal-scroll-content-width',
  targetViewportProperty: '--app-horizontal-scroll-viewport-width',
  resetOnTargetWidthChange: true,
  clampTolerance: 0,
  resolveTarget: (controller) => (
    controller.closest('.settings-card')?.querySelector('.settings-card-scroll-viewport')
  ),
  getContentElements: ({ target }) => [target.querySelector('.settings-card-scroll-surface')],
  getObservedElements: ({ target, contentElements }) => [
    target.closest('.settings-card'),
    target,
    ...contentElements,
  ],
};

function SettingsCardHorizontalScrollProxy() {
  return <HorizontalScrollProxy options={SETTINGS_CARD_HORIZONTAL_SCROLL_PROXY_OPTIONS} />;
}

function SettingsCard({ className, title, children }) {
  return (
    <div className={`panel-shell settings-card ${className || ''}`.trim()}>
      <h3 className="settings-card-title">{title}</h3>
      <div className="settings-card-scroll-frame">
        <SettingsCardHorizontalScrollProxy />
        <div className="settings-card-scroll-viewport">
          <div className="settings-card-scroll-surface">
            {children}
          </div>
        </div>
      </div>
    </div>
  );
}

function getProviderLabel(providerKey, providers = DEFAULT_MARKET_DATA_PROVIDERS) {
  return providers.find((provider) => provider.key === providerKey)?.label || 'FMP';
}

function getTimeFormatLabel(timeFormat) {
  return USER_TIME_FORMAT_OPTIONS.find((option) => option.key === timeFormat)?.label || '24-hour';
}

function SettingsDropdown({
  id,
  value,
  options,
  onChange,
  disabled = false,
}) {
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef(null);
  const normalizedOptions = useMemo(() => (
    (options || []).map((option) => {
      if (typeof option === 'string') {
        return { key: option, label: option };
      }
      return {
        key: String(option.key ?? ''),
        label: String(option.label ?? option.key ?? ''),
      };
    }).filter((option) => option.key)
  ), [options]);
  const selectedOption = normalizedOptions.find((option) => option.key === value) || normalizedOptions[0] || null;
  const closeDropdown = useCallback(() => setIsOpen(false), []);

  useDismissibleLayer({
    open: isOpen,
    ref: dropdownRef,
    onDismiss: closeDropdown,
    pointerEvent: 'mousedown',
  });

  const handleTriggerKeyDown = (event) => {
    if (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown') {
      event.preventDefault();
      if (!disabled) {
        setIsOpen(true);
      }
    }
  };

  const handleOptionSelect = (optionKey) => {
    onChange(optionKey);
    setIsOpen(false);
  };

  return (
    <div className={`settings-dropdown ${isOpen ? 'is-open' : ''}`.trim()} ref={dropdownRef}>
      <button
        id={id}
        type="button"
        className="settings-dropdown-trigger app-control-root"
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        disabled={disabled || normalizedOptions.length === 0}
        onClick={() => setIsOpen((previous) => !previous)}
        onKeyDown={handleTriggerKeyDown}
      >
        <span className="settings-dropdown-value app-control-label">{selectedOption?.label || 'Select'}</span>
        <span className="settings-dropdown-icon app-control-chevron" aria-hidden="true">
          <ControlChevron />
        </span>
      </button>
      {isOpen && (
        <div
          className="settings-dropdown-menu is-open"
          role="listbox"
          aria-labelledby={id}
        >
          <div className="settings-dropdown-option-list">
            {normalizedOptions.map((option) => {
              const isSelected = option.key === value;
              return (
                <button
                  key={option.key}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  className={`settings-dropdown-option app-control-root ${isSelected ? 'is-selected' : ''}`.trim()}
                  onClick={() => handleOptionSelect(option.key)}
                >
                  <span className="settings-dropdown-option-label app-control-label">{option.label}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function formatUpdateBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const amount = bytes / (1024 ** unitIndex);
  return `${amount.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function Settings({
  showPageTitle = true,
  onTimezoneChange = null,
  onTimeFormatChange = null,
  devToolsEnabled = false,
  promoDemoActive = false,
  onTogglePromoDemo = null,
  onOpenWelcome = null,
}) {
  const timezoneOptions = useMemo(() => getAvailableTimezones(), []);
  const desktopShell = useMemo(() => isDesktopShell(), []);
  const [timezoneStatus, setTimezoneStatus] = useState({
    loading: true,
    savedTimezone: DEFAULT_USER_TIMEZONE,
    savedTimeFormat: DEFAULT_USER_TIME_FORMAT,
  });
  const [userTimezone, setUserTimezone] = useState(DEFAULT_USER_TIMEZONE);
  const [userTimeFormat, setUserTimeFormat] = useState(DEFAULT_USER_TIME_FORMAT);
  const [timezoneSaving, setTimezoneSaving] = useState(false);
  const [timezoneMessage, setTimezoneMessage] = useState({ type: '', text: '' });
  const [marketDataStatus, setMarketDataStatus] = useState({
    loading: true,
    hasKey: false,
    maskedKey: null,
    mode: 'keyless',
    source: 'none',
    configRevision: '',
    provider: 'fmp',
    providerLabel: 'FMP',
    availableProviders: DEFAULT_MARKET_DATA_PROVIDERS,
  });
  const [marketDataProvider, setMarketDataProvider] = useState('fmp');
  const [marketDataKey, setMarketDataKey] = useState('');
  const [marketDataKeyVisible, setMarketDataKeyVisible] = useState(false);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [marketDataMessage, setMarketDataMessage] = useState({ type: '', text: '' });
  const [exportIncludeHidden, setExportIncludeHidden] = useState(true);
  const [exportingKey, setExportingKey] = useState('');
  const [importMessage, setImportMessage] = useState({ type: '', text: '' });
  const [updateStatus, setUpdateStatus] = useState(null);
  const [updateBusy, setUpdateBusy] = useState('');
  const [updateToken, setUpdateToken] = useState('');
  const [updateTokenSaving, setUpdateTokenSaving] = useState(false);
  const [updateMessage, setUpdateMessage] = useState({ type: '', text: '' });
  const onTimezoneChangeRef = useRef(onTimezoneChange);
  const onTimeFormatChangeRef = useRef(onTimeFormatChange);

  useEffect(() => {
    onTimezoneChangeRef.current = onTimezoneChange;
  }, [onTimezoneChange]);

  useEffect(() => {
    onTimeFormatChangeRef.current = onTimeFormatChange;
  }, [onTimeFormatChange]);

  const applyTimezoneSettings = useCallback((data = {}) => {
    const savedTimezone = String(data.user_timezone || '').trim() || DEFAULT_USER_TIMEZONE;
    const savedTimeFormat = normalizeUserTimeFormat(data.user_time_format);
    setTimezoneStatus({
      loading: false,
      savedTimezone,
      savedTimeFormat,
    });
    setUserTimezone(savedTimezone);
    setUserTimeFormat(savedTimeFormat);
    setTimezoneMessage({ type: '', text: '' });
    if (typeof onTimezoneChangeRef.current === 'function') {
      onTimezoneChangeRef.current(savedTimezone);
    }
    if (typeof onTimeFormatChangeRef.current === 'function') {
      onTimeFormatChangeRef.current(savedTimeFormat);
    }
  }, []);

  const applyMarketDataStatus = useCallback((data = {}, fallbackSource = 'none') => {
    const availableProviders = Array.isArray(data.available_providers) && data.available_providers.length > 0
      ? data.available_providers
      : DEFAULT_MARKET_DATA_PROVIDERS;
    const provider = data.provider || 'fmp';
    const providerLabel = data.provider_label || getProviderLabel(provider, availableProviders);

    setMarketDataStatus({
      loading: false,
      hasKey: Boolean(data.has_key),
      maskedKey: data.masked_key || null,
      mode: data.mode || 'keyless',
      source: data.source || fallbackSource,
      configRevision: data.config_revision || '',
      provider,
      providerLabel,
      availableProviders,
    });
    setMarketDataProvider(provider);
    setMarketDataMessage({ type: '', text: '' });
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadTimezoneSettings = async () => {
      try {
        const resp = await fetch(`${API}/settings`);
        if (!resp.ok) {
          throw new Error('Timezone settings request failed');
        }
        const data = await resp.json();
        if (!cancelled) {
          applyTimezoneSettings(data);
        }
      } catch (_) {
        if (!cancelled) {
          applyTimezoneSettings({
            user_timezone: DEFAULT_USER_TIMEZONE,
            user_time_format: DEFAULT_USER_TIME_FORMAT,
          });
          setTimezoneMessage({ type: 'error', text: 'Failed to load timezone settings.' });
        }
      }
    };

    const loadMarketDataStatus = async () => {
      try {
        const resp = await fetch(`${API}/settings/market-data`);
        if (!resp.ok) {
          throw new Error('Market data settings request failed');
        }
        const data = await resp.json();
        if (data.status !== 'ok') {
          throw new Error(data.message || 'Market data settings request failed');
        }
        if (!cancelled) {
          applyMarketDataStatus(data, 'none');
        }
      } catch (_) {
        if (!cancelled) {
          setMarketDataStatus((previous) => ({
            ...previous,
            loading: false,
            mode: 'unavailable',
            source: 'unknown',
          }));
          setMarketDataMessage({ type: 'error', text: 'Failed to load market data settings.' });
        }
      }
    };

    loadTimezoneSettings();
    loadMarketDataStatus();

    return () => {
      cancelled = true;
    };
  }, [applyMarketDataStatus, applyTimezoneSettings]);

  useEffect(() => {
    if (!desktopShell) {
      return undefined;
    }
    let cancelled = false;
    const applyStatus = (status) => {
      if (!cancelled) {
        setUpdateStatus(status);
      }
    };

    getDesktopUpdateStatus().then(applyStatus).catch(() => {
      applyStatus({
        status: 'error',
        message: 'Failed to load desktop update status.',
        canCheck: false,
        canDownload: false,
        canInstall: false,
      });
    });
    const unsubscribe = subscribeDesktopUpdateStatus(applyStatus);

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [desktopShell, promoDemoActive]);

  const handleSaveTimezone = async () => {
    const normalizedTimeFormat = normalizeUserTimeFormat(userTimeFormat);
    setTimezoneSaving(true);
    setTimezoneMessage({ type: '', text: '' });
    try {
      const resp = await fetch(`${API}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_timezone: userTimezone,
          user_time_format: normalizedTimeFormat,
        }),
      });
      const data = await resp.json();
      if (!resp.ok || data.status !== 'ok') {
        setTimezoneMessage({ type: 'error', text: data.message || 'Failed to save timezone settings.' });
        return;
      }
      setTimezoneStatus({
        loading: false,
        savedTimezone: userTimezone,
        savedTimeFormat: normalizedTimeFormat,
      });
      setUserTimeFormat(normalizedTimeFormat);
      if (typeof onTimezoneChangeRef.current === 'function') {
        onTimezoneChangeRef.current(userTimezone);
      }
      if (typeof onTimeFormatChangeRef.current === 'function') {
        onTimeFormatChangeRef.current(normalizedTimeFormat);
      }
      setTimezoneMessage({
        type: 'success',
        text: `Time preferences saved as ${userTimezone}, ${getTimeFormatLabel(normalizedTimeFormat)}.`,
      });
    } catch (_) {
      setTimezoneMessage({ type: 'error', text: 'Failed to save timezone settings.' });
    } finally {
      setTimezoneSaving(false);
    }
  };

  const handleSave = async () => {
    const trimmedKey = marketDataKey.trim();
    if (!trimmedKey) {
      setMarketDataMessage({ type: 'error', text: 'API key cannot be empty.' });
      return;
    }

    setSaving(true);
    setMarketDataMessage({ type: '', text: '' });
    try {
      const resp = await fetch(`${API}/settings/market-data`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: marketDataProvider, api_key: trimmedKey }),
      });
      const data = await resp.json();
      if (data.status !== 'ok') {
        setMarketDataMessage({ type: 'error', text: data.message || 'Failed to save market data settings.' });
        return;
      }
      applyMarketDataStatus(data, 'user');
      setMarketDataKey('');
      setMarketDataKeyVisible(false);
      setMarketDataMessage({ type: 'success', text: `${data.provider_label || getProviderLabel(marketDataProvider)} API key saved.` });
    } catch (_) {
      setMarketDataMessage({ type: 'error', text: 'Failed to save market data settings.' });
    } finally {
      setSaving(false);
    }
  };

  const handleRemove = async () => {
    setRemoving(true);
    setMarketDataMessage({ type: '', text: '' });
    try {
      const resp = await fetch(`${API}/settings/market-data`, { method: 'DELETE' });
      const data = await resp.json();
      if (data.status !== 'ok') {
        setMarketDataMessage({ type: 'error', text: data.message || 'Failed to remove market data API key.' });
        return;
      }
      applyMarketDataStatus(data, 'none');
      setMarketDataKey('');
      setMarketDataKeyVisible(false);
      setMarketDataMessage({ type: 'success', text: 'Market data API key removed.' });
    } catch (_) {
      setMarketDataMessage({ type: 'error', text: 'Failed to remove market data API key.' });
    } finally {
      setRemoving(false);
    }
  };

  const handleExport = async (dataset) => {
    setExportingKey(dataset);
    setImportMessage({ type: '', text: '' });
    try {
      await downloadCsv(
        dataset,
        exportIncludeHidden ? { include_hidden: 'true', include_internal_ids: 'true' } : {},
      );
      setImportMessage({ type: 'success', text: `${dataset === 'networth-history' ? 'Net worth' : dataset[0].toUpperCase() + dataset.slice(1)} export downloaded.` });
    } catch (_) {
      setImportMessage({ type: 'error', text: 'Export failed. Is the backend running?' });
    } finally {
      setExportingKey('');
    }
  };

  const handleExportAll = async () => {
    setExportingKey('all');
    setImportMessage({ type: '', text: '' });
    try {
      await downloadAllData();
      setImportMessage({
        type: 'success',
        text: 'All four data exports downloaded in one ZIP. Store it somewhere secure.',
      });
    } catch (_) {
      setImportMessage({ type: 'error', text: 'Data export failed. Is the backend running?' });
    } finally {
      setExportingKey('');
    }
  };

  const runUpdateAction = async (busyKey, action) => {
    setUpdateBusy(busyKey);
    setUpdateMessage({ type: '', text: '' });
    try {
      const status = await action();
      setUpdateStatus(status);
      if (status?.status === 'error') {
        setUpdateMessage({ type: 'error', text: status.message || 'Update action failed.' });
      }
    } catch (_) {
      setUpdateMessage({ type: 'error', text: 'Update action failed.' });
    } finally {
      setUpdateBusy('');
    }
  };

  const handleSaveUpdateToken = async () => {
    const token = updateToken.trim();
    if (!token) {
      setUpdateMessage({ type: 'error', text: 'Paste a GitHub token before saving.' });
      return;
    }

    setUpdateTokenSaving(true);
    setUpdateMessage({ type: '', text: '' });
    try {
      const status = await saveDesktopUpdateToken(token);
      setUpdateStatus(status);
      if (status?.actionStatus === 'ok') {
        setUpdateToken('');
        setUpdateMessage({ type: 'success', text: status.actionMessage || 'Private update token saved.' });
      } else {
        setUpdateMessage({ type: 'error', text: status?.actionMessage || 'Could not save the update token.' });
      }
    } catch (_) {
      setUpdateMessage({ type: 'error', text: 'Could not save the update token.' });
    } finally {
      setUpdateTokenSaving(false);
    }
  };

  const handleClearUpdateToken = async () => {
    setUpdateTokenSaving(true);
    setUpdateMessage({ type: '', text: '' });
    try {
      const status = await clearDesktopUpdateToken();
      setUpdateStatus(status);
      if (status?.actionStatus === 'ok') {
        setUpdateToken('');
        setUpdateMessage({ type: 'success', text: status.actionMessage || 'Stored update token removed.' });
      } else {
        setUpdateMessage({ type: 'error', text: status?.actionMessage || 'Could not remove the update token.' });
      }
    } catch (_) {
      setUpdateMessage({ type: 'error', text: 'Could not remove the update token.' });
    } finally {
      setUpdateTokenSaving(false);
    }
  };

  const statusCopy = useMemo(() => {
    if (marketDataStatus.loading) {
      return 'Loading market data settings…';
    }
    if (marketDataStatus.source === 'user' && marketDataStatus.maskedKey) {
      return `Saved ${marketDataStatus.providerLabel} key: ${marketDataStatus.maskedKey}`;
    }
    if (marketDataStatus.source === 'app' && marketDataStatus.maskedKey) {
      return `App-managed ${marketDataStatus.providerLabel} backend key active: ${marketDataStatus.maskedKey}`;
    }
    if (marketDataStatus.mode === 'keyless') {
      return `Keyless mode active. ${APP_BRAND_NAME} will use FRED, CoinGecko, and local cached public data where available.`;
    }
    if (marketDataStatus.mode === 'unavailable') {
      return 'Market data settings are currently unavailable. You can enter and save a new API key below.';
    }
    return `No ${marketDataStatus.providerLabel} API key saved for this user.`;
  }, [marketDataStatus]);

  const updateStatusCopy = useMemo(() => {
    if (!desktopShell) {
      return '';
    }
    if (!updateStatus) {
      return 'Loading update status...';
    }
    if (updateStatus.status === 'downloading' && updateStatus.progress) {
      const transferred = formatUpdateBytes(updateStatus.progress.transferred);
      const total = formatUpdateBytes(updateStatus.progress.total);
      return `${updateStatus.message || 'Downloading update.'} ${transferred} of ${total}`;
    }
    return updateStatus.message || 'Ready to check for updates.';
  }, [desktopShell, updateStatus]);

  const updatePercent = updateStatus?.progress?.percent
    ? Math.max(0, Math.min(100, updateStatus.progress.percent))
    : 0;
  const updateChecking = updateBusy === 'check' || updateStatus?.status === 'checking';
  const updateDownloading = updateBusy === 'download' || updateStatus?.status === 'downloading';
  const updateInstalling = updateBusy === 'install' || updateStatus?.status === 'installing';
  const showUpdateProgress = updateDownloading || updateInstalling;
  const updateProgressIndeterminate = updateInstalling && !updateDownloading;
  const updateFeedPrivate = updateStatus?.auth?.feed?.private === true;
  const updateFeedPublic = updateStatus?.auth?.feed?.private === false;
  const showUpdateTokenControls = !updateFeedPublic;
  const updateAuthCopy = useMemo(() => {
    const auth = updateStatus?.auth;
    if (!auth) {
      return 'Private release token: loading.';
    }
    if (auth.feed?.private === false) {
      return '';
    }
    if (auth.hasToken) {
      return auth.source === 'environment'
        ? 'Private release token: active from GH_TOKEN or GITHUB_TOKEN.'
        : 'Private release token: saved on this device.';
    }
    return 'Private release token: not saved.';
  }, [updateStatus]);

  return (
    <>
      {showPageTitle && <h2 className="page-top-title settings-page-title">Settings</h2>}
      <div className="settings-section settings-grid">
        <SettingsCard className="settings-pos-2" title="Timezone">
          <div className="settings-status">
            <span>
              {timezoneStatus.loading
                ? 'Loading timezone settings…'
                : `Saved timezone: ${timezoneStatus.savedTimezone} · ${getTimeFormatLabel(timezoneStatus.savedTimeFormat)}`}
            </span>
          </div>
          <div className="settings-field settings-field-timezone">
            <label htmlFor="user-timezone">Timezone</label>
            <SettingsDropdown
              id="user-timezone"
              value={userTimezone}
              options={timezoneOptions}
              onChange={setUserTimezone}
            />
          </div>
          <div className="settings-field settings-field-time-format">
            <label htmlFor="user-time-format">Time Format</label>
            <SettingsDropdown
              id="user-time-format"
              value={userTimeFormat}
              options={USER_TIME_FORMAT_OPTIONS}
              onChange={(nextValue) => setUserTimeFormat(normalizeUserTimeFormat(nextValue))}
            />
          </div>
          <div className="settings-actions">
            <button
              type="button"
              className="btn-primary app-control-root"
              onClick={handleSaveTimezone}
              disabled={
                timezoneSaving
                || timezoneStatus.loading
                || (
                  userTimezone === timezoneStatus.savedTimezone
                  && normalizeUserTimeFormat(userTimeFormat) === timezoneStatus.savedTimeFormat
                )
              }
            >
              <span className="app-control-label">{timezoneSaving ? 'Saving…' : 'Save Time Preferences'}</span>
            </button>
          </div>
          {timezoneMessage.text && (
            <div className={`settings-message ${timezoneMessage.type}`.trim()}>
              {timezoneMessage.text}
            </div>
          )}
        </SettingsCard>
        {desktopShell && (
          <SettingsCard className="settings-pos-1" title="App Updates">
            <div className="settings-update-meta">
              <span>Current version: {updateStatus?.currentVersion || 'Unknown'}</span>
              {updateStatus?.updateInfo?.version && (
                <span>Available version: {updateStatus.updateInfo.version}</span>
              )}
            </div>
            <div className={`settings-status ${updateStatus?.status === 'available' || updateStatus?.status === 'downloaded' ? 'has-key' : ''}`.trim()}>
              <span>{updateStatusCopy}</span>
            </div>
            {showUpdateProgress && (
              <div
                className={`settings-update-progress ${updateProgressIndeterminate ? 'is-indeterminate' : ''}`.trim()}
                aria-label={updateProgressIndeterminate ? 'Update installation progress' : 'Update download progress'}
              >
                <span style={updateProgressIndeterminate ? undefined : { width: `${updatePercent}%` }} />
              </div>
            )}
            {showUpdateTokenControls && (
              <div className="settings-field">
                <label htmlFor="desktop-update-token">GitHub Token</label>
                <div className="settings-secret-row">
                  <input
                    id="desktop-update-token"
                    type="password"
                    value={updateToken}
                    onChange={(event) => setUpdateToken(event.target.value)}
                    placeholder={updateStatus?.auth?.hasToken ? 'Token saved for private release checks' : 'Paste a fine-grained GitHub token'}
                    autoComplete="off"
                    spellCheck="false"
                  />
                  <button
                    type="button"
                    className="btn-secondary settings-inline-btn app-control-root"
                    onClick={handleSaveUpdateToken}
                    disabled={
                      updateTokenSaving
                      || updateChecking
                      || updateDownloading
                      || updateInstalling
                      || !updateToken.trim()
                      || updateStatus?.auth?.storageAvailable === false
                    }
                  >
                    <span className="app-control-label">{updateTokenSaving ? 'Saving...' : 'Save Token'}</span>
                  </button>
                  {updateStatus?.auth?.hasStoredToken && (
                    <button
                      type="button"
                      className="btn-secondary settings-inline-btn app-control-root"
                      onClick={handleClearUpdateToken}
                      disabled={updateTokenSaving || updateChecking || updateDownloading || updateInstalling}
                    >
                      <span className="app-control-label">Clear Token</span>
                    </button>
                  )}
                </div>
              </div>
            )}
            {updateAuthCopy && (
              <p className="settings-note">
                {updateFeedPrivate ? `${updateAuthCopy} Use this only for private release testing.` : updateAuthCopy}
              </p>
            )}
            {showUpdateTokenControls && updateStatus?.auth?.error && (
              <div className="settings-message error">
                {updateStatus.auth.error}
              </div>
            )}
            <div className="settings-actions">
              <button
                type="button"
                className="btn-secondary app-control-root"
                onClick={() => runUpdateAction('check', checkDesktopUpdates)}
                disabled={!updateStatus?.canCheck || updateChecking || updateDownloading || updateInstalling}
              >
                <span className="app-control-label">{updateChecking ? 'Checking...' : 'Check for Updates'}</span>
              </button>
              {updateStatus?.canDownload && (
                <button
                  type="button"
                  className="btn-primary app-control-root"
                  onClick={() => runUpdateAction('download', downloadDesktopUpdate)}
                  disabled={updateDownloading || updateInstalling}
                >
                  <span className="app-control-label">{updateDownloading ? 'Downloading...' : 'Download Update'}</span>
                </button>
              )}
              {updateStatus?.canInstall && (
                <button
                  type="button"
                  className="btn-primary app-control-root"
                  onClick={() => runUpdateAction('install', installDesktopUpdate)}
                  disabled={updateInstalling}
                >
                  <span className="app-control-label">{updateInstalling ? 'Installing...' : 'Restart to Update'}</span>
                </button>
              )}
            </div>
            {updateMessage.text && (
              <div className={`settings-message ${updateMessage.type}`.trim()}>
                {updateMessage.text}
              </div>
            )}
          </SettingsCard>
        )}
        <SettingsCard className="settings-pos-3" title="Market Data">
          <p className="settings-desc">
            Save one market data API key so BreakTwenty can classify tickers into sectors when possible, identify funds
            and ETFs for cleaner Holdings breakdowns, pull benchmark history, and power the ticker strip on the Dashboard.
            FMP is strongest for sector and fund-aware classification, Polygon / Massive is available for alternate index
            data, and Twelve Data can be tested for quotes, commodities, crypto, and benchmark coverage.
          </p>
          <div className={`settings-status ${marketDataStatus.hasKey ? 'has-key' : ''}`.trim()}>
            <span>{statusCopy}</span>
          </div>
          <div className="settings-field settings-field-market-provider">
            <label htmlFor="market-data-provider">Provider</label>
            <SettingsDropdown
              id="market-data-provider"
              value={marketDataProvider}
              options={marketDataStatus.availableProviders}
              onChange={setMarketDataProvider}
            />
          </div>
          <div className="settings-field">
            <label htmlFor="market-data-api-key">{getProviderLabel(marketDataProvider, marketDataStatus.availableProviders)} API Key</label>
            <div className="settings-secret-row">
              <input
                id="market-data-api-key"
                type={marketDataKeyVisible ? 'text' : 'password'}
                value={marketDataKey}
                onChange={(event) => setMarketDataKey(event.target.value)}
                placeholder={marketDataStatus.hasKey ? 'Enter a new API key to replace the saved one' : 'Paste your API key'}
                autoComplete="off"
                spellCheck="false"
              />
              <button
                type="button"
                className="btn-secondary settings-inline-btn app-control-root"
                onClick={() => setMarketDataKeyVisible((previous) => !previous)}
                disabled={!marketDataKey}
              >
                <span className="app-control-label">{marketDataKeyVisible ? 'Hide' : 'Show'}</span>
              </button>
            </div>
          </div>
          <div className="settings-actions">
            <button
              type="button"
              className="btn-primary app-control-root"
              onClick={handleSave}
              disabled={saving || removing || !marketDataKey.trim()}
            >
              <span className="app-control-label">{marketDataStatus.hasKey ? 'Update Key' : 'Save Key'}</span>
            </button>
            {marketDataStatus.source === 'user' && marketDataStatus.hasKey && (
              <button
                type="button"
                className="btn-secondary app-control-root"
                onClick={handleRemove}
                disabled={saving || removing}
              >
                <span className="app-control-label">Remove Key</span>
              </button>
            )}
          </div>
          <p className="settings-note">
            Your market data API key is stored locally, is not sent to the developer,
            and is used only in requests from this device to the selected market data provider.
          </p>
          {marketDataMessage.text && (
            <div className={`settings-message ${marketDataMessage.type}`.trim()}>
              {marketDataMessage.text}
            </div>
          )}
        </SettingsCard>
        <SettingsCard className="settings-pos-4" title="Data Export & Recovery">
          <p className="settings-desc">
            Export readable copies of your financial data for safekeeping or spreadsheet use. These exports are not a
            full BreakTwenty backup and cannot recreate every app setting or connection.
          </p>
          <label className="settings-checkbox-row">
            <input
              type="checkbox"
              checked={exportIncludeHidden}
              onChange={(event) => setExportIncludeHidden(event.target.checked)}
            />
            <span>Include hidden accounts and institutions</span>
          </label>
          <p className="settings-checkbox-hint">Applies to individual CSV buttons. Export All Data always includes hidden records.</p>
          <div className="settings-recovery-warning" role="note" aria-label="Recovery limitation">
            <MdWarningAmber className="settings-recovery-warning-icon" aria-hidden="true" />
            <div>
              <p className="settings-recovery-warning-title">Recovery limitation</p>
              <p className="settings-recovery-warning-copy">
                BreakTwenty does not currently provide a portable app backup or full restore. Your{' '}
                <code>breaktwenty.db</code> file can&apos;t be opened on its own. BreakTwenty also needs a separate digital
                key, which is protected by your computer and user account. Because the database and digital key belong
                together, copying the database—or even the whole app data folder—may not work on another computer. If
                you lose your computer, user account, or digital key, BreakTwenty may no longer
                be able to unlock the database.
              </p>
              <p className="settings-recovery-warning-copy">
                Use <strong>Export All Data</strong> to retain all four readable CSV datasets. The ZIP and CSV files are
                unencrypted and contain sensitive financial information, so store them securely.
              </p>
            </div>
          </div>
          <div className="settings-export-row">
            <button type="button" className="btn-primary app-control-root settings-export-all" onClick={handleExportAll} disabled={!!exportingKey}>
              <span className="app-control-label">{exportingKey === 'all' ? 'Exporting…' : 'Export All Data'}</span>
            </button>
            <button type="button" className="btn-secondary app-control-root" onClick={() => handleExport('transactions')} disabled={!!exportingKey}>
              <span className="app-control-label">{exportingKey === 'transactions' ? '…' : 'Transactions'}</span>
            </button>
            <button type="button" className="btn-secondary app-control-root" onClick={() => handleExport('balances')} disabled={!!exportingKey}>
              <span className="app-control-label">{exportingKey === 'balances' ? '…' : 'Balances'}</span>
            </button>
            <button type="button" className="btn-secondary app-control-root" onClick={() => handleExport('holdings')} disabled={!!exportingKey}>
              <span className="app-control-label">{exportingKey === 'holdings' ? '…' : 'Holdings'}</span>
            </button>
            <button type="button" className="btn-secondary app-control-root" onClick={() => handleExport('networth-history')} disabled={!!exportingKey}>
              <span className="app-control-label">{exportingKey === 'networth-history' ? '…' : 'Net Worth'}</span>
            </button>
          </div>
          {importMessage.text && (
            <div className={`settings-message ${importMessage.type}`.trim()}>
              {importMessage.text}
            </div>
          )}
          <p className="settings-note">
            Transaction and balance CSVs can be imported into a manually created account from <strong>Add to Net Worth</strong>,
            but that import is not a full restore and does not recreate account configuration or every exported field.
          </p>
        </SettingsCard>
        {devToolsEnabled && (
          <SettingsCard className="settings-pos-5" title="Developer Demo">
            <p className="settings-desc">
              Open the onboarding demo or switch into the editable promo screenshot environment.
            </p>
            <div className="settings-status">
              <span>{promoDemoActive ? 'Promo environment is active.' : 'Promo environment is off.'}</span>
            </div>
            <div className="settings-actions">
              <button
                type="button"
                className={`${promoDemoActive ? 'btn-primary' : 'btn-secondary'} app-control-root`.trim()}
                onClick={() => { if (onTogglePromoDemo) void onTogglePromoDemo(); }}
              >
                <span className="app-control-label">{promoDemoActive ? 'Exit Promo' : 'Open Promo'}</span>
              </button>
              <button
                type="button"
                className="btn-secondary app-control-root"
                onClick={() => { if (onOpenWelcome) void onOpenWelcome(); }}
              >
                <span className="app-control-label">Open Welcome</span>
              </button>
            </div>
          </SettingsCard>
        )}
      </div>
    </>
  );
}

export default Settings;
