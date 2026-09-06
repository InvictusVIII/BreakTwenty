import React from 'react';
import { APP_BRAND_NAME } from '../constants/brand';
import './BrandName.css';

const BRAND_PARTS = Object.freeze({
  break: 'Break',
  twenty: 'Twenty',
});

function BrandName({ className = '' }) {
  const classes = ['brand-name', className].filter(Boolean).join(' ');
  return (
    <span className={classes}>
      <span className="brand-name-break">{BRAND_PARTS.break}</span>
      <span className="brand-name-twenty">{BRAND_PARTS.twenty}</span>
    </span>
  );
}

export function renderBrandText(value, keyPrefix = 'brand-text') {
  if (value === null || value === undefined || value === false) return value;
  if (React.isValidElement(value)) return value;
  if (Array.isArray(value)) {
    return value.map((part, index) => (
      <React.Fragment key={`${keyPrefix}-${index}`}>
        {renderBrandText(part, `${keyPrefix}-${index}`)}
      </React.Fragment>
    ));
  }

  const text = String(value);
  const parts = text.split(APP_BRAND_NAME);
  if (parts.length === 1) return text;

  return parts.flatMap((part, index) => {
    const rendered = [];
    if (part) rendered.push(part);
    if (index < parts.length - 1) {
      rendered.push(<BrandName key={`${keyPrefix}-brand-${index}`} />);
    }
    return rendered;
  });
}

export function renderBrandMarkedText(value, keyPrefix = 'brand-marked-text') {
  if (value === null || value === undefined || value === false) return value;
  if (React.isValidElement(value)) return value;

  const text = String(value);
  const parts = [];
  const boldPattern = /\*\*(.+?)\*\*/g;
  let cursor = 0;
  let match;

  while ((match = boldPattern.exec(text)) !== null) {
    if (match.index > cursor) {
      parts.push(renderBrandText(text.slice(cursor, match.index), `${keyPrefix}-${parts.length}`));
    }
    parts.push(
      <strong key={`${keyPrefix}-strong-${parts.length}`}>
        {renderBrandText(match[1], `${keyPrefix}-strong-${parts.length}`)}
      </strong>,
    );
    cursor = match.index + match[0].length;
  }

  if (cursor < text.length) {
    parts.push(renderBrandText(text.slice(cursor), `${keyPrefix}-${parts.length}`));
  }

  return parts.length ? parts : renderBrandText(text, keyPrefix);
}

export function brandNameHtml() {
  return '<span class="brand-name"><span class="brand-name-break">Break</span><span class="brand-name-twenty">Twenty</span></span>';
}

export function replaceBrandNameHtml(value) {
  return String(value).replaceAll(APP_BRAND_NAME, brandNameHtml());
}

export default BrandName;
