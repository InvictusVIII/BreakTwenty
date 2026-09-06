/**
 * Shared helpers used by Holdings page tooltips and the Holdings page itself.
 * Extracted from pages/Holdings.js during Stage 4 of the cleanup to give the
 * extracted tooltip components a stable import target without circular deps.
 *
 * Keep this file dependency-light — only types/utilities the tooltips and the
 * Holdings page both need. Page-only logic stays in pages/Holdings.js.
 */
import {
  formatMoneyNarrowOrDash as formatMoneyNarrowRaw,
  formatMoneyOrDash as formatMoneyRaw,
  formatQuantity,
  formatSignedMoneyNarrow as formatSignedMoneyNarrowRaw,
  formatTableMoney as formatTableMoneyRaw,
} from '../../utils/format';
import { getBalancesHidden } from '../../hooks/useBalancesHidden';

export const MASKED_BALANCE_TEXT = '******';

export const HOLDINGS_CHART_TOOLTIP_STYLE = {
  background: 'var(--tooltip-bg)',
  border: '1px solid var(--tooltip-border-color)',
  borderRadius: 'var(--tooltip-radius)',
  boxShadow: 'var(--tooltip-shadow)',
};

export function formatMoney(value, currency) {
  return getBalancesHidden() ? MASKED_BALANCE_TEXT : formatMoneyRaw(value, currency);
}

export function formatMoneyNarrow(value, currency) {
  return getBalancesHidden() ? MASKED_BALANCE_TEXT : formatMoneyNarrowRaw(value, currency);
}

export function formatSignedMoneyNarrow(value, currency) {
  return getBalancesHidden() ? MASKED_BALANCE_TEXT : formatSignedMoneyNarrowRaw(value, currency);
}

export function formatTableMoney(value, currency) {
  return getBalancesHidden() ? MASKED_BALANCE_TEXT : formatTableMoneyRaw(value, currency);
}

export function formatShareCount(quantity) {
  const numericQuantity = Number(quantity);
  const suffix = Math.abs(numericQuantity) === 1 ? 'share' : 'shares';
  return `${formatQuantity(quantity)} ${suffix}`;
}
