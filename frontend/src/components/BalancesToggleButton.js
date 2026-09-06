import React from 'react';
import useBalancesHidden from '../hooks/useBalancesHidden';
import { VisibilityToolbarIcon } from './ToolbarActionIcon';

function BalancesToggleButton({ className = '' }) {
  const [balancesHidden, toggleBalancesHidden] = useBalancesHidden();
  const label = balancesHidden ? 'Show balance information' : 'Hide balance information';
  return (
    <button
      type="button"
      className={`top-toolbar-icon-btn balances-toggle-btn app-control-root ${balancesHidden ? 'is-active' : ''} ${className}`.trim()}
      onClick={toggleBalancesHidden}
      data-tooltip={label}
      aria-label={label}
      aria-pressed={balancesHidden}
    >
      <span className="app-control-icon" aria-hidden="true">
        <VisibilityToolbarIcon hidden={balancesHidden} />
      </span>
    </button>
  );
}

export default BalancesToggleButton;
