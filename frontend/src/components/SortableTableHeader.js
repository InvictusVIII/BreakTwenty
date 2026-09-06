import { useEffect, useState } from 'react';
import TriangleIcon from './TriangleIcon';

export function getNextSortConfig(currentSort, key, defaultDirections = {}) {
  const defaultDirection = defaultDirections[key] || 'asc';
  if (!currentSort || currentSort.key !== key) {
    return { key, direction: defaultDirection };
  }
  if (currentSort.direction === defaultDirection) {
    return { key, direction: defaultDirection === 'asc' ? 'desc' : 'asc' };
  }
  return null;
}

function normalizeSortConfig(sortConfig, defaultDirections = {}) {
  if (!sortConfig || typeof sortConfig !== 'object') return null;
  const key = typeof sortConfig.key === 'string' ? sortConfig.key : '';
  const direction = sortConfig.direction === 'asc' || sortConfig.direction === 'desc'
    ? sortConfig.direction
    : '';
  const knownKeys = Object.keys(defaultDirections || {});

  if (!key || !direction) return null;
  if (knownKeys.length > 0 && !Object.prototype.hasOwnProperty.call(defaultDirections, key)) return null;
  return { key, direction };
}

function loadStoredSortConfig(storageKey, defaultDirections = {}, fallbackSort = null) {
  const storedValue = localStorage.getItem(storageKey);
  if (!storedValue) return normalizeSortConfig(fallbackSort, defaultDirections);
  try {
    return normalizeSortConfig(JSON.parse(storedValue), defaultDirections);
  } catch {
    localStorage.removeItem(storageKey);
    return normalizeSortConfig(fallbackSort, defaultDirections);
  }
}

function persistStoredSortConfig(storageKey, sortConfig, defaultDirections = {}) {
  const normalizedSort = normalizeSortConfig(sortConfig, defaultDirections);
  if (!normalizedSort) {
    localStorage.removeItem(storageKey);
    return;
  }
  localStorage.setItem(storageKey, JSON.stringify(normalizedSort));
}

export function usePersistentSortConfig(storageKey, fallbackSort = null, defaultDirections = {}) {
  const [sortConfig, setSortConfig] = useState(() => (
    loadStoredSortConfig(storageKey, defaultDirections, fallbackSort)
  ));

  useEffect(() => {
    persistStoredSortConfig(storageKey, sortConfig, defaultDirections);
  }, [defaultDirections, sortConfig, storageKey]);

  return [sortConfig, setSortConfig];
}

function SortableTableHeader({
  label,
  sortKey,
  sortConfig,
  onSort,
  defaultDirections = {},
  align = 'center',
  className = '',
  ariaLabelPrefix = 'Sort by',
  resetAriaLabel = 'Reset ordering',
}) {
  const isActive = sortConfig?.key === sortKey;
  const ariaSort = isActive
    ? (sortConfig.direction === 'asc' ? 'ascending' : 'descending')
    : 'none';
  const nextSort = getNextSortConfig(sortConfig, sortKey, defaultDirections);
  const buttonLabel = nextSort
    ? `${ariaLabelPrefix} ${label} ${nextSort.direction === 'asc' ? 'ascending' : 'descending'}`
    : resetAriaLabel;
  const sortDirection = isActive && sortConfig.direction === 'asc' ? 'up' : 'down';

  return (
    <span
      className={`sortable-table-header-cell ${className} is-${align} ${isActive ? 'is-active' : ''}`.trim()}
      role="columnheader"
      aria-sort={ariaSort}
    >
      <button
        type="button"
        className="sortable-table-header-button"
        onClick={onSort}
        aria-label={buttonLabel}
      >
        <span className="sortable-table-header-label">{label}</span>
        <span className="sortable-table-header-indicator" aria-hidden="true">
          {isActive ? <TriangleIcon direction={sortDirection} /> : null}
        </span>
      </button>
    </span>
  );
}

export default SortableTableHeader;
