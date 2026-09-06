import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { MdAdd, MdDelete, MdEdit } from 'react-icons/md';
import { API } from '../config';
import TwemojiIcon from '../components/TwemojiIcon';
import CategoryPill from '../components/CategoryPill';
import { ChartColorRow } from '../components/ChartColorControls';
import EmojiPickerPopover from '../components/EmojiPickerPopover';
import Dropdown from '../components/Dropdown';
import { useTheme } from '../appState';
import { readJsonResponse } from '../utils/apiResponse';
import { getCategoryIdentityColor, getCategoryThemeVars } from '../utils/categoryColors';
import { loadTransactionCategories } from '../utils/transactionCategories';
import './CategoriesSettings.css';

const CLASSIFICATION_LABELS = {
  income: 'Income',
  expense: 'Expense',
  transfer: 'Transfer',
  investment: 'Investment',
};

function CategoryRow({ category, onEdit, onDelete }) {
  const isLeaf = category.parent_id !== null;
  const { mode } = useTheme();
  return (
    <div className={`cs-row ${isLeaf ? 'is-leaf' : 'is-group'}`} style={getCategoryThemeVars(category, mode)}>
      <div className="cs-row-icon">
        <TwemojiIcon emoji={category.icon} set={category.icon_set} size={isLeaf ? 18 : 22} />
      </div>
      <div className="cs-row-name">{category.name}</div>
      {isLeaf && (
        <div className="cs-row-preview" data-tooltip="How this category appears on transactions">
          <CategoryPill category={category} size="sm" />
        </div>
      )}
      {!isLeaf && (
        <div className="cs-row-classification">
          {CLASSIFICATION_LABELS[category.classification] || category.classification}
        </div>
      )}
      <div className="cs-row-actions">
        <button type="button" className="cs-row-action" onClick={() => onEdit(category)} data-tooltip="Edit" aria-label="Edit">
          <MdEdit size={16} />
        </button>
        {!category.is_system && (
          <button type="button" className="cs-row-action is-danger" onClick={() => onDelete(category)} data-tooltip="Delete" aria-label="Delete">
            <MdDelete size={16} />
          </button>
        )}
      </div>
    </div>
  );
}

function CategoryForm({ initial, onSubmit, onCancel, onReset, allowClassificationChange }) {
  const { chartColors, mode } = useTheme();
  const [name, setName] = useState(initial?.name || '');
  const [icon, setIcon] = useState(initial?.icon || '');
  const [iconSet, setIconSet] = useState(initial?.icon_set || null);
  const [themeColors, setThemeColors] = useState({
    dark: initial?.color_dark || null,
    light: initial?.color_light || null,
  });
  const [classification, setClassification] = useState(initial?.classification || 'expense');
  const color = themeColors[mode] || chartColors.other;
  const colorField = mode === 'light' ? 'color_light' : 'color_dark';
  const setColor = (nextColor) => {
    setThemeColors((previous) => ({ ...previous, [mode]: nextColor }));
  };

  const previewCategory = {
    ...initial,
    id: initial?.id || 0,
    parent_id: initial?.parent_id || null,
    name: (name && name.trim()) || initial?.name || 'PREVIEW',
    icon: icon || initial?.icon || '❓',
    icon_set: iconSet || null,
    [colorField]: color || null,
    classification,
  };
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerAnchorRect, setPickerAnchorRect] = useState(null);

  const handleOpenPicker = (event) => {
    if (pickerOpen) {
      setPickerOpen(false);
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    setPickerAnchorRect({ top: rect.top, left: rect.left, bottom: rect.bottom, right: rect.right });
    setPickerOpen(true);
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    if (!name.trim()) {
      setError('Name is required');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      const payload = {
        name: name.trim(),
        icon: icon.trim() || null,
        icon_set: iconSet || null,
        classification,
      };
      if (initial?.id != null) {
        payload[colorField] = color || null;
      } else {
        payload.color_dark = themeColors.dark || color || null;
        payload.color_light = themeColors.light || color || null;
      }
      await onSubmit(payload);
    } catch (err) {
      setError(err.message || 'Failed to save');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="cs-form" onSubmit={handleSubmit}>
      <div className="cs-form-preview-row">
        <span className="cs-form-preview-label">Live preview</span>
        <CategoryPill category={previewCategory} size="md" />
      </div>
      <div className="cs-form-row">
        <label className="cs-form-name">
          Name
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} autoFocus maxLength={80} />
        </label>
        <label className="cs-form-icon">
          Icon
          <div className="cs-form-icon-row">
            <button
              type="button"
              className="cs-form-icon-current"
              onClick={handleOpenPicker}
              data-tooltip="Browse emojis"
              aria-label="Browse emojis"
            >
              <TwemojiIcon emoji={icon || '❓'} set={iconSet || undefined} size={22} />
            </button>
          </div>
        </label>
      </div>
      <div className="cs-form-color-row">
        <ChartColorRow
          label="Color"
          color={color}
          defaultColor={getCategoryIdentityColor(initial, mode) || color}
          onApply={(next) => setColor(next)}
          onReset={() => setColor(getCategoryIdentityColor(initial, mode) || color)}
        />
      </div>
      {allowClassificationChange && (
        <div className="cs-form-row">
          <label>
            Classification
            <Dropdown
              value={classification}
              options={Object.entries(CLASSIFICATION_LABELS).map(([key, label]) => ({ value: key, label }))}
              ariaLabel="Classification"
              onChange={setClassification}
            />
          </label>
        </div>
      )}
      {error && <div className="cs-form-error">{error}</div>}
      <div className="cs-form-actions">
        {onReset && initial?.is_system && (
          <button
            type="button"
            className="cs-form-button is-secondary cs-form-button-reset app-control-root"
            onClick={onReset}
            disabled={submitting}
            data-tooltip="Restore the original seeded icon, color, and name"
          >
            <span className="app-control-label">Reset to default</span>
          </button>
        )}
        <button type="button" className="cs-form-button is-secondary app-control-root" onClick={onCancel} disabled={submitting}>
          <span className="app-control-label">Cancel</span>
        </button>
        <button type="submit" className="btn-primary cs-form-button app-control-root" disabled={submitting}>
          <span className="app-control-label">{submitting ? 'Saving...' : 'Save'}</span>
        </button>
      </div>
      <EmojiPickerPopover
        isOpen={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onSelect={(emojiChar, setId) => {
          setIcon(emojiChar);
          setIconSet(setId || null);
        }}
        anchorRect={pickerAnchorRect}
      />
    </form>
  );
}

function CategoriesSettings() {
  const { mode } = useTheme();
  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(null); // { mode: 'create-group' | 'create-leaf' | 'edit', category?, parentId? }
  const [error, setError] = useState('');
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [resetTarget, setResetTarget] = useState(null);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState('');

  const fetchCategories = useCallback(async () => {
    setLoading(true);
    try {
      setCategories(await loadTransactionCategories());
    } catch (err) {
      setError(err.message || 'Failed to load categories');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function loadInitialCategories() {
      try {
        const nextCategories = await loadTransactionCategories();
        if (!cancelled) {
          setCategories(nextCategories);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || 'Failed to load categories');
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void loadInitialCategories();
    return () => {
      cancelled = true;
    };
  }, []);

  const groupedCategories = useMemo(() => {
    const parents = categories
      .filter((c) => c.parent_id === null)
      .sort((a, b) => a.sort_order - b.sort_order);
    return parents.map((parent) => ({
      parent,
      children: categories
        .filter((c) => c.parent_id === parent.id)
        .sort((a, b) => a.sort_order - b.sort_order),
    }));
  }, [categories]);

  const handleCreate = async (payload) => {
    const resp = await fetch(`${API}/categories`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    await readJsonResponse(resp, { label: 'Category creation' });
    await fetchCategories();
    setEditing(null);
  };

  const handleUpdate = async (categoryId, payload) => {
    const resp = await fetch(`${API}/categories/${categoryId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    await readJsonResponse(resp, { label: 'Category update' });
    await fetchCategories();
    setEditing(null);
  };

  const handleResetToDefault = (categoryId) => {
    setResetError('');
    setResetTarget(categoryId);
  };

  const confirmReset = async () => {
    if (!resetTarget) return;
    setResetting(true);
    setResetError('');
    try {
      const resp = await fetch(`${API}/categories/${resetTarget}/reset-to-seed`, { method: 'POST' });
      await readJsonResponse(resp, { label: 'Category reset' });
      await fetchCategories();
      setEditing(null);
      setResetTarget(null);
    } catch (err) {
      setResetError(err.message || 'Failed to reset');
    } finally {
      setResetting(false);
    }
  };

  const handleDelete = (category) => {
    setDeleteError('');
    setDeleteTarget(category);
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError('');
    try {
      const resp = await fetch(`${API}/categories/${deleteTarget.id}`, { method: 'DELETE' });
      await readJsonResponse(resp, { label: 'Category deletion' });
      await fetchCategories();
      setDeleteTarget(null);
    } catch (err) {
      setDeleteError(err.message || 'Failed to delete');
    } finally {
      setDeleting(false);
    }
  };

  if (loading) {
    return <div className="categories-settings"><div className="cs-loading">Loading categories...</div></div>;
  }

  return (
    <div className="page-frame is-narrow categories-settings">
      <div className="cs-header">
        <div className="cs-header-actions">
          <button
            type="button"
            className="btn-primary cs-add-group-button app-control-root"
            onClick={() => setEditing({ mode: 'create-group' })}
          >
            <span className="app-control-icon" aria-hidden="true"><MdAdd /></span>
            <span className="app-control-label">New main category</span>
          </button>
        </div>
      </div>

      {error && <div className="cs-error">{error}</div>}

      {editing?.mode === 'create-group' && (
        <div className="cs-edit-panel">
          <h3>New main category</h3>
          <CategoryForm
            onSubmit={handleCreate}
            onCancel={() => setEditing(null)}
            allowClassificationChange
          />
        </div>
      )}

      <div className="cs-groups">
        {groupedCategories.map(({ parent, children }) => (
          <div key={parent.id} className="panel-shell cs-group">
            <div className="cs-group-header" style={getCategoryThemeVars(parent, mode)}>
              <div className="cs-group-header-icon"><TwemojiIcon emoji={parent.icon} set={parent.icon_set} size={22} /></div>
              <div className="cs-group-header-name">{parent.name}</div>
              <div className="cs-group-header-classification">
                {CLASSIFICATION_LABELS[parent.classification] || parent.classification}
              </div>
              <div className="cs-group-header-actions">
                <button
                  type="button"
                  className="cs-row-action"
                  onClick={() => setEditing({ mode: 'edit', category: parent })}
                  data-tooltip="Edit group"
                  aria-label="Edit group"
                >
                  <MdEdit size={16} />
                </button>
                {!parent.is_system && (
                  <button
                    type="button"
                    className="cs-row-action is-danger"
                    onClick={() => handleDelete(parent)}
                    data-tooltip="Delete group"
                    aria-label="Delete group"
                  >
                    <MdDelete size={16} />
                  </button>
                )}
                <button
                  type="button"
                  className="cs-add-sub-button app-control-root"
                  onClick={() => setEditing({ mode: 'create-leaf', parentId: parent.id })}
                  data-tooltip="Add subcategory"
                >
                  <span className="app-control-icon" aria-hidden="true">
                    <MdAdd />
                  </span>
                  <span className="app-control-label">Add</span>
                </button>
              </div>
            </div>

            {editing?.mode === 'edit' && editing.category?.id === parent.id && (
              <div className="cs-edit-panel">
                <h3>Edit group</h3>
                <CategoryForm
                  initial={parent}
                  onSubmit={(payload) => handleUpdate(parent.id, payload)}
                  onCancel={() => setEditing(null)}
                  onReset={() => handleResetToDefault(parent.id)}
                  allowClassificationChange={!parent.is_system}
                />
              </div>
            )}

            {editing?.mode === 'create-leaf' && editing.parentId === parent.id && (
              <div className="cs-edit-panel">
                <h3>New subcategory under {parent.name}</h3>
                <CategoryForm
                  initial={{
                    classification: parent.classification,
                    parent_id: parent.id,
                    color_dark: parent.color_dark,
                    color_light: parent.color_light,
                  }}
                  onSubmit={(payload) => handleCreate({ ...payload, parent_id: parent.id, classification: parent.classification })}
                  onCancel={() => setEditing(null)}
                />
              </div>
            )}

            <div className="cs-leaves">
              {children.map((leaf) => (
                <React.Fragment key={leaf.id}>
                  <CategoryRow
                    category={leaf}
                    onEdit={(cat) => setEditing({ mode: 'edit', category: cat })}
                    onDelete={handleDelete}
                  />
                  {editing?.mode === 'edit' && editing.category?.id === leaf.id && (
                    <div className="cs-edit-panel cs-edit-panel-inset">
                      <CategoryForm
                        initial={leaf}
                        onSubmit={(payload) => handleUpdate(leaf.id, payload)}
                        onCancel={() => setEditing(null)}
                        onReset={() => handleResetToDefault(leaf.id)}
                      />
                    </div>
                  )}
                </React.Fragment>
              ))}
            </div>
          </div>
        ))}
      </div>

      {resetTarget && (
        <div className="modal-overlay modal-overlay-elevated">
          <div className="modal-content modal-dialog-card modal-dialog-wide">
            <p className="modal-2fa-text modal-dialog-title">Reset to default?</p>
            <p className="modal-dialog-copy">
              This category goes back to its seeded default icon, color, and name.
            </p>
            {resetError && <p className="modal-error-block">{resetError}</p>}
            <div className="modal-dialog-actions">
              <button className="btn-primary modal-dialog-action app-control-root" onClick={confirmReset} disabled={resetting}>
                <span className="app-control-label">{resetting ? 'Resetting…' : 'Yes, reset'}</span>
              </button>
              <button
                className="btn-secondary modal-dialog-action app-control-root"
                onClick={() => setResetTarget(null)}
                disabled={resetting}
              >
                <span className="app-control-label">Cancel</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div className="modal-overlay modal-overlay-elevated">
          <div className="modal-content modal-dialog-card modal-dialog-wide">
            <p className="modal-2fa-text modal-dialog-title">Delete “{deleteTarget.name}”?</p>
            <p className="modal-dialog-copy">
              {deleteTarget.parent_id === null
                ? 'This group will be removed.'
                : 'Transactions tagged here revert to their original category.'}
            </p>
            {deleteError && <p className="modal-error-block">{deleteError}</p>}
            <div className="modal-dialog-actions">
              <button className="btn-danger modal-dialog-action app-control-root" onClick={confirmDelete} disabled={deleting}>
                <span className="app-control-label">{deleting ? 'Deleting…' : 'Yes, delete'}</span>
              </button>
              <button
                className="btn-secondary modal-dialog-action app-control-root"
                onClick={() => setDeleteTarget(null)}
                disabled={deleting}
              >
                <span className="app-control-label">Cancel</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default CategoriesSettings;
