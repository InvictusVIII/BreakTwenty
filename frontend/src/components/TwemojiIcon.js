import React, { useState } from 'react';

// Centralised inventory of bundled emoji + icon sets. The picker renders each
// set as its own tab and category storage records which set was picked per
// icon. The three "emoji" sets are Unicode-codepoint-indexed; the "finance"
// set is a curated icon pack indexed by slug (see frontend/src/constants/financeIcons.json
// and the private development workspace's finance-icon generator).
export const EMOJI_SETS = [
  { id: 'twemoji', label: 'Twemoji', kind: 'emoji' },
  { id: 'noto', label: 'Noto', kind: 'emoji' },
  { id: 'fluent', label: 'Fluent', kind: 'emoji' },
  { id: 'finance', label: 'Finance', kind: 'icon' },
];

export const DEFAULT_EMOJI_SET = 'twemoji';
const PUBLIC_BASE_URL = (import.meta.env.BASE_URL || '/').replace(/\/$/, '');

export function isIconSet(setId) {
  return setId === 'finance';
}

function emojiToCodepoint(emoji) {
  if (!emoji) return '';
  const cps = [];
  for (const ch of emoji) {
    const cp = ch.codePointAt(0);
    if (cp === 0xfe0f) continue;
    cps.push(cp.toString(16));
  }
  return cps.join('-');
}

/**
 * Renders an emoji or finance icon using one of the bundled SVG sets.
 *
 * If the requested set doesn't have the codepoint, the `<img>` tag errors and
 * we fall back to the OS-native emoji font so the user still sees something.
 *
 */
function TwemojiIcon({ emoji, set = DEFAULT_EMOJI_SET, size = 18, className = '', alt = '', title }) {
  const [failedSrc, setFailedSrc] = useState('');
  if (!emoji) return null;

  const resolvedSet = set || DEFAULT_EMOJI_SET;
  const iconSet = isIconSet(resolvedSet);
  // For the finance icon set, `emoji` is the slug (e.g. "coin-stack").
  // For emoji sets, `emoji` is the rendered character; we convert to codepoint.
  const filename = iconSet ? emoji : emojiToCodepoint(emoji);
  const imageSrc = filename ? `${PUBLIC_BASE_URL}/emoji/${resolvedSet}/${filename}.svg` : '';
  const failed = failedSrc === imageSrc;

  if (failed) {
    return (
      <span
        className={`twemoji-fallback ${className}`.trim()}
        style={{ fontSize: `${size}px`, lineHeight: 1 }}
        title={title || alt}
        aria-label={alt || undefined}
      >
        {iconSet ? '◌' : emoji}
      </span>
    );
  }

  if (!filename) return null;

  // Per-set visual scale: Noto's 128px viewBox leaves more padding around the
  // glyph than Twemoji's 36px viewBox, so without compensation Noto looks
  // ~15% smaller at the same rendered size. Scale-up here normalises across sets.
  const SET_SCALE = {
    twemoji: 1,
    noto: 1.18,
    fluent: 1.04,
    finance: 1,
  };
  const scale = SET_SCALE[resolvedSet] ?? 1;

  return (
    <img
      src={imageSrc}
      alt={alt}
      width={size}
      height={size}
      className={`twemoji ${className}`.trim()}
      style={{
        display: 'inline-block',
        verticalAlign: '-0.125em',
        transform: scale !== 1 ? `scale(${scale})` : undefined,
        transformOrigin: 'center',
      }}
      onError={() => setFailedSrc(imageSrc)}
      onLoad={() => setFailedSrc('')}
      title={title || undefined}
      draggable={false}
    />
  );
}

export default TwemojiIcon;
