import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import ProviderSyncStatus from './ProviderSyncStatus';

describe('ProviderSyncStatus', () => {
  it('attaches the combined tooltip to status text and never to the sync icon', () => {
    render(
      <ProviderSyncStatus
        model={{
          label: 'Partial sync',
          tone: 'partial',
          colorClass: 'is-partial',
          iconState: 'attention',
          spin: false,
          actionTarget: 'sync',
          tooltipRows: [
            'Last sync: 2 hours ago',
            'Retry needed: Date range was not returned',
          ],
          tooltip: 'Last sync: 2 hours ago\nRetry needed: Date range was not returned',
        }}
        institutionName="BMO"
        onAction={vi.fn()}
        iconClassName="inst-sync-btn"
        textClassName="sync-timestamp"
      />,
    );

    expect(screen.getByRole('button', { name: 'Retry BMO sync' }))
      .toHaveClass('provider-sync-status-action');
    expect(screen.getByRole('button', { name: 'Retry BMO sync' })).not.toHaveAttribute('data-tooltip');
    expect(screen.getByText('Partial sync')).toHaveAttribute(
      'data-tooltip',
      'Last sync: 2 hours ago\nRetry needed: Date range was not returned',
    );
  });

  it('can render status-colored last-sync text without a redundant tray tooltip', () => {
    render(
      <ProviderSyncStatus
        model={{
          label: 'Sign in required',
          tone: 'auth',
          colorClass: 'is-auth',
          iconState: 'sync',
          spin: false,
          actionTarget: 'auth',
          tooltipRows: ['Last sync: 2 hours ago'],
          tooltip: 'Last sync: 2 hours ago',
        }}
        institutionName="BMO"
        showIcon={false}
        showTooltip={false}
        displayLabel="Last sync: 2 hours ago"
      />,
    );

    const timestamp = screen.getByText('Last sync: 2 hours ago');
    expect(timestamp).toHaveClass('is-auth');
    expect(timestamp).not.toHaveAttribute('data-tooltip');
    expect(timestamp).not.toHaveAttribute('tabindex');
    expect(timestamp).toHaveAccessibleName('Sign in required. Last sync: 2 hours ago');
    expect(screen.queryByText('Sign in required')).not.toBeInTheDocument();
  });

  it('does not create a hover target when the model has no tooltip rows', () => {
    render(
      <ProviderSyncStatus
        model={{
          label: 'Manually added',
          tone: 'manual',
          colorClass: 'is-manual',
          iconState: 'dot',
          spin: false,
          actionTarget: null,
          tooltipRows: [],
          tooltip: '',
        }}
        institutionName="Manual asset"
        showIcon={false}
      />,
    );

    const status = screen.getByText('Manually added');
    expect(status).not.toHaveAttribute('data-tooltip');
    expect(status).not.toHaveAttribute('tabindex');
  });
});
