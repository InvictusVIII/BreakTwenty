import React, { useRef, useState, useLayoutEffect } from 'react';
import './FitMoney.css';

// Shared overflow primitive (consumed by FitMoney AND by value+change clusters like the dashboard
// metric boxes, so the "does it fit?" decision is one implementation everywhere). A hidden,
// always-full measurer's natural width is compared against the box's available width, re-checked
// via ResizeObserver so it re-evaluates BOTH ways (compact <-> full) as panel/zoom/resolution
// change. IMPORTANT: the box must track AVAILABLE width (a fixed width, or width:100% of a
// width-constrained parent) — a content-sized box can compact but then can't tell space reopened,
// so it never expands back. `deps` are the content inputs that change the natural width (the
// formatted strings), so a value change re-measures.
function useFitCompact(deps) {
  const boxRef = useRef(null);
  const measureRef = useRef(null);
  const [overflow, setOverflow] = useState(false);
  useLayoutEffect(() => {
    const box = boxRef.current;
    const measure = measureRef.current;
    if (!box || !measure) return undefined;
    const check = () => setOverflow(measure.scrollWidth > box.clientWidth + 0.5);
    check();
    const observer = new ResizeObserver(check);
    observer.observe(box);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { boxRef, measureRef, overflow };
}

// Shows `full`; if it overflows its box — too many digits, OR a narrower box from zoom / resolution
// / a smaller panel — it swaps to `compact` and surfaces `full` as a hover tooltip, so precision is
// never lost. Measurement contract: see useFitCompact.
function FitMoney({ full, compact, className = '', suppressTooltip = false }) {
  const { boxRef, measureRef, overflow } = useFitCompact([full]);
  const showCompact = overflow && Boolean(compact);
  return (
    <span ref={boxRef} className={`fit-money ${className}`.trim()}>
      <span ref={measureRef} className="fit-money-measure" aria-hidden="true">{full}</span>
      <span className="fit-money-value" data-tooltip={(showCompact && !suppressTooltip) ? full : undefined}>
        {showCompact ? compact : full}
      </span>
    </span>
  );
}

// Reusable value + optional "change" (amount + %) renderer with INDEPENDENT compaction and
// SEPARATE per-element tooltips: the value and the change each fold to their own compact form on
// their own, and each surfaces ONLY its own full form on hover, ONLY when it actually shrank (so
// the value tooltip is just the value, the change tooltip is the change + its %). Measurement:
// three hidden clones (both-full, value-compact, change-compact) are compared against the box's
// available width, preferring to keep the headline value full and fold the change first. The box
// must be an available-width, position:relative, overflow:hidden slot (e.g. Dashboard.css
// .overview-metric-value-box). `classes` supplies the styling hooks (box / line / value);
// `change.className` styles the change cluster; `change.suffix` is the always-shown JSX (% + label).
const FIT_MEASURE_STYLE = { position: 'absolute', top: 0, left: 0, visibility: 'hidden', pointerEvents: 'none' };

export function FitMetricValue({ valueFull, valueCompact, change = null, classes = {}, suppressTooltip = false }) {
  const boxRef = useRef(null);
  const aRef = useRef(null);
  const bRef = useRef(null);
  const cRef = useRef(null);
  const [state, setState] = useState({ value: false, change: false });

  useLayoutEffect(() => {
    const box = boxRef.current;
    if (!box) return undefined;
    const measure = () => {
      const avail = box.clientWidth + 0.5;
      const wa = aRef.current ? aRef.current.scrollWidth : 0;
      if (!change) { setState({ value: wa > avail, change: false }); return; }
      const wb = bRef.current ? bRef.current.scrollWidth : 0;
      const wc = cRef.current ? cRef.current.scrollWidth : 0;
      if (wa <= avail) setState({ value: false, change: false });
      else if (wc <= avail) setState({ value: false, change: true });   // keep headline value full, fold the change
      else if (wb <= avail) setState({ value: true, change: false });
      else setState({ value: true, change: true });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [valueFull, valueCompact, change]);

  const lineClass = classes.line || '';
  const valueClass = classes.value || '';
  const cluster = (vc, cc, live) => (
    <>
      <span className={valueClass} data-tooltip={live && state.value && !suppressTooltip ? valueFull : undefined}>
        {vc ? valueCompact : valueFull}
      </span>
      {change ? (
        <span className={change.className} data-tooltip={live && state.change && !suppressTooltip ? change.tooltip : undefined}>
          <span>{cc ? change.amountCompact : change.amountFull}</span>
          {change.pctFull ? <span className="fit-metric-pct">{cc ? change.pctCompact : change.pctFull}</span> : null}
          {change.suffix}
        </span>
      ) : null}
    </>
  );

  return (
    <span ref={boxRef} className={classes.box || ''}>
      <span ref={aRef} className={lineClass} style={FIT_MEASURE_STYLE} aria-hidden="true">{cluster(false, false, false)}</span>
      {change ? <span ref={bRef} className={lineClass} style={FIT_MEASURE_STYLE} aria-hidden="true">{cluster(true, false, false)}</span> : null}
      {change ? <span ref={cRef} className={lineClass} style={FIT_MEASURE_STYLE} aria-hidden="true">{cluster(false, true, false)}</span> : null}
      <span className={lineClass}>{cluster(state.value, state.change, true)}</span>
    </span>
  );
}

export default FitMoney;
