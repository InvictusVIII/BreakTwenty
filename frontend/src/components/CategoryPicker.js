import React, { useCallback, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { MdAdd } from 'react-icons/md';
import TwemojiIcon from './TwemojiIcon';
import AnchoredPopover from './AnchoredPopover';
import EmojiPickerPopover from './EmojiPickerPopover';
import Dropdown from './Dropdown';
import { ChartColorRow } from './ChartColorControls';
import { API } from '../config';
import { useTheme } from '../appState';
import { getCategoryIdentityColor, getCategoryThemeVars } from '../utils/categoryColors';
import './CategoryPicker.css';

const POPOVER_WIDTH = 400;
const POPOVER_MAX_HEIGHT = 520;

const MANAGE_CATEGORIES_PATH = '/settings/categories';

function CategoryPicker({
  isOpen,
  onClose,
  currentCategoryId = null,
  onSelect,
  categories = [],
  anchorRect = null,
  onCategoriesChanged,
  placement = 'bottom',
  crossAlign = 'start',
  offsetY = 0,
}) {
  const [query, setQuery] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newParentId, setNewParentId] = useState('');
  const [newIcon, setNewIcon] = useState('');
  const [newIconSet, setNewIconSet] = useState(null);
  const [newColors, setNewColors] = useState({ dark: null, light: null });
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [emojiAnchorRect, setEmojiAnchorRect] = useState(null);
  const [groupMenuOpen, setGroupMenuOpen] = useState(false);
  const iconButtonRef = useRef(null);
  const navigate = useNavigate();
  const { mode } = useTheme();
  const newColor = newColors[mode];
  const setNewColor = useCallback((nextColor) => {
    setNewColors((previous) => ({ ...previous, [mode]: nextColor }));
  }, [mode]);

  const resetPickerState = useCallback(() => {
    setQuery('');
    setCreateOpen(false);
    setNewName('');
    setNewParentId('');
    setNewIcon('');
    setNewIconSet(null);
    setNewColors({ dark: null, light: null });
    setCreating(false);
    setCreateError('');
    setEmojiOpen(false);
    setGroupMenuOpen(false);
  }, []);

  const closePicker = useCallback(() => {
    resetPickerState();
    onClose();
  }, [onClose, resetPickerState]);

  // All top-level groups (including empty ones) are valid create targets, so
  // this is computed separately from `grouped` (which hides empty groups).
  const parentGroups = useMemo(
    () => categories
      .filter((c) => c.parent_id === null)
      .sort((a, b) => a.sort_order - b.sort_order),
    [categories],
  );

  const selectedParent = useMemo(
    () => parentGroups.find((p) => String(p.id) === String(newParentId)) || null,
    [parentGroups, newParentId],
  );

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase();
    return parentGroups
      .map((parent) => {
        const allChildren = categories
          .filter((c) => c.parent_id === parent.id)
          .sort((a, b) => a.sort_order - b.sort_order);
        const matched = q
          ? allChildren.filter(
              (c) =>
                c.name.toLowerCase().includes(q) ||
                parent.name.toLowerCase().includes(q)
            )
          : allChildren;
        return { parent, children: matched };
      })
      .filter((group) => group.children.length > 0);
  }, [categories, parentGroups, query]);

  // Defaults follow the chosen group: a new leaf inherits both group colors and
  // icon (and classification on submit) unless the user overrides them.
  const seedDefaultsFromParent = useCallback((parent) => {
    setNewIcon(parent?.icon || '');
    setNewIconSet(parent?.icon_set || null);
    setNewColors({
      dark: parent?.color_dark || null,
      light: parent?.color_light || null,
    });
  }, []);

  const openCreate = useCallback(() => {
    let defaultParent = parentGroups[0] || null;
    if (currentCategoryId != null) {
      const current = categories.find((c) => c.id === currentCategoryId);
      const currentParent = current?.parent_id != null
        ? parentGroups.find((g) => g.id === current.parent_id)
        : null;
      if (currentParent) defaultParent = currentParent;
    }
    setNewParentId(defaultParent ? String(defaultParent.id) : '');
    setNewName('');
    seedDefaultsFromParent(defaultParent);
    setCreateError('');
    setCreateOpen(true);
  }, [parentGroups, categories, currentCategoryId, seedDefaultsFromParent]);

  const handleChangeParent = useCallback((value) => {
    setNewParentId(value);
    seedDefaultsFromParent(parentGroups.find((g) => String(g.id) === String(value)));
  }, [parentGroups, seedDefaultsFromParent]);

  const handleOpenEmoji = useCallback(() => {
    if (iconButtonRef.current) {
      const r = iconButtonRef.current.getBoundingClientRect();
      setEmojiAnchorRect({ top: r.top, left: r.left, bottom: r.bottom, right: r.right });
    }
    setEmojiOpen(true);
  }, []);

  const handleCreateSubmit = useCallback(async (event) => {
    if (event) event.preventDefault();
    const name = newName.trim();
    if (!name) {
      setCreateError('Name is required');
      return;
    }
    if (!newParentId || !selectedParent) {
      setCreateError('Pick a group');
      return;
    }
    setCreating(true);
    setCreateError('');
    try {
      const resp = await fetch(`${API}/categories`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          parent_id: Number(newParentId),
          icon: (newIcon && newIcon.trim()) || null,
          icon_set: newIconSet || null,
          color_dark: newColors.dark || selectedParent.color_dark || null,
          color_light: newColors.light || selectedParent.color_light || null,
          // A leaf's classification is always its group's — never chosen here.
          classification: selectedParent.classification,
        }),
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => null);
        throw new Error(detail?.detail || 'Failed to create category');
      }
      const created = await resp.json();
      if (onCategoriesChanged) await onCategoriesChanged();
      onSelect(created.id);
      closePicker();
    } catch (err) {
      setCreateError(err.message || 'Failed to create category');
      setCreating(false);
    }
  }, [newName, newParentId, selectedParent, newIcon, newIconSet, newColors, onCategoriesChanged, onSelect, closePicker]);

  const handleManage = useCallback(() => {
    closePicker();
    navigate(MANAGE_CATEGORIES_PATH);
  }, [closePicker, navigate]);

  return (
    <AnchoredPopover
      isOpen={isOpen}
      anchorRect={anchorRect}
      width={POPOVER_WIDTH}
      clampHeight={POPOVER_MAX_HEIGHT}
      placement={placement}
      align="right"
      crossAlign={crossAlign}
      offsetY={offsetY}
      onDismiss={closePicker}
      disableDismiss={emojiOpen || groupMenuOpen}
      className="category-picker-popover"
      ariaLabel="Choose category"
    >
      {createOpen ? (
        <form className="category-picker-create" onSubmit={handleCreateSubmit}>
          <div className="category-picker-create-title">New category</div>
          <div className="category-picker-create-name-row">
            <button
              type="button"
              ref={iconButtonRef}
              className="category-picker-create-icon app-control-root"
              onClick={handleOpenEmoji}
              data-tooltip="Pick an icon"
              aria-label="Pick an icon"
            >
              <span className="app-control-icon" aria-hidden="true">
                <TwemojiIcon
                  emoji={newIcon || selectedParent?.icon || '❓'}
                  set={newIconSet || selectedParent?.icon_set || undefined}
                  size={20}
                />
              </span>
            </button>
            <input
              type="text"
              className="category-picker-create-name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Category name"
              autoFocus
              maxLength={80}
            />
          </div>

          <div className="category-picker-create-field">
            <span className="category-picker-create-field-label">Group</span>
            <Dropdown
              value={newParentId}
              options={parentGroups.map((p) => ({ value: String(p.id), label: p.name }))}
              onChange={handleChangeParent}
              onOpenChange={setGroupMenuOpen}
              ariaLabel="Category group"
            />
          </div>

          <div className="category-picker-create-color">
            <ChartColorRow
              label="Color"
              color={newColor}
              defaultColor={getCategoryIdentityColor(selectedParent, mode) || newColor}
              onApply={(next) => setNewColor(next)}
              onReset={() => setNewColor(getCategoryIdentityColor(selectedParent, mode))}
            />
          </div>

          {createError && <div className="category-picker-create-error">{createError}</div>}

          <div className="category-picker-create-actions">
            <button
              type="button"
              className="category-picker-create-cancel app-control-root"
              onClick={() => setCreateOpen(false)}
              disabled={creating}
            >
              <span className="app-control-label">Cancel</span>
            </button>
            <button
              type="submit"
              className="btn-primary category-picker-create-submit app-control-root"
              disabled={creating || !newName.trim()}
            >
              <span className="app-control-label">{creating ? 'Creating…' : 'Create'}</span>
            </button>
          </div>
        </form>
      ) : (
        <>
          <div className="category-picker-search">
            <input
              type="text"
              placeholder="Search categories..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoFocus
            />
          </div>

          <div className="category-picker-list">
            {currentCategoryId != null && (
              <button
                type="button"
                className="category-picker-clear app-control-root"
                onClick={() => {
                  onSelect(null);
                  closePicker();
                }}
              >
                <span className="app-control-label">Clear category</span>
              </button>
            )}

            {grouped.length === 0 ? (
              <div className="category-picker-empty">No matches</div>
            ) : (
              grouped.map((group) => (
                <div key={group.parent.id} className="category-picker-group">
                  <div className="category-picker-group-header">
                    <TwemojiIcon emoji={group.parent.icon} set={group.parent.icon_set} size={14} />
                    <span>{group.parent.name}</span>
                  </div>
                  {group.children.map((cat) => (
                    <button
                      key={cat.id}
                      type="button"
                      className={`category-picker-item ${
                        currentCategoryId === cat.id ? 'is-selected' : ''
                      }`.trim()}
                      onClick={() => {
                        onSelect(cat.id);
                        closePicker();
                      }}
                      style={getCategoryThemeVars(cat, mode)}
                    >
                      <TwemojiIcon emoji={cat.icon} set={cat.icon_set} size={18} />
                      <span className="category-picker-item-name">{cat.name}</span>
                    </button>
                  ))}
                </div>
              ))
            )}
          </div>

          <div className="category-picker-footer">
            <button
              type="button"
              className="category-picker-create-trigger app-control-root"
              onClick={openCreate}
              disabled={parentGroups.length === 0}
            >
              <span className="app-control-icon" aria-hidden="true">
                <MdAdd />
              </span>
              <span className="app-control-label">New category</span>
            </button>
            <button
              type="button"
              className="category-picker-manage"
              onClick={handleManage}
            >
              Manage categories →
            </button>
          </div>
        </>
      )}

      <EmojiPickerPopover
        key={emojiOpen ? 'open' : 'closed'}
        isOpen={emojiOpen}
        onClose={() => setEmojiOpen(false)}
        onSelect={(emojiChar, setId) => {
          setNewIcon(emojiChar);
          setNewIconSet(setId || null);
        }}
        anchorRect={emojiAnchorRect}
      />
    </AnchoredPopover>
  );
}

export default CategoryPicker;
