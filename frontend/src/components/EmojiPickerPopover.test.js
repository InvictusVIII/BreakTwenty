import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import EmojiPickerPopover from './EmojiPickerPopover';

const ANCHOR_RECT = {
  top: 20,
  left: 20,
  right: 60,
  bottom: 60,
  width: 40,
  height: 40,
};

const EMOJI_DATA = {
  categories: [
    { id: 'people', emojis: ['wave'] },
  ],
  emojis: {
    wave: {
      id: 'wave',
      name: 'Wave',
      keywords: ['hello'],
      skins: [{ native: 'x' }],
    },
  },
};

function renderPicker(loader, overrides = {}) {
  const onClose = overrides.onClose || vi.fn();
  const onSelect = overrides.onSelect || vi.fn();
  return render(
    <EmojiPickerPopover
      isOpen
      anchorRect={ANCHOR_RECT}
      onClose={onClose}
      onSelect={onSelect}
      emojiDataLoader={loader}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('EmojiPickerPopover', () => {
  it('shows a retry state when emoji data fails to load', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const loader = vi.fn()
      .mockRejectedValueOnce(new Error('chunk failed'))
      .mockResolvedValueOnce(EMOJI_DATA);

    renderPicker(loader);

    expect(await screen.findByText('Emoji data could not load.')).toBeInTheDocument();
    expect(loader).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('button', { name: 'Wave' })).toBeInTheDocument();
    expect(screen.queryByText('Emoji data could not load.')).not.toBeInTheDocument();
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('keeps the finance icon set usable when emoji data fails to load', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const loader = vi.fn().mockRejectedValue(new Error('chunk failed'));

    renderPicker(loader);

    expect(await screen.findByText('Emoji data could not load.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Finance' }));

    expect(await screen.findByRole('button', { name: 'Money' })).toBeInTheDocument();
    expect(loader).toHaveBeenCalledTimes(1);
  });

  it('retries automatically when the browser comes back online', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const loader = vi.fn()
      .mockRejectedValueOnce(new Error('chunk failed'))
      .mockResolvedValueOnce(EMOJI_DATA);

    renderPicker(loader);

    expect(await screen.findByText('Emoji data could not load.')).toBeInTheDocument();
    fireEvent(window, new Event('online'));

    expect(await screen.findByRole('button', { name: 'Wave' })).toBeInTheDocument();
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('returns the selected value and icon set before closing', async () => {
    const onClose = vi.fn();
    const onSelect = vi.fn();
    renderPicker(vi.fn().mockResolvedValue(EMOJI_DATA), { onClose, onSelect });

    fireEvent.click(await screen.findByRole('button', { name: 'Wave' }));

    expect(onSelect).toHaveBeenCalledWith('x', 'twemoji');
    expect(onSelect.mock.invocationCallOrder[0]).toBeLessThan(onClose.mock.invocationCallOrder[0]);
    expect(onClose).toHaveBeenCalledOnce();
  });
});
