import { fireEvent, render, screen } from '@testing-library/react';
import InstitutionAccountSelector, {
  formatScopeSelectionSummary,
} from './InstitutionAccountSelector';

test('renders the canonical scope account shape', () => {
  const onToggleAccount = vi.fn();
  render(
    <InstitutionAccountSelector
      institutions={[{
        id: 7,
        key: 'institution-7',
        name: 'North Bank',
        provider: 'north',
        accountIds: [11],
        accounts: [{ id: 11, name: 'Chequing', account_type: 'chequing', currency: 'CAD' }],
      }]}
      selectedAccountIds={new Set([11])}
      onToggleInstitution={vi.fn()}
      onToggleAccount={onToggleAccount}
    />,
  );

  fireEvent.click(screen.getByRole('tab', { name: 'Accounts' }));
  expect(screen.getAllByText('Chequing')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: /Chequing.*North Bank/ }));
  expect(onToggleAccount).toHaveBeenCalledWith(11, expect.objectContaining({ account_type: 'chequing' }));
});

test('exposes mixed institution selection and searches canonical account metadata', () => {
  const onToggleInstitution = vi.fn();
  render(
    <InstitutionAccountSelector
      institutions={[
        {
          id: 7,
          key: 'institution-7',
          name: 'North Bank',
          provider: 'north',
          accountIds: [11, 12],
          accounts: [
            { id: 11, name: 'Daily Account', account_type: 'chequing', currency: 'CAD' },
            { id: 12, name: 'Rainy Day', account_type: 'savings', currency: 'CAD' },
          ],
        },
        {
          id: 8,
          key: 'institution-8',
          name: 'South Credit',
          provider: 'south',
          accountIds: [21],
          accounts: [{ id: 21, name: 'Rewards', account_type: 'credit_card', currency: 'USD' }],
        },
      ]}
      selectedAccountIds={[11]}
      onToggleInstitution={onToggleInstitution}
      onToggleAccount={vi.fn()}
    />,
  );

  const northTile = screen.getByRole('button', { name: 'North Bank' });
  expect(northTile).toHaveAttribute('aria-pressed', 'mixed');
  fireEvent.click(northTile);
  expect(onToggleInstitution).toHaveBeenCalledWith(expect.objectContaining({
    id: 7,
    isPartiallySelected: true,
    selectedCount: 1,
  }));

  fireEvent.change(screen.getByRole('searchbox', { name: 'Search institutions and accounts' }), {
    target: { value: 'USD' },
  });
  expect(screen.queryByRole('button', { name: 'North Bank' })).not.toBeInTheDocument();
  const southTile = screen.getByRole('button', { name: 'South Credit' });
  expect(southTile).toBeInTheDocument();
  expect(southTile).toHaveAttribute('data-tooltip-hover-only');

  fireEvent.click(screen.getByRole('tab', { name: 'Accounts' }));
  expect(screen.queryByRole('button', { name: /Daily Account/ })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Rewards.*South Credit/ })).toBeInTheDocument();
});

test('exposes fully selected institution state and forwards the enriched selection counts', () => {
  const onToggleInstitution = vi.fn();
  render(
    <InstitutionAccountSelector
      institutions={[{
        id: 7,
        key: 'institution-7',
        name: 'North Bank',
        provider: 'north',
        accountIds: [11, 12],
        accounts: [
          { id: 11, name: 'Daily Account', account_type: 'chequing', currency: 'CAD' },
          { id: 12, name: 'Rainy Day', account_type: 'savings', currency: 'CAD' },
        ],
      }]}
      selectedAccountIds={new Set([11, 12])}
      onToggleInstitution={onToggleInstitution}
      onToggleAccount={vi.fn()}
    />,
  );

  const northTile = screen.getByRole('button', { name: 'North Bank' });
  expect(northTile).toHaveAttribute('aria-pressed', 'true');
  expect(northTile).toHaveAttribute('data-tooltip', 'North Bank');
  expect(northTile).toHaveAttribute('data-tooltip-hover-only');

  fireEvent.click(northTile);
  expect(onToggleInstitution).toHaveBeenCalledWith(expect.objectContaining({
    id: 7,
    isAllSelected: true,
    isPartiallySelected: false,
    selectedCount: 2,
  }));
});

test('formats complete, partial, and empty scope summaries', () => {
  expect(formatScopeSelectionSummary({
    totalInstitutions: 2,
    selectedInstitutions: 2,
    selectedAccounts: 3,
  })).toBe('2 Institutions · 3 Accounts');
  expect(formatScopeSelectionSummary({
    totalInstitutions: 2,
    selectedInstitutions: 1,
    selectedAccounts: 1,
  })).toBe('1 of 2 institutions · 1 Account');
  expect(formatScopeSelectionSummary({
    totalInstitutions: 2,
    selectedInstitutions: 0,
    selectedAccounts: 0,
  })).toBe('No sources selected');
});
