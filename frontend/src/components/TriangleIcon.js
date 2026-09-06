import React from 'react';

const TRIANGLE_PATHS = Object.freeze({
  down: 'M5 9.33h14l-7 8Z',
  up: 'M5 14.67h14l-7-8Z',
  right: 'M9.33 5l8 7-8 7Z',
  left: 'M14.67 5l-8 7 8 7Z',
});

function TriangleIcon({ direction = 'down', className = '' }) {
  const path = TRIANGLE_PATHS[direction] || TRIANGLE_PATHS.down;

  return (
    <svg
      className={`app-triangle-icon ${className}`.trim()}
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
    >
      <path d={path} />
    </svg>
  );
}

export default TriangleIcon;
