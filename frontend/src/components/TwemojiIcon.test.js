import React from 'react';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import TwemojiIcon from './TwemojiIcon';

afterEach(cleanup);

describe('TwemojiIcon', () => {
  it('retries a different source after the previous source failed', () => {
    const view = render(<TwemojiIcon emoji="©️" set="noto" alt="Copyright" />);
    const copyrightIcon = screen.getByRole('img', { name: 'Copyright' });
    expect(copyrightIcon).toHaveAttribute('src', '/emoji/noto/a9.svg');

    fireEvent.error(copyrightIcon);
    expect(screen.queryByRole('img', { name: 'Copyright' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Copyright')).toHaveTextContent('©️');

    view.rerender(<TwemojiIcon emoji="®️" set="noto" alt="Registered" />);
    expect(screen.getByRole('img', { name: 'Registered' })).toHaveAttribute('src', '/emoji/noto/ae.svg');
  });

  it('uses unpadded keycap codepoints', () => {
    render(<TwemojiIcon emoji="0️⃣" set="fluent" alt="Zero keycap" />);
    expect(screen.getByRole('img', { name: 'Zero keycap' })).toHaveAttribute(
      'src',
      '/emoji/fluent/30-20e3.svg',
    );
  });
});

describe('bundled emoji filename parity', () => {
  it('ships the audited Noto and Fluent assets under runtime codepoint names', () => {
    const emojiRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../public/emoji');
    const expected = {
      noto: ['23-20e3', '2a-20e3', ...Array.from({ length: 10 }, (_, index) => `${index + 30}-20e3`), 'a9', 'ae'],
      fluent: [...Array.from({ length: 10 }, (_, index) => `${index + 30}-20e3`), 'a9', 'ae'],
    };
    const obsolete = {
      noto: ['0023-20e3', '002a-20e3', ...Array.from({ length: 10 }, (_, index) => `003${index}-20e3`), '00a9', '00ae'],
      fluent: [...Array.from({ length: 10 }, (_, index) => `003${index}-20e3`), '00a9', '00ae'],
    };

    Object.entries(expected).forEach(([set, filenames]) => {
      filenames.forEach((filename) => {
        expect(existsSync(path.join(emojiRoot, set, `${filename}.svg`))).toBe(true);
      });
    });
    Object.entries(obsolete).forEach(([set, filenames]) => {
      filenames.forEach((filename) => {
        expect(existsSync(path.join(emojiRoot, set, `${filename}.svg`))).toBe(false);
      });
    });
  });
});
