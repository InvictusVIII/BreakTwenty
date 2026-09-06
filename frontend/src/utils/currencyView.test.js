import {
  buildFxHistoryIndex,
  cadPerUnitAtDate,
  makeHistoricalCurrencyConverter,
} from './currencyView';

describe('historical currency conversion', () => {
  const fxHistoryIndex = buildFxHistoryIndex({
    USD: {
      '2024-01-01': 1.25,
      '2024-02-01': 1.5,
    },
    EUR: {
      '2024-01-01': 1.4,
    },
  });

  const currentRates = {
    CAD: 1,
    USD: 0.8,
    EUR: 0.7,
  };

  it('uses nearest-prior CAD-per-unit rates for historical dates', () => {
    expect(cadPerUnitAtDate(fxHistoryIndex, 'USD', '2024-01-15', currentRates)).toBe(1.25);
    expect(cadPerUnitAtDate(fxHistoryIndex, 'USD', '2024-02-15', currentRates)).toBe(1.5);
  });

  it('converts dated balances through historical CAD anchors', () => {
    const convertAtDate = makeHistoricalCurrencyConverter('CAD', currentRates, fxHistoryIndex);

    expect(convertAtDate(100, 'USD', 'CAD', '2024-01-15')).toBe(125);
    expect(convertAtDate(100, 'USD', 'EUR', '2024-02-15')).toBeCloseTo(107.142857, 5);
  });

  it('falls back to current cross-rates when no date is supplied', () => {
    const convertAtDate = makeHistoricalCurrencyConverter('CAD', currentRates, fxHistoryIndex);

    expect(convertAtDate(100, 'USD', 'CAD')).toBe(125);
  });

  it('uses CAD anchors for current cross-rates when the primary currency is not CAD', () => {
    const usdPrimaryRates = { USD: 1, CAD: 1.25, EUR: 0.9 };
    const convertAtDate = makeHistoricalCurrencyConverter('USD', usdPrimaryRates, {});

    expect(cadPerUnitAtDate({}, 'USD', null, usdPrimaryRates)).toBe(1.25);
    expect(convertAtDate(100, 'USD', 'EUR')).toBeCloseTo(90, 8);
    expect(convertAtDate(90, 'EUR', 'CAD')).toBeCloseTo(125, 8);
  });
});
