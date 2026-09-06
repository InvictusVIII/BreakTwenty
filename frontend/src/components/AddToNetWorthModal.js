import React, { useState, useEffect, useMemo } from 'react';
import { MdSearch, MdArrowBack } from 'react-icons/md';
import InstitutionLogo from './InstitutionLogo';
import { API } from '../config';
import { LOGO_ASSETS } from '../assets/logoAssets';
import { getProviderAddAuthModal, getProviderAddCategory } from '../constants/providers';

// Level-1 launcher tiles — one flat grid, Wealthica-style (no section headers,
// since debt isn't exclusive to manual entry — synced banks/brokerages carry it too).
//  kind 'providers'   -> drills into the syncable-provider picker for that catalog category
//  kind 'manual'      -> opens the manual-institution wizard
//  kind 'asset_group' -> opens the tangible-asset form for that connector-less bucket
//  kind 'cash'        -> opens the wallet Cash opening-balance form
const LAUNCHER_ICON_SIZE = 36;

const LAUNCHER_TILES = [
  { key: 'bank_brokerage', label: 'Banks & Brokerages', logoAsset: 'bank-brokerage.png', kind: 'providers' },
  { key: 'crypto_wallet', label: 'Crypto & Wallets', logoAsset: 'crypto-wallet.png', kind: 'providers' },
  { key: 'manual_institution', label: 'Manual Institutions', logoAsset: 'manual-institution.png', kind: 'manual' },
  { key: 'real_estate', label: 'Real Estate', logoAsset: 'real-estate.png', kind: 'asset_group' },
  { key: 'vehicles', label: 'Vehicles', logoAsset: 'vehicles.png', kind: 'asset_group' },
  { key: 'valuables', label: 'Valuables', logoAsset: 'valuables.png', kind: 'asset_group' },
  { key: 'private_investments', label: 'Private Investments', logoAsset: 'private-investments.png', kind: 'asset_group' },
  { key: 'other_assets', label: 'Other Assets', logoAsset: 'other-assets.png', kind: 'asset_group' },
  { key: 'cash', label: 'Cash', logoAsset: 'cash.png', kind: 'cash' },
  { key: 'debt', label: 'Debt', logoAsset: 'debt.png', kind: 'asset_group' },
];

function LauncherTile({ tile, onClick }) {
  const Icon = tile.icon;
  const iconSize = tile.iconSize || LAUNCHER_ICON_SIZE;
  return (
    <button
      type="button"
      className={`add-networth-tile is-${tile.key}`}
      onClick={onClick}
    >
      <span className="add-networth-tile-icon">
        {tile.logoAsset
          ? (
            <InstitutionLogo
              name={tile.label}
              logoUrl={LOGO_ASSETS[tile.logoAsset]}
              size={iconSize}
              staged
              stageKey={tile.logoAsset}
            />
          )
          : <Icon size={iconSize} aria-hidden="true" />}
      </span>
      <span className="add-networth-tile-label">{tile.label}</span>
    </button>
  );
}

// The "Add to Net Worth" launcher. Level 1 is the category grid; selecting a
// syncable category (Banks & Brokerages, Crypto & Wallets) drills into the
// provider picker. Manual Institutions hands off to the existing wizard.
function AddToNetWorthModal({ existingProviders = [], onClose, onAuthNeeded, onAddManual, onAddAssetGroup, onAddCash }) {
  const [available, setAvailable] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [view, setView] = useState(null); // null = grid; otherwise a provider-category key

  useEffect(() => {
    fetch(`${API}/institutions/available`)
      .then((r) => r.json())
      .then((data) => {
        setAvailable(data.filter(
          (i) => !existingProviders.includes(i.provider) && getProviderAddAuthModal(i.provider),
        ));
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [existingProviders]);

  const handleTile = (tile) => {
    if (tile.kind === 'manual') { onClose(); onAddManual(); return; }
    if (tile.kind === 'asset_group') { onClose(); onAddAssetGroup(tile.key); return; }
    if (tile.kind === 'cash') { onClose(); onAddCash(); return; }
    if (tile.kind === 'providers') { setSearch(''); setView(tile.key); }
  };

  const handleSelectProvider = (inst) => {
    if (!inst.implemented || !getProviderAddAuthModal(inst.provider)) return;
    onClose();
    onAuthNeeded(inst);
  };

  const activeTile = view ? LAUNCHER_TILES.find((t) => t.key === view) : null;
  const providersForView = useMemo(
    () => (view ? available.filter((inst) => getProviderAddCategory(inst.provider) === view) : []),
    [view, available],
  );
  const filteredProviders = providersForView.filter(
    (inst) => inst.name.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div className="modal-overlay">
      <div className="modal-content add-networth-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          {view && (
            <button className="add-networth-back" onClick={() => setView(null)} aria-label="Back">
              <MdArrowBack size={20} />
            </button>
          )}
          <h3 className="modal-title">{view ? activeTile.label : 'Add to your Net Worth'}</h3>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        <div className="modal-body add-networth-body">
          {!view && (
            <div className="add-networth-grid">
              {LAUNCHER_TILES.map((tile) => (
                <LauncherTile key={tile.key} tile={tile} onClick={() => handleTile(tile)} />
              ))}
            </div>
          )}

          {view && (
            loading ? (
              <p className="no-data">Loading...</p>
            ) : providersForView.length === 0 ? (
              <p className="no-data">No supported institutions are available</p>
            ) : (
              <>
                <div className="add-inst-search">
                  <MdSearch size={18} className="add-inst-search-icon" />
                  <input
                    type="text"
                    placeholder="Search institutions..."
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    className="add-inst-search-input"
                    autoFocus
                  />
                </div>
                <div className="add-inst-list">
                  {filteredProviders.map((inst) => (
                    <div
                      key={inst.provider}
                      className="add-inst-item"
                      onClick={() => handleSelectProvider(inst)}
                      role="button"
                      tabIndex={0}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          handleSelectProvider(inst);
                        }
                      }}
                    >
                      <span className="add-inst-logo-slot"><InstitutionLogo name={inst.name} size={32} /></span>
                      <span className="add-inst-name">{inst.name}</span>
                    </div>
                  ))}
                </div>
              </>
            )
          )}
        </div>
      </div>
    </div>
  );
}

export default AddToNetWorthModal;
