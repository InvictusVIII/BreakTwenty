import React, { useEffect, useMemo, useState } from 'react';
import TwemojiIcon, { EMOJI_SETS, DEFAULT_EMOJI_SET, isIconSet } from './TwemojiIcon';
import AnchoredPopover from './AnchoredPopover';
import financeIcons from '../constants/financeIcons.json';
import './EmojiPickerPopover.css';

const PICKER_WIDTH = 360;
const PICKER_HEIGHT = 460;

// No per-category cap now that we render only one set at a time — even the
// biggest category (Smileys & People, ~447 emoji) is ~3× lighter than the old
// multi-set grid was rendering for the smallest. Search still has a soft cap
// so an empty/very-short query can't dump every emoji into the DOM.
const MAX_SEARCH_RESULTS = 250;

const CATEGORY_LABELS = {
  people: 'People',
  nature: 'Nature',
  foods: 'Food',
  activity: 'Activity',
  places: 'Places',
  objects: 'Objects',
  symbols: 'Symbols',
  flags: 'Flags',
  frequent: 'Recent',
};

function defaultEmojiDataLoader() {
  return import('@emoji-mart/data').then((mod) => mod.default || mod);
}

function EmojiPickerPopover({
  isOpen,
  onClose,
  onSelect,
  anchorRect,
  emojiDataLoader = defaultEmojiDataLoader,
}) {
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [activeSet, setActiveSet] = useState(DEFAULT_EMOJI_SET);
  const [activeCategory, setActiveCategory] = useState('people');

  // Debounce so each keystroke doesn't re-filter 1870 emoji + re-render the grid.
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedQuery(query), 180);
    return () => clearTimeout(handle);
  }, [query]);

  // Lazy-load the ~600KB emoji dataset on first open.
  useEffect(() => {
    if (!isOpen || data || loadError) return;
    let cancelled = false;
    emojiDataLoader()
      .then((loadedData) => {
        if (!cancelled) setData(loadedData);
      })
      .catch((err) => {
        if (cancelled) return;
        console.warn('EmojiPickerPopover: failed to load emoji data', err);
        setLoadError(err);
      });
    return () => { cancelled = true; };
  }, [isOpen, data, loadError, emojiDataLoader, loadAttempt]);

  useEffect(() => {
    if (!isOpen || !loadError || data) return undefined;
    const retryWhenOnline = () => {
      setLoadError(null);
      setLoadAttempt((attempt) => attempt + 1);
    };
    window.addEventListener('online', retryWhenOnline);
    return () => window.removeEventListener('online', retryWhenOnline);
  }, [isOpen, loadError, data]);

  const isFinanceSet = isIconSet(activeSet);

  const visibleEmojis = useMemo(() => {
    // Finance set: synchronously available from the bundled manifest.
    if (isFinanceSet) {
      const q = debouncedQuery.trim().toLowerCase();
      const all = financeIcons.icons || [];
      const items = q
        ? all.filter((icon) => (
            icon.name?.toLowerCase().includes(q) ||
            icon.slug?.toLowerCase().includes(q) ||
            (icon.keywords || []).some((k) => k.toLowerCase().includes(q))
          ))
        : all;
      return items.map((icon) => ({
        id: icon.slug,
        name: icon.name,
        keywords: icon.keywords,
        _financeSlug: icon.slug,
      }));
    }
    if (!data) return [];
    const q = debouncedQuery.trim().toLowerCase();
    if (q) {
      const matched = [];
      for (const id in data.emojis) {
        if (matched.length >= MAX_SEARCH_RESULTS) break;
        const e = data.emojis[id];
        if (!e) continue;
        const nameMatches = e.name?.toLowerCase().includes(q);
        const keywordMatches = e.keywords?.some((k) => k.toLowerCase().includes(q));
        if (nameMatches || keywordMatches) matched.push(e);
      }
      return matched;
    }
    const cat = data.categories.find((c) => c.id === activeCategory);
    return (cat?.emojis || [])
      .map((id) => data.emojis[id])
      .filter(Boolean);
  }, [data, debouncedQuery, activeCategory, isFinanceSet]);

  const isSearchActive = Boolean(debouncedQuery.trim());
  const retryEmojiDataLoad = () => {
    setLoadError(null);
    setLoadAttempt((attempt) => attempt + 1);
  };

  return (
    <AnchoredPopover
      isOpen={isOpen}
      anchorRect={anchorRect}
      width={PICKER_WIDTH}
      height={PICKER_HEIGHT}
      align="left"
      onDismiss={onClose}
      className="emoji-picker-popover"
      ariaLabel="Pick emoji"
    >
      <div className="emoji-picker-search">
        <input
          autoFocus
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search emoji…"
        />
      </div>

      <div className="emoji-picker-set-strip" role="tablist" aria-label="Emoji style">
        {EMOJI_SETS.map((s) => (
          <button
            key={s.id}
            type="button"
            role="tab"
            aria-selected={activeSet === s.id}
            className={`emoji-picker-set-tab app-control-root ${activeSet === s.id ? 'is-active' : ''}`.trim()}
            onClick={() => setActiveSet(s.id)}
          >
            <span className="app-control-label">{s.label}</span>
          </button>
        ))}
      </div>

      {!isSearchActive && !isFinanceSet && data && (
        <div className="emoji-picker-cat-strip" role="tablist" aria-label="Emoji category">
          {data.categories.map((c) => (
            <button
              key={c.id}
              type="button"
              role="tab"
              aria-selected={activeCategory === c.id}
              className={`emoji-picker-cat-tab app-control-root ${activeCategory === c.id ? 'is-active' : ''}`.trim()}
              onClick={() => setActiveCategory(c.id)}
              title={CATEGORY_LABELS[c.id] || c.id}
            >
              <span className="app-control-label">{CATEGORY_LABELS[c.id] || c.id}</span>
            </button>
          ))}
        </div>
      )}

      <div className="emoji-picker-list">
        {!isFinanceSet && loadError ? (
          <div className="emoji-picker-empty emoji-picker-load-error" role="status">
            <span>Emoji data could not load.</span>
            <button
              type="button"
              className="emoji-picker-retry app-control-root"
              onClick={retryEmojiDataLoad}
            >
              <span className="app-control-label">Retry</span>
            </button>
          </div>
        ) : !isFinanceSet && !data ? (
          <div className="emoji-picker-empty">Loading emoji…</div>
        ) : visibleEmojis.length === 0 ? (
          <div className="emoji-picker-empty">No matches</div>
        ) : (
          <div className="emoji-picker-grid">
            {visibleEmojis.map((e) => {
              // For finance icons the lookup key is the slug; for emoji sets it's the native char.
              const value = e._financeSlug || e.skins?.[0]?.native;
              if (!value) return null;
              return (
                <button
                  key={e.id}
                  type="button"
                  className="emoji-picker-cell"
                  onClick={() => { onSelect(value, activeSet); onClose(); }}
                  title={e.name}
                  aria-label={e.name}
                >
                  <TwemojiIcon emoji={value} set={activeSet} size={24} />
                </button>
              );
            })}
          </div>
        )}
      </div>
    </AnchoredPopover>
  );
}

export default EmojiPickerPopover;
