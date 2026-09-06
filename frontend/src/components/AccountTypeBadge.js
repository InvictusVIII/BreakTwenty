import React from 'react';
import { ACCOUNT_TYPE_LABELS } from '../constants/providers';
import { getAccountTypeBadgeClass } from '../utils/accountType';

function joinClassNames(...values) {
  return values.filter(Boolean).join(' ');
}

function getAccountTypeBadgeLabel(accountType, label = '') {
  return String(label || ACCOUNT_TYPE_LABELS[accountType] || accountType || '').trim();
}

export default function AccountTypeBadge({ accountType, label = '', className = '' }) {
  const badgeLabel = getAccountTypeBadgeLabel(accountType, label);
  if (!badgeLabel) return null;

  return (
    <span className={joinClassNames('account-type', className, `is-${getAccountTypeBadgeClass(accountType)}`)}>
      <span className="account-type-label">{badgeLabel}</span>
    </span>
  );
}
