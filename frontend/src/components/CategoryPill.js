import React from 'react';
import TwemojiIcon, { DEFAULT_EMOJI_SET } from './TwemojiIcon';
import { useTheme } from '../appState';
import { getCategoryThemeVars } from '../utils/categoryColors';
import './CategoryPill.css';

/**
 * Standardized category pill used wherever a category appears next to a
 * transaction, amount, recurring item, or budget row.
 *
 * Pass `onClick` to make it interactive (renders as <button>). Omit to render
 * as a static <span>.
 *
 * `size` controls the visual density:
 *   - "sm" (default): dense table rows
 *   - "md": detail surfaces / dashboard widgets
 */
function CategoryPill({
  category,
  onClick,
  size = 'sm',
  className = '',
}) {
  const name = category?.name || 'Uncategorized';
  const icon = category?.icon || '❓';
  const isClickable = typeof onClick === 'function';
  const Element = isClickable ? 'button' : 'span';
  const { mode } = useTheme();

  const handleClick = isClickable
    ? (event) => {
        event.stopPropagation();
        onClick(event);
      }
    : undefined;

  return (
    <Element
      type={isClickable ? 'button' : undefined}
      className={`category-pill is-${size} ${category ? '' : 'is-uncategorized'} ${className}`.trim()}
      style={getCategoryThemeVars(category, mode)}
      onClick={handleClick}
    >
      <TwemojiIcon
        emoji={icon}
        set={category?.icon_set || DEFAULT_EMOJI_SET}
        size={size === 'md' ? 17 : 14}
        className="category-pill-icon"
      />
      <span className="category-pill-name">{name}</span>
    </Element>
  );
}

export default CategoryPill;
