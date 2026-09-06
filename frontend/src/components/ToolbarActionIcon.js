import React from 'react';

const ICONS = Object.freeze({
  visibility: {
    viewBox: '0 0 24 24',
    path: 'M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z',
  },
  'visibility-off': {
    // The artwork spans y=3..22, so its viewBox center is y=12.5.
    viewBox: '0 0.5 24 24',
    path: 'M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.43-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46A11.804 11.804 0 0 0 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78 3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z',
  },
  export: {
    // The artwork spans y=3..20, so its viewBox center is y=11.5.
    viewBox: '0 -0.5 24 24',
    path: 'M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z',
  },
});

function ToolbarActionIcon({ name }) {
  const icon = ICONS[name];

  return (
    <svg
      className="app-toolbar-action-icon"
      viewBox={icon.viewBox}
      aria-hidden="true"
      focusable="false"
    >
      <path d={icon.path} />
    </svg>
  );
}

export function VisibilityToolbarIcon({ hidden = false }) {
  return <ToolbarActionIcon name={hidden ? 'visibility-off' : 'visibility'} />;
}

export function ExportToolbarIcon() {
  return <ToolbarActionIcon name="export" />;
}
