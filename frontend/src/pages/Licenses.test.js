import React from 'react';
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import Licenses from './Licenses';

describe('legal and licence links', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('links to the website policies and current source-available licence', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));

    render(<Licenses />);

    expect(screen.getByRole('link', { name: 'Privacy Policy' }))
      .toHaveAttribute('href', 'https://breaktwenty.com/privacy/');
    expect(screen.getByRole('link', { name: 'Terms of Use' }))
      .toHaveAttribute('href', 'https://breaktwenty.com/terms/');
    expect(screen.getByRole('link', { name: 'Security' }))
      .toHaveAttribute('href', 'https://breaktwenty.com/security/');
    expect(screen.getByRole('link', { name: 'BreakTwenty Source-Available License 1.0' }))
      .toHaveAttribute(
        'href',
        'https://github.com/InvictusVIII/BreakTwenty/blob/main/LICENSE.md',
      );
    expect(screen.getAllByText(/source-available license 1\.0/i)).toHaveLength(2);
    expect(screen.queryByText(/PolyForm Perimeter/i)).not.toBeInTheDocument();
    expect(screen.getByText(/developer does not receive or control that local data/i))
      .toBeInTheDocument();
    expect(screen.getByText(/only when you intentionally export and send them/i))
      .toBeInTheDocument();
  });
});
