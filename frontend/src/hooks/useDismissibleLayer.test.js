import React, { useRef } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import useDismissibleLayer from './useDismissibleLayer';

function DismissibleHarness({ onDismiss, open = true }) {
  const layerRef = useRef(null);
  useDismissibleLayer({
    open,
    ref: layerRef,
    onDismiss,
    pointerEvent: 'mousedown',
  });

  return (
    <>
      <div ref={layerRef} data-testid="layer">
        <button type="button">Inside</button>
      </div>
      <button type="button" data-app-non-dismiss-interaction>
        Theme
      </button>
      <button type="button">
        Outside
      </button>
      <div data-testid="scrollable" style={{ overflowY: 'scroll' }} />
    </>
  );
}

describe('useDismissibleLayer', () => {
  it('keeps dismissible layers open when the theme toggle is pressed', () => {
    const onDismiss = vi.fn();
    render(<DismissibleHarness onDismiss={onDismiss} />);

    fireEvent.mouseDown(screen.getByRole('button', { name: 'Theme' }));
    expect(onDismiss).not.toHaveBeenCalled();

    fireEvent.mouseDown(screen.getByRole('button', { name: 'Outside' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('dismisses on Escape and ignores pointer events inside the layer', () => {
    const onDismiss = vi.fn();
    render(<DismissibleHarness onDismiss={onDismiss} />);

    fireEvent.mouseDown(screen.getByRole('button', { name: 'Inside' }));
    expect(onDismiss).not.toHaveBeenCalled();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('removes document listeners when the layer unmounts', () => {
    const onDismiss = vi.fn();
    const view = render(<DismissibleHarness onDismiss={onDismiss} />);

    view.unmount();
    fireEvent.mouseDown(document.body);
    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onDismiss).not.toHaveBeenCalled();
  });

  it('ignores pointer events on a native scrollbar', () => {
    const onDismiss = vi.fn();
    render(<DismissibleHarness onDismiss={onDismiss} />);
    const scrollable = screen.getByTestId('scrollable');
    Object.defineProperties(scrollable, {
      clientHeight: { configurable: true, value: 100 },
      clientWidth: { configurable: true, value: 180 },
      offsetHeight: { configurable: true, value: 100 },
      offsetWidth: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 200 },
      scrollWidth: { configurable: true, value: 180 },
    });
    scrollable.getBoundingClientRect = () => ({
      bottom: 100,
      height: 100,
      left: 0,
      right: 200,
      top: 0,
      width: 200,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    fireEvent.mouseDown(scrollable, { clientX: 195, clientY: 50 });

    expect(onDismiss).not.toHaveBeenCalled();
  });
});
