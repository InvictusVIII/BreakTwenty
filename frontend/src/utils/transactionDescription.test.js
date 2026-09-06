import { describe, expect, it } from 'vitest';

import {
  getTransactionAccountTooltip,
  getTransactionPrimaryDescription,
  getTransactionQuantityLabel,
  isCurrencyTransactionSymbol,
} from './transactionDescription';

describe('transaction primary descriptions', () => {
  it('uses the brokerage symbol instead of a verbose dividend description', () => {
    expect(getTransactionPrimaryDescription({
      symbol: 'INTY',
      description: 'INTY(CA30053U3047) CASH DIVIDEND CAD 0.26 PER SHARE',
      type: 'dividend',
    })).toBe('INTY');
  });

  it('keeps the Transactions-page precedence for custom and ordinary descriptions', () => {
    expect(getTransactionPrimaryDescription({
      symbol: 'INTY',
      user_description: 'Income fund payout',
      description: 'INTY CASH DIVIDEND',
      type: 'dividend',
    })).toBe('Income fund payout');
    expect(getTransactionPrimaryDescription({
      description: 'Monthly account fee',
      type: 'fee',
    })).toBe('Monthly account fee');
    expect(getTransactionPrimaryDescription({ type: 'interest_paid' })).toBe('Interest Paid');
  });

  it('preserves the currency-symbol special case', () => {
    expect(isCurrencyTransactionSymbol('USD')).toBe(true);
    expect(isCurrencyTransactionSymbol('INTY')).toBe(false);
    expect(getTransactionPrimaryDescription({
      symbol: 'USD',
      description: 'US Dollar issued by Wise',
    })).toBe('Wise');
  });

  it('formats quantity labels only for dividend, distribution, buy, and sell rows', () => {
    expect(getTransactionQuantityLabel({ type: 'dividend', quantity: 100 })).toBe('Qty: 100');
    expect(getTransactionQuantityLabel({ type: 'distribution', quantity: 22.125 })).toBe('Qty: 22.125');
    expect(getTransactionQuantityLabel({ type: 'buy', quantity: '1200.5' })).toBe('Qty: 1,200.5');
    expect(getTransactionQuantityLabel({ type: 'sell', quantity: 0 })).toBe('Qty: 0');
    expect(getTransactionQuantityLabel({ type: 'fee', quantity: 5 })).toBe('');
    expect(getTransactionQuantityLabel({ type: 'buy', quantity: null })).toBe('');
  });

  it('combines institution and account names for source tooltips', () => {
    expect(getTransactionAccountTooltip({
      institution_name: 'Questrade',
      account_name: 'TFSA',
    })).toBe('Questrade — TFSA');
    expect(getTransactionAccountTooltip({ institution_name: 'Wise' })).toBe('Wise');
    expect(getTransactionAccountTooltip({ account_name: 'Cash account' })).toBe('Cash account');
  });
});
