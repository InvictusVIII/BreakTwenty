import { getAccountTypeBadgeClass } from './accountType';

describe('getAccountTypeBadgeClass', () => {
  it.each([
    ['keeps an underscored credit-card type', 'credit_card', 'credit_card'],
    ['normalizes a spaced credit-card type', 'credit card', 'credit_card'],
    ['normalizes a spaced line-of-credit type', 'line of credit', 'line_of_credit'],
    ['falls back to other for an empty type', '', 'other'],
  ])('%s', (_name, accountType, expected) => {
    expect(getAccountTypeBadgeClass(accountType)).toBe(expected);
  });
});
