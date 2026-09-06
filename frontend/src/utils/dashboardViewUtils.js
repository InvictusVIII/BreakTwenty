import { getAppNow } from './appClock';
import {
  PORTFOLIO_CUSTOM_TIMEFRAME_KEY,
  PORTFOLIO_TIMEFRAMES,
} from './portfolioViewUtils';

function formatMarketStripChange(value, fractionDigits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const digits = Math.max(0, Math.min(6, Number(fractionDigits) || 2));
  const numericValue = Number(value);
  const sign = numericValue > 0 ? '+' : numericValue < 0 ? '-' : '';
  return `${sign}${Math.abs(numericValue).toFixed(digits)}`;
}

function formatMarketStripAsOf(value) {
  const raw = String(value || '').slice(0, 10);
  const [year, month, day] = raw.split('-').map((part) => Number(part));
  if (!year || !month || !day) return raw;
  return new Intl.DateTimeFormat('en-CA', {
    month: 'short',
    day: 'numeric',
  }).format(new Date(year, month - 1, day));
}

export function getMarketStripTilePresentation(tile, isKeylessMarketData) {
  const change = Number(tile.change);
  const changePct = Number(tile.change_pct);
  const precision = Number.isFinite(Number(tile.precision)) ? Number(tile.precision) : 2;
  const formattedAsOf = tile.as_of ? formatMarketStripAsOf(tile.as_of) : '';
  const asOfCopy = isKeylessMarketData && tile.source === 'fred' && formattedAsOf
    ? `EOD ${formattedAsOf}`
    : '';
  const tone = Number.isFinite(change) && change > 0
    ? 'positive'
    : Number.isFinite(change) && change < 0
      ? 'negative'
      : 'neutral';
  const changeCopy = Number.isFinite(change) && Number.isFinite(changePct)
    ? `${formatMarketStripChange(change, precision)} ${formatMarketStripChange(changePct)}%`
    : '—';
  return { asOfCopy, changeCopy, precision, tone };
}

export function getDashboardCashFlowDisplayCurrency(payload, fallbackCurrency = 'CAD') {
  const responseCurrency = String(payload?.period?.currency || '').trim().toUpperCase();
  return responseCurrency || String(fallbackCurrency || 'CAD').trim().toUpperCase() || 'CAD';
}

export function filterDashboardNetWorthHistory(history, timeframe, customDateRange, now = getAppNow()) {
  if (history.length === 0) return [];

  if (timeframe === PORTFOLIO_CUSTOM_TIMEFRAME_KEY) {
    const startMs = customDateRange?.start ? new Date(customDateRange.start).getTime() : null;
    const endMs = customDateRange?.end ? new Date(customDateRange.end).getTime() : null;
    const points = history.filter((point) => {
      const pointMs = new Date(point.date).getTime();
      return (startMs === null || pointMs >= startMs) && (endMs === null || pointMs <= endMs);
    });

    // Clamp the left edge to the selected start and carry the last known value forward.
    if (startMs !== null && (points.length === 0 || new Date(points[0].date).getTime() > startMs)) {
      let priorPoint = null;
      for (let index = history.length - 1; index >= 0; index -= 1) {
        if (new Date(history[index].date).getTime() <= startMs) {
          priorPoint = history[index];
          break;
        }
      }
      if (priorPoint) points.unshift({ ...priorPoint, date: customDateRange.start });
    }
    // Clamp the right edge to the selected end and carry the last known value forward.
    if (endMs !== null && points.length > 0 && new Date(points[points.length - 1].date).getTime() < endMs) {
      points.push({ ...points[points.length - 1], date: customDateRange.end });
    }
    return points;
  }

  const preset = PORTFOLIO_TIMEFRAMES.find((item) => item.label === timeframe);
  if (!preset || preset.days === null) return history;

  let cutoff;
  if (preset.days === 'ytd') {
    cutoff = new Date(now.getFullYear(), 0, 1);
  } else if (preset.days === 1) {
    cutoff = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  } else {
    cutoff = new Date(now.getTime() - preset.days * 24 * 60 * 60 * 1000);
  }
  const cutoffKey = `${cutoff.getFullYear()}-${String(cutoff.getMonth() + 1).padStart(2, '0')}-${String(cutoff.getDate()).padStart(2, '0')}`;
  const filtered = history.filter((point) => String(point.date).slice(0, 10) >= cutoffKey);
  if (filtered.length === 0) return [history[history.length - 1]];
  if (String(filtered[0].date).slice(0, 10) > cutoffKey) {
    for (let index = history.length - 1; index >= 0; index -= 1) {
      if (String(history[index].date).slice(0, 10) <= cutoffKey) {
        filtered.unshift({ ...history[index], date: cutoffKey });
        break;
      }
    }
  }
  return filtered;
}
