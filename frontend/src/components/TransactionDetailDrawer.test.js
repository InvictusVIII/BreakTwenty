import React from 'react';
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import TransactionDetailDrawer from './TransactionDetailDrawer';

vi.mock('./SideDetailDrawer', () => ({
  SideDetailDrawerPanel: ({ children }) => <div>{children}</div>,
}));

vi.mock('./CategoryPicker', () => ({
  default: () => null,
}));

vi.mock('./CategoryPill', () => ({
  default: ({ category, onClick }) => (
    <button type="button" onClick={onClick}>{category?.name || 'Uncategorized'}</button>
  ),
}));

vi.mock('../hooks/useDismissibleLayer', () => ({
  default: () => {},
}));

const categories = [{ id: 1, name: 'Food', parent_id: null }];

function transaction(id, userDescription) {
  return {
    id,
    date: '2026-08-12',
    amount: -10,
    currency: 'CAD',
    description: `Raw ${id}`,
    display_description: `Transaction ${id}`,
    user_description: userDescription,
    user_notes: '',
    category: categories[0],
    category_source: 'auto',
    manual_kind: null,
    institution_name: 'Bank',
    account_name: 'Card',
    institution_provider: 'rbc',
    type: 'expense',
  };
}

function response(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: vi.fn().mockResolvedValue(payload),
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function drawerProps(currentTransaction, overrides = {}) {
  return {
    transaction: currentTransaction,
    animatedOpen: true,
    categories,
    onClose: vi.fn(),
    onSaved: vi.fn(),
    ...overrides,
  };
}

beforeEach(() => {
  document.body.innerHTML = '<div class="app-main"></div>';
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('TransactionDetailDrawer request ownership', () => {
  it('aborts and ignores a stale similar-count response after the selected row changes', async () => {
    const firstCount = deferred();
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/transactions/1/similar-count')) return firstCount.promise;
      if (url.endsWith('/transactions/2/similar-count')) return Promise.resolve(response({ count: 3 }));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const view = render(<TransactionDetailDrawer {...drawerProps(transaction(1, 'First'))} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const firstRequestOptions = fetchMock.mock.calls[0][1];

    view.rerender(<TransactionDetailDrawer {...drawerProps(transaction(2, 'Second'))} />);

    await waitFor(() => {
      expect(document.querySelector('.tx-detail-apply-heading')).toHaveTextContent(
        '2 other transactions share the same raw description',
      );
    });
    expect(firstRequestOptions.signal.aborted).toBe(true);

    await act(async () => {
      firstCount.resolve(response({ count: 9 }));
      await firstCount.promise;
    });

    expect(document.querySelector('.tx-detail-apply-heading')).toHaveTextContent(
      '2 other transactions share the same raw description',
    );
    expect(document.querySelector('.tx-detail-apply-heading')).not.toHaveTextContent(
      '8 other transactions share the same raw description',
    );
  });

  it('does not apply a completed save to a different transaction', async () => {
    const pendingSave = deferred();
    const onSaved = vi.fn();
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/similar-count')) return Promise.resolve(response({ count: 1 }));
      if (url.endsWith('/transactions/1') && options.method === 'PATCH') return pendingSave.promise;
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const view = render(
      <TransactionDetailDrawer {...drawerProps(transaction(1, 'First'), { onSaved })} />,
    );
    const description = await screen.findByDisplayValue('First');
    fireEvent.change(description, { target: { value: 'Updated first' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([, options]) => options?.method === 'PATCH')).toBe(true);
    });
    const patchCall = fetchMock.mock.calls.find(([, options]) => options?.method === 'PATCH');

    view.rerender(
      <TransactionDetailDrawer {...drawerProps(transaction(2, 'Second'), { onSaved })} />,
    );

    expect(await screen.findByDisplayValue('Second')).toBeInTheDocument();
    expect(patchCall[1].signal.aborted).toBe(true);

    await act(async () => {
      pendingSave.resolve(response({
        user_description: 'Updated first',
        user_notes: null,
        category_id: 1,
        category_source: 'manual',
        amount: -10,
        bulk_updated_count: 0,
      }));
      await pendingSave.promise;
    });

    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue('Second')).toBeInTheDocument();
  });

  it('does not start retained similar-count or save requests after the drawer closes', async () => {
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/transactions/1/similar-count')) {
        return Promise.resolve(response({ count: 1 }));
      }
      return Promise.resolve(response({}));
    });
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<TransactionDetailDrawer {...drawerProps(transaction(1, 'First'))} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    view.rerender(
      <TransactionDetailDrawer {...drawerProps(null, { animatedOpen: false })} />,
    );
    fireEvent.change(screen.getByDisplayValue('First'), { target: { value: 'Closing edit' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save', hidden: true }));
    await act(async () => Promise.resolve());

    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/similar-count'))).toHaveLength(1);
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === 'PATCH')).toBe(false);
  });

  it('does not start a default-category request from retained closing content', async () => {
    const manualCategoryTransaction = {
      ...transaction(1, 'First'),
      category_source: 'manual',
    };
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/similar-count')) return Promise.resolve(response({ count: 1 }));
      return Promise.resolve(response({ category_id: 1 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const view = render(
      <TransactionDetailDrawer {...drawerProps(manualCategoryTransaction)} />,
    );

    await screen.findByRole('button', { name: 'Default' });
    view.rerender(
      <TransactionDetailDrawer {...drawerProps(null, { animatedOpen: false })} />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Default', hidden: true }));
    await act(async () => Promise.resolve());

    expect(fetchMock.mock.calls.some(([url]) => url.endsWith('/default-category'))).toBe(false);
  });

  it('does not start a retained delete request after transaction handoff', async () => {
    const manualTransaction = {
      ...transaction(1, 'First'),
      manual_kind: 'manual',
    };
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/similar-count')) return Promise.resolve(response({ count: 1 }));
      return Promise.resolve(response({}));
    });
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<TransactionDetailDrawer {...drawerProps(manualTransaction)} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    const confirmButton = screen.getByRole('button', { name: 'Yes, delete' });
    view.rerender(<TransactionDetailDrawer {...drawerProps(transaction(2, 'Second'))} />);
    fireEvent.click(confirmButton);
    await act(async () => Promise.resolve());

    expect(fetchMock.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false);
    expect(await screen.findByDisplayValue('Second')).toBeInTheDocument();
  });
});

describe('TransactionDetailDrawer mutation contracts', () => {
  it('sends the current description and notes once and re-baselines a successful save', async () => {
    const onSaved = vi.fn();
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/similar-count')) return Promise.resolve(response({ count: 1 }));
      if (url.endsWith('/transactions/1') && options.method === 'PATCH') {
        return Promise.resolve(response({
          user_description: 'Updated description',
          user_notes: 'Private note',
          category_id: 1,
          category_source: 'manual',
          amount: -10,
          bulk_updated_count: 2,
        }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<TransactionDetailDrawer {...drawerProps(transaction(1, 'First'), { onSaved })} />);

    fireEvent.change(await screen.findByDisplayValue('First'), {
      target: { value: '  Updated description  ' },
    });
    fireEvent.change(screen.getByPlaceholderText('Add a private note for this transaction'), {
      target: { value: '  Private note  ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ bulkUpdated: 2 }));
    const patchCalls = fetchMock.mock.calls.filter(([, options]) => options?.method === 'PATCH');
    expect(patchCalls).toHaveLength(1);
    expect(JSON.parse(patchCalls[0][1].body)).toEqual({
      user_description: 'Updated description',
      user_notes: 'Private note',
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await act(async () => Promise.resolve());
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === 'PATCH')).toHaveLength(1);
  });

  it('saves description and a staged default reset in one request and re-baselines once', async () => {
    const onSaved = vi.fn();
    const currentTransaction = {
      ...transaction(1, 'First'),
      category_source: 'manual',
    };
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/similar-count')) return Promise.resolve(response({ count: 1 }));
      if (url.endsWith('/default-category')) return Promise.resolve(response({ category_id: 1 }));
      if (url.endsWith('/transactions/1') && options.method === 'PATCH') {
        return Promise.resolve(response({
          user_description: 'Updated with reset',
          user_notes: null,
          category_id: 1,
          category_source: 'auto',
          amount: -10,
          bulk_updated_count: 0,
        }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<TransactionDetailDrawer {...drawerProps(currentTransaction, { onSaved })} />);

    fireEvent.change(await screen.findByDisplayValue('First'), {
      target: { value: 'Updated with reset' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Default' }));
    expect(await screen.findByText('Reverts to the default category when you save.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    const patchCalls = fetchMock.mock.calls.filter(([, options]) => options?.method === 'PATCH');
    expect(patchCalls).toHaveLength(1);
    expect(JSON.parse(patchCalls[0][1].body)).toEqual({
      user_description: 'Updated with reset',
      reset_category: true,
    });
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
    expect(screen.getByDisplayValue('Updated with reset')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Default' })).not.toBeInTheDocument();
    expect(screen.queryByText('Reverts to the default category when you save.')).not.toBeInTheDocument();
  });

  it('keeps a failed atomic default reset staged and retryable', async () => {
    const onSaved = vi.fn();
    const currentTransaction = {
      ...transaction(1, 'First'),
      category_source: 'manual',
    };
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/similar-count')) return Promise.resolve(response({ count: 1 }));
      if (url.endsWith('/default-category')) return Promise.resolve(response({ category_id: 1 }));
      if (url.endsWith('/transactions/1') && options.method === 'PATCH') {
        return Promise.resolve(response({ detail: 'Atomic save rejected' }, { ok: false, status: 409 }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<TransactionDetailDrawer {...drawerProps(currentTransaction, { onSaved })} />);

    fireEvent.change(await screen.findByDisplayValue('First'), {
      target: { value: 'Unsaved description' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Default' }));
    expect(await screen.findByText('Reverts to the default category when you save.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('Atomic save rejected')).toBeInTheDocument();
    const patchCalls = fetchMock.mock.calls.filter(([, options]) => options?.method === 'PATCH');
    expect(patchCalls).toHaveLength(1);
    expect(JSON.parse(patchCalls[0][1].body)).toEqual({
      user_description: 'Unsaved description',
      reset_category: true,
    });
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue('Unsaved description')).toBeInTheDocument();
    expect(screen.getByText('Reverts to the default category when you save.')).toBeInTheDocument();
  });

  it('deletes a manual transaction, refreshes data, and closes the drawer', async () => {
    const onClose = vi.fn();
    const onSaved = vi.fn().mockResolvedValue(undefined);
    const currentTransaction = {
      ...transaction(1, 'First'),
      manual_kind: 'manual',
    };
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/similar-count')) return Promise.resolve(response({ count: 1 }));
      if (url.endsWith('/transactions/manual/1') && options.method === 'DELETE') {
        return Promise.resolve(response({ status: 'ok' }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    render(
      <TransactionDetailDrawer
        {...drawerProps(currentTransaction, { onClose, onSaved })}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(fetchMock.mock.calls.some(([url, options]) => (
      url.endsWith('/transactions/manual/1') && options?.method === 'DELETE'
    ))).toBe(true);
    expect(onClose).toHaveBeenCalledOnce();
  });
});
