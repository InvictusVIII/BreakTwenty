import React, { useState } from 'react';
import { MdEdit, MdCheck, MdClose } from 'react-icons/md';
import { API } from '../config';

function EditableAccountName({
  accountId,
  name,
  onRename,
  isEditing = false,
  onEditStart = () => {},
  onEditEnd = () => {},
}) {
  const [value, setValue] = useState(name);
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    if (!value.trim() || value === name) {
      setValue(name);
      onEditEnd();
      return;
    }
    setSaving(true);
    try {
      const resp = await fetch(`${API}/accounts/${accountId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: value.trim() }),
      });
      const data = await resp.json();
      if (data.status === 'ok') {
        if (onRename) onRename(accountId, data.name);
        onEditEnd();
      }
    } catch (err) {
      console.error('Failed to rename:', err);
    } finally {
      setSaving(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleSave();
    }
    if (e.key === 'Escape') {
      setValue(name);
      onEditEnd();
    }
  };

  if (isEditing) {
    return (
      <div className="account-name-row" onClick={(e) => e.stopPropagation()}>
        <input
          className="edit-name-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          autoFocus
          disabled={saving}
        />
        <div className="edit-name-actions">
          <button type="button" className="edit-name-save" onClick={handleSave} disabled={saving}>
            <MdCheck size={18} />
          </button>
          <button type="button" className="edit-name-cancel" onClick={() => { setValue(name); onEditEnd(); }}>
            <MdClose size={18} />
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="account-name-row hoverable-edit">
      <span className="account-name">{name}</span>
      <button type="button" className="edit-name-btn" onClick={(e) => { e.stopPropagation(); setValue(name); onEditStart(); }}>
        <MdEdit size={14} />
      </button>
    </div>
  );
}

export default EditableAccountName;
