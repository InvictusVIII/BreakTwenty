import React from 'react';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ThemeContext } from '../appState';
import { resolveCategoryAccentColor } from '../utils/categoryColors';
import CategoriesSettings from './CategoriesSettings';

const category = {
  id: 89,
  parent_id: null,
  name: 'Other',
  icon: '❓',
  icon_set: 'noto',
  color_dark: '#ffffff',
  color_light: '#8e877b',
  classification: 'expense',
  is_system: true,
  sort_order: 1,
};

function response(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: vi.fn().mockResolvedValue(payload),
  };
}

function renderCategories(fetchMock, mode = 'dark') {
  vi.stubGlobal('fetch', fetchMock);
  return render(
    <ThemeContext.Provider value={{ mode, colors: {}, chartColors: { other: '#999999' } }}>
      <CategoriesSettings />
    </ThemeContext.Provider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('CategoriesSettings updates', () => {
  it('derives the parent-family accent from the active theme', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ categories: [category] }));
    const { container } = renderCategories(fetchMock, 'light');

    await screen.findByText('Other');
    expect(container.querySelector('.cs-group-header')).toHaveStyle({
      '--cat-color': resolveCategoryAccentColor(category, 'light'),
    });
  });

  it('sends an accepted PATCH payload when changing a category color', async () => {
    const fetchMock = vi.fn((url, options = {}) => {
      if ((options.method || 'GET') === 'GET') {
        return Promise.resolve(response({ categories: [category] }));
      }
      if (options.method === 'PATCH' && url.endsWith('/categories/89')) {
        return Promise.resolve(response({ ...category, color_dark: '#123456' }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });

    renderCategories(fetchMock);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit group' }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit Color color' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Color hex color' }), {
      target: { value: '#123456' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      const patchCall = fetchMock.mock.calls.find(([, options = {}]) => options.method === 'PATCH');
      expect(patchCall).toBeDefined();
      expect(JSON.parse(patchCall[1].body)).toEqual({
        name: 'Other',
        icon: '❓',
        icon_set: 'noto',
        color_dark: '#123456',
        classification: 'expense',
      });
    });
  });

  it('updates only the light color when editing in light mode', async () => {
    const fetchMock = vi.fn((url, options = {}) => {
      if ((options.method || 'GET') === 'GET') {
        return Promise.resolve(response({ categories: [category] }));
      }
      if (options.method === 'PATCH' && url.endsWith('/categories/89')) {
        return Promise.resolve(response({ ...category, color_light: '#123456' }));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });

    renderCategories(fetchMock, 'light');
    fireEvent.click(await screen.findByRole('button', { name: 'Edit group' }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit Color color' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Color hex color' }), {
      target: { value: '#123456' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      const patchCall = fetchMock.mock.calls.find(([, options = {}]) => options.method === 'PATCH');
      const payload = JSON.parse(patchCall[1].body);
      expect(payload.color_light).toBe('#123456');
      expect(payload).not.toHaveProperty('color_dark');
    });
  });

  it('renders structured API validation errors as readable text', async () => {
    const fetchMock = vi.fn((url, options = {}) => {
      if ((options.method || 'GET') === 'GET') {
        return Promise.resolve(response({ categories: [category] }));
      }
      if (options.method === 'PATCH' && url.endsWith('/categories/89')) {
        return Promise.resolve(response(
          { detail: [{ msg: 'Invalid category color' }] },
          { ok: false, status: 422 },
        ));
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
    });

    renderCategories(fetchMock);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit group' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('Invalid category color')).toBeInTheDocument();
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument();
  });
});
