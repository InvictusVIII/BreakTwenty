import React, { useMemo, useState } from 'react';
import { LuLandmark } from 'react-icons/lu';
import { MdCheck, MdSearch } from 'react-icons/md';
import InstitutionLogo from './InstitutionLogo';
import { API } from '../config';
import ControlChevron from './ControlChevron';
import AccountTypeBadge from './AccountTypeBadge';
import { ACCOUNT_TYPE_LABELS } from '../constants/providers';

function joinClassNames(...values) {
  return values.filter(Boolean).join(' ');
}

function getInstitutionAccountIds(institution) {
  return institution.accountIds;
}

function getDefaultAccountTypeLabel(account) {
  return String(ACCOUNT_TYPE_LABELS[account.account_type] || account.account_type || '').trim();
}

function normalizeSearchText(value) {
  return String(value || '').trim().toLowerCase();
}

function getAccountDisplayName(account) {
  return String(account?.name || 'Unnamed account').trim();
}

function getAccountSearchText(account, institution) {
  return normalizeSearchText([
    institution?.name,
    institution?.provider,
    getAccountDisplayName(account),
    account?.currency,
    getDefaultAccountTypeLabel(account),
  ].filter(Boolean).join(' '));
}

function formatScopeCount(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

export function formatScopeSelectionSummary({
  totalInstitutions,
  selectedInstitutions,
  selectedAccounts,
}) {
  if (selectedAccounts === 0) {
    return 'No sources selected';
  }

  const accountLabel = formatScopeCount(selectedAccounts, 'Account');

  if (selectedInstitutions === totalInstitutions) {
    return `${formatScopeCount(totalInstitutions, 'Institution')} · ${accountLabel}`;
  }

  return `${selectedInstitutions} of ${totalInstitutions} institutions · ${accountLabel}`;
}

export function ScopeSelectorTrigger({
  isOpen = false,
  summary,
  onClick,
  className = '',
  controls,
  ariaLabel = 'Scope',
}) {
  return (
    <button
      type="button"
      className={joinClassNames('scope-selector-trigger', 'app-control-root', isOpen ? 'is-open' : '', className)}
      aria-haspopup="dialog"
      aria-expanded={isOpen}
      aria-controls={controls}
      aria-label={ariaLabel}
      onClick={onClick}
    >
      <span className="scope-selector-icon app-control-icon" aria-hidden="true">
        <LuLandmark />
      </span>
      <span className="scope-selector-summary app-control-label">{summary}</span>
      <span className={`scope-selector-chevron app-control-chevron ${isOpen ? 'is-open' : ''}`.trim()} aria-hidden="true">
        <ControlChevron />
      </span>
    </button>
  );
}

function InstitutionAccountSelector({
  institutions,
  selectedAccountIds,
  onToggleInstitution,
  onToggleAccount,
  showAccountTypeBadge = true,
  className = '',
  headerActions = null,
}) {
  const [query, setQuery] = useState('');
  const [viewMode, setViewMode] = useState('institutions');

  const selectedAccountIdSet = useMemo(() => {
    if (selectedAccountIds instanceof Set) {
      return selectedAccountIds;
    }

    return new Set(selectedAccountIds || []);
  }, [selectedAccountIds]);

  const normalizedQuery = normalizeSearchText(query);

  const enrichedInstitutions = useMemo(() => (
    (institutions || []).map((institution) => {
      const accounts = institution.accounts || [];
      const accountIds = getInstitutionAccountIds(institution);
      const selectedCount = accountIds.filter((accountId) => selectedAccountIdSet.has(accountId)).length;
      const isAllSelected = accountIds.length > 0 && selectedCount === accountIds.length;
      const isPartiallySelected = selectedCount > 0 && !isAllSelected;

      return {
        ...institution,
        accounts,
        accountIds,
        selectedCount,
        isAllSelected,
        isPartiallySelected,
        isSelected: selectedCount > 0,
      };
    })
  ), [institutions, selectedAccountIdSet]);

  const totalAccountCount = enrichedInstitutions.reduce((total, institution) => total + institution.accountIds.length, 0);
  const selectedAccountCount = enrichedInstitutions.reduce((total, institution) => total + institution.selectedCount, 0);

  const institutionEntries = useMemo(() => (
    enrichedInstitutions
      .map((institution) => {
        const institutionSearchText = normalizeSearchText([institution.name, institution.provider].filter(Boolean).join(' '));
        const hasMatchingAccount = institution.accounts.some((account) => (
          getAccountSearchText(account, institution).includes(normalizedQuery)
        ));
        const institutionMatches = !normalizedQuery || institutionSearchText.includes(normalizedQuery);

        return {
          institution,
          isVisible: institutionMatches || hasMatchingAccount,
        };
      })
      .filter((entry) => entry.isVisible)
  ), [enrichedInstitutions, normalizedQuery]);

  const accountEntries = useMemo(() => (
    enrichedInstitutions.flatMap((institution) => (
      institution.accounts
        .filter((account) => (
          !normalizedQuery
          || getAccountSearchText(account, institution).includes(normalizedQuery)
        ))
        .map((account) => ({
          account,
          institution,
          isSelected: selectedAccountIdSet.has(account.id),
        }))
    ))
  ), [enrichedInstitutions, normalizedQuery, selectedAccountIdSet]);

  const renderAccountTypeBadge = (account) => {
    const accountTypeLabel = showAccountTypeBadge ? getDefaultAccountTypeLabel(account) : '';
    return accountTypeLabel ? (
      <AccountTypeBadge
        accountType={account.account_type}
        label={accountTypeLabel}
        className="institution-account-selector-account-type"
      />
    ) : null;
  };

  const renderAccountRow = (account, institution, isSelected, classNameSuffix = '') => (
    <button
      key={account.id}
      type="button"
      className={joinClassNames('institution-account-selector-account-row', classNameSuffix, isSelected ? 'is-active' : '')}
      aria-pressed={isSelected}
      onClick={() => onToggleAccount(account.id, account)}
    >
      <span className="institution-account-selector-account-check" aria-hidden="true">
        {isSelected ? <MdCheck size={14} /> : null}
      </span>
      <span className="institution-account-selector-account-copy">
        <span className="institution-account-selector-account-name">{getAccountDisplayName(account)}</span>
        {institution ? (
          <span className="institution-account-selector-account-meta">{institution.name}</span>
        ) : null}
      </span>
      {renderAccountTypeBadge(account)}
    </button>
  );

  return (
    <div className={joinClassNames('institution-account-selector', `is-${viewMode}-view`, className)}>
      <div className="institution-account-selector-topbar">
        <div className="institution-account-selector-topbar-row institution-account-selector-topbar-actions">
          <div className="institution-account-selector-tabs" role="tablist" aria-label="Source filter mode">
            <button
              type="button"
              role="tab"
              aria-selected={viewMode === 'institutions'}
              className={joinClassNames('institution-account-selector-tab', 'app-control-root', viewMode === 'institutions' ? 'is-active' : '')}
              onClick={() => setViewMode('institutions')}
            >
              <span className="app-control-label">Institutions</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={viewMode === 'accounts'}
              className={joinClassNames('institution-account-selector-tab', 'app-control-root', viewMode === 'accounts' ? 'is-active' : '')}
              onClick={() => setViewMode('accounts')}
            >
              <span className="app-control-label">Accounts</span>
            </button>
          </div>

          {headerActions ? (
            <div className="institution-account-selector-header-actions">
              {headerActions}
            </div>
          ) : null}
        </div>

        <label className="institution-account-selector-search">
          <span className="institution-account-selector-search-icon" aria-hidden="true">
            <MdSearch size={17} />
          </span>
          <input
            type="search"
            value={query}
            aria-label="Search institutions and accounts"
            placeholder="Search"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>

      {selectedAccountCount === 0 && totalAccountCount > 0 ? (
        <div className="institution-account-selector-empty-selection">No sources selected</div>
      ) : null}

      <div className="institution-account-selector-scroll">
        {viewMode === 'institutions' ? (
          <div className="institution-account-selector-grid">
            {institutionEntries.length > 0 ? institutionEntries.map((entry) => {
              const { institution } = entry;
              const stateClass = institution.isAllSelected
                ? 'is-on'
                : institution.isPartiallySelected
                  ? 'is-partial'
                  : 'is-off';

              return (
                <button
                  key={institution.key}
                  type="button"
                  className={joinClassNames('institution-account-selector-tile', stateClass)}
                  aria-pressed={institution.isPartiallySelected ? 'mixed' : institution.isAllSelected}
                  aria-label={institution.name}
                  data-tooltip={institution.name}
                  data-tooltip-hover-only
                  onClick={() => onToggleInstitution(institution)}
                >
                  <InstitutionLogo
                    name={institution.name}
                    provider={institution.provider}
                    logoUrl={institution.has_logo ? `${API}/institutions/${institution.id}/logo` : undefined}
                    size={32}
                  />
                </button>
              );
            }) : (
              <div className="institution-account-selector-empty">No matching sources</div>
            )}
          </div>
        ) : (
          <div className="institution-account-selector-flat-list" role="list">
            {accountEntries.length > 0 ? accountEntries.map(({ account, institution, isSelected }) => (
              <div key={account.id} role="listitem">
                <span className="institution-account-selector-flat-logo" aria-hidden="true">
                  <InstitutionLogo
                    name={institution.name}
                    provider={institution.provider}
                    logoUrl={institution.has_logo ? `${API}/institutions/${institution.id}/logo` : undefined}
                    size={20}
                  />
                </span>
                {renderAccountRow(account, institution, isSelected, 'is-flat')}
              </div>
            )) : (
              <div className="institution-account-selector-empty">No matching accounts</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default InstitutionAccountSelector;
