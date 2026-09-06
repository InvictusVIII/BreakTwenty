import React from 'react';
import { MdReceiptLong } from 'react-icons/md';
import CategoryPill from './CategoryPill';
import FitMoney from './FitMoney';
import HorizontalScrollProxy from './HorizontalScrollProxy';
import InstitutionLogo from './InstitutionLogo';
import { formatShortDateValue } from '../utils/date';
import { formatSignedCompactMoney, formatSignedDollarAmount } from '../utils/format';
import { getTransactionPrimaryDescription } from '../utils/transactionDescription';
import './RecentTransactionsTable.css';

const HORIZONTAL_SCROLL_PROXY_OPTIONS = {
  className: 'recent-transactions-horizontal-scrollbar app-horizontal-scroll-proxy',
  innerClassName: 'recent-transactions-horizontal-scrollbar-inner app-horizontal-scroll-proxy-inner',
  contentWidthProperty: '--app-horizontal-scroll-content-width',
  targetViewportProperty: '--app-horizontal-scroll-viewport-width',
  resetOnTargetWidthChange: true,
  clampTolerance: 0,
  resolveTarget: (controller) => controller.closest('[data-recent-transactions-horizontal-scroll-sync]'),
  getContentElements: ({ target }) => [
    ...target.querySelectorAll('.recent-transactions-scroll-surface'),
    target.querySelector('.recent-transactions-head'),
    target.querySelector('.recent-transactions-rows'),
  ],
  getObservedElements: ({ target, contentElements }) => [target, ...contentElements],
};

function RecentTransactionsHorizontalScrollProxy() {
  return <HorizontalScrollProxy options={HORIZONTAL_SCROLL_PROXY_OPTIONS} />;
}

function getAmountTone(transaction) {
  const amount = Number(transaction.amount);
  if (transaction.category?.classification === 'transfer') return 'transfer';
  if (Number.isFinite(amount) && amount > 0) return 'positive';
  if (Number.isFinite(amount) && amount < 0) return 'negative';
  return 'neutral';
}

export default function RecentTransactionsTable({
  transactions = [],
  status = 'idle',
  balancesHidden = false,
  showCategory = true,
  ariaLabel = 'Most recent transactions',
  emptyLabel = 'No transactions yet.',
  loadingLabel = 'Loading recent transactions...',
  errorLabel = 'Recent transactions are unavailable.',
  activeTransactionId = null,
  rowsRef = null,
  onTransactionClick = null,
}) {
  const isLoading = status === 'loading';
  const isError = status === 'error';
  const hasTransactions = transactions.length > 0;
  const clickable = typeof onTransactionClick === 'function';

  return (
    <div
      className={`recent-transactions-table ${showCategory ? '' : 'is-category-omitted'}`.trim()}
      role="table"
      aria-label={ariaLabel}
      data-recent-transactions-horizontal-scroll-sync
    >
      <div className="recent-transactions-scroll-surface">
        <div className="recent-transactions-head" role="row">
          <span>Date</span>
          <span aria-label="Institution logo" />
          <span>Account</span>
          <span>Description</span>
          {showCategory ? <span>Category</span> : null}
          <span>Amount</span>
        </div>
        <RecentTransactionsHorizontalScrollProxy />
        {isLoading ? (
          <div className="recent-transactions-state" role="status">
            <MdReceiptLong size={22} aria-hidden="true" />
            <span>{loadingLabel}</span>
          </div>
        ) : isError ? (
          <div className="recent-transactions-state is-error">
            <MdReceiptLong size={22} aria-hidden="true" />
            <span>{errorLabel}</span>
          </div>
        ) : hasTransactions ? (
          <div className="recent-transactions-rows" ref={rowsRef}>
            {transactions.map((transaction) => {
              const title = getTransactionPrimaryDescription(transaction);
              const active = transaction.id != null
                && activeTransactionId != null
                && String(transaction.id) === String(activeTransactionId);
              const handleClick = clickable ? () => onTransactionClick(transaction) : undefined;
              return (
                <div
                  className={`recent-transactions-row ${clickable ? 'is-clickable' : ''} ${active ? 'is-active' : ''}`.trim()}
                  role="row"
                  key={transaction.id}
                  onClick={handleClick}
                  onKeyDown={clickable ? (event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      onTransactionClick(transaction);
                    }
                  } : undefined}
                  tabIndex={clickable ? 0 : undefined}
                  aria-selected={active || undefined}
                >
                  <span className="recent-transaction-date">{formatShortDateValue(transaction.date, '—')}</span>
                  <span className="recent-transaction-logo">
                    <InstitutionLogo name={transaction.institution_name} size={28} />
                  </span>
                  <span className="recent-transaction-account">{transaction.account_name || 'Account'}</span>
                  <span className="recent-transaction-main">
                    <span className="recent-transaction-copy">
                      <span className="recent-transaction-title">{title}</span>
                    </span>
                  </span>
                  {showCategory ? (
                    <span className="recent-transaction-category">
                      <CategoryPill
                        category={transaction.category}
                        source={transaction.category_source}
                        size="sm"
                      />
                    </span>
                  ) : null}
                  <span className={`recent-transaction-amount is-${getAmountTone(transaction)}`.trim()}>
                    <FitMoney
                      full={balancesHidden ? '******' : formatSignedDollarAmount(transaction.amount, transaction.currency)}
                      compact={balancesHidden ? '******' : formatSignedCompactMoney(transaction.amount, transaction.currency)}
                      className="recent-transaction-money"
                    />
                    {!balancesHidden && transaction.currency ? (
                      <span className="recent-transaction-currency">{transaction.currency}</span>
                    ) : null}
                  </span>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="recent-transactions-state">
            <MdReceiptLong size={22} aria-hidden="true" />
            <span>{emptyLabel}</span>
          </div>
        )}
      </div>
    </div>
  );
}
