import { formatQuantity } from './format';

const TRANSACTION_TYPES_WITH_QUANTITY = new Set([
  'buy',
  'sell',
  'dividend',
  'distribution',
]);

export function isCurrencyTransactionSymbol(symbol) {
  return /^[A-Z]{3}$/.test(String(symbol || '').trim());
}

function titleCaseTransactionType(type) {
  return String(type || '')
    .split('_')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

export function getTransactionPrimaryDescription(transaction, fallback = 'Transaction') {
  const symbol = String(transaction?.symbol || '').trim();
  const userDescription = String(transaction?.user_description || '').trim();
  const description = String(transaction?.description || '').trim();

  if (userDescription) return userDescription;

  if (symbol && isCurrencyTransactionSymbol(symbol) && description) {
    const issuedBy = description.match(/issued by\s+(.+)/i);
    return issuedBy ? issuedBy[1].trim() : description;
  }

  if (symbol && !isCurrencyTransactionSymbol(symbol)) return symbol;
  return description || titleCaseTransactionType(transaction?.type) || fallback;
}

export function getTransactionQuantityLabel(transaction) {
  const type = String(transaction?.type || '').trim().toLowerCase();
  const quantity = transaction?.quantity;
  if (
    !TRANSACTION_TYPES_WITH_QUANTITY.has(type)
    || quantity == null
    || quantity === ''
    || !Number.isFinite(Number(quantity))
  ) {
    return '';
  }
  return `Qty: ${formatQuantity(quantity)}`;
}

export function getTransactionAccountTooltip(transaction) {
  const institution = String(transaction?.institution_name || '').trim();
  const account = String(transaction?.account_name || '').trim();
  return [institution, account].filter(Boolean).join(' — ');
}
