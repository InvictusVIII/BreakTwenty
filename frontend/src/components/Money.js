import React from 'react';
import FitMoney from './FitMoney';
import {
  formatMoneyNarrow,
  formatCompactMoney,
  formatSignedMoneyNarrow,
  formatSignedCompactMoney,
} from '../utils/format';

// One money figure that never clips. Shows the exact value, but if it would overflow
// its box it compacts to "Kč 1.9P"/"$4.2M" and keeps the full number on hover — the
// same measure-and-fit behavior as the dashboard net-worth (see FitMoney).
//
// IMPORTANT: the box tracks the AVAILABLE width, so the parent must give it a bounded
// slot — width:100% of a width-constrained container, a grid `minmax(0,1fr)` column,
// or a flex child with `flex:1; min-width:0`. A content-sized parent can't reopen
// space, so it would compact and never expand back.
function Money({ amount, currency = 'CAD', signed = false, hidden = false, className = '' }) {
  const num = Number(amount);
  const safe = Number.isFinite(num) ? num : 0;
  // When balances are hidden, mask the figure (matches the app-wide `******` convention)
  // while keeping the same box/className so layout is unchanged.
  const full = hidden ? '******' : (signed ? formatSignedMoneyNarrow(safe, currency) : formatMoneyNarrow(safe, currency));
  const compact = hidden ? '******' : (signed ? formatSignedCompactMoney(safe, currency) : formatCompactMoney(safe, currency));
  return <FitMoney className={className} full={full} compact={compact} />;
}

export default Money;
