import { formatMoney, formatMoneyNarrow } from './format';

describe('money formatting aliases', () => {
  it.each(['CAD', 'CZK', 'BTC'])('keeps narrow %s formatting aligned with formatMoney', (currency) => {
    expect(formatMoneyNarrow(-1234.5, currency)).toBe(formatMoney(-1234.5, currency));
  });
});
