import React, { useLayoutEffect, useRef, useState } from 'react';

// Amount input with live thousands-separators, so a big number reads as
// 44,654,686,748,465 instead of an unreadable run of digits. Native number inputs
// reject commas, so this is a text field; the RAW numeric string (no commas) is what
// flows back through onChange(raw), so callers keep doing Number(value) exactly as before.
// Positive magnitudes only (the sign is owned by category/liability elsewhere).

// Keep only digits and a single decimal point.
function toRaw(text) {
  let raw = String(text ?? '').replace(/[^\d.]/g, '');
  const dot = raw.indexOf('.');
  if (dot !== -1) raw = raw.slice(0, dot + 1) + raw.slice(dot + 1).replace(/\./g, '');
  return raw;
}

// Group the integer part in threes. String-based (no Number()) so precision survives
// however many digits the user types.
function formatWithCommas(value) {
  const raw = toRaw(value);
  if (raw === '') return '';
  const parts = raw.split('.');
  const intPart = parts[0].replace(/^0+(?=\d)/, '') || (parts.length > 1 ? '0' : '');
  const intFmt = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return parts.length > 1 ? `${intFmt || '0'}.${parts[1]}` : intFmt;
}

// Count of digits/point chars before the caret — the position that must survive reformat.
function significantBefore(text, caret) {
  return (String(text).slice(0, caret).match(/[\d.]/g) || []).length;
}
function caretForSignificant(formatted, count) {
  if (count <= 0) return 0;
  let seen = 0;
  for (let i = 0; i < formatted.length; i += 1) {
    if (/[\d.]/.test(formatted[i])) {
      seen += 1;
      if (seen >= count) return i + 1;
    }
  }
  return formatted.length;
}

export default function MoneyInput({ value, onChange, ...rest }) {
  const ref = useRef(null);
  const pendingCaret = useRef(null);
  const externalRaw = toRaw(value);
  const [displayState, setDisplayState] = useState(() => ({
    raw: externalRaw,
    display: formatWithCommas(value),
  }));
  const display = displayState.raw === externalRaw ? displayState.display : formatWithCommas(value);

  // Restore the caret to the same digit position after commas shift the string.
  useLayoutEffect(() => {
    if (pendingCaret.current != null && ref.current) {
      const pos = caretForSignificant(display, pendingCaret.current);
      try { ref.current.setSelectionRange(pos, pos); } catch { /* ignore non-text selection */ }
      pendingCaret.current = null;
    }
  }, [display]);

  const handleChange = (e) => {
    const typed = e.target.value;
    const caret = e.target.selectionStart == null ? typed.length : e.target.selectionStart;
    const raw = toRaw(typed);
    pendingCaret.current = significantBefore(typed, caret);
    setDisplayState({ raw, display: formatWithCommas(raw) }); // local re-render always fires (drops invalid chars, reformats)
    onChange(raw);
  };

  return (
    <input
      ref={ref}
      {...rest}
      type="text"
      inputMode="decimal"
      value={display}
      onChange={handleChange}
    />
  );
}
