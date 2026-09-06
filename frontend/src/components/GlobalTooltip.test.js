import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import GlobalTooltip from './GlobalTooltip';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  delete document.documentElement.clientWidth;
  delete document.documentElement.clientHeight;
});

describe('GlobalTooltip', () => {
  it('shows and dismisses hint bubbles for keyboard focus', () => {
    render(
      <>
        <button type="button" data-tooltip="Keyboard hint">Trigger</button>
        <button type="button">Outside</button>
        <GlobalTooltip />
      </>,
    );

    const trigger = screen.getByRole('button', { name: 'Trigger' });
    const outside = screen.getByRole('button', { name: 'Outside' });

    fireEvent.focusIn(trigger);
    expect(screen.getByRole('tooltip')).toHaveTextContent('Keyboard hint');

    fireEvent.focusOut(trigger, { relatedTarget: outside });
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('shows hover-only hints on physical hover without retaining them from click focus', () => {
    render(
      <>
        <button type="button" data-tooltip="Dark theme" data-tooltip-hover-only>Theme</button>
        <GlobalTooltip />
      </>,
    );

    const trigger = screen.getByRole('button', { name: 'Theme' });

    fireEvent.focusIn(trigger);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    fireEvent.pointerOver(trigger);
    expect(screen.getByRole('tooltip')).toHaveTextContent('Dark theme');

    fireEvent.pointerDown(trigger);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    fireEvent.pointerOut(trigger, { relatedTarget: document.body });
    fireEvent.pointerOver(trigger);
    expect(screen.getByRole('tooltip')).toHaveTextContent('Dark theme');
  });

  it('restores the focused hint after a different hovered hint ends', () => {
    render(
      <>
        <button type="button" data-tooltip="Focused hint">Focused</button>
        <button type="button" data-tooltip="Hovered hint">Hovered</button>
        <GlobalTooltip />
      </>,
    );

    const focused = screen.getByRole('button', { name: 'Focused' });
    const hovered = screen.getByRole('button', { name: 'Hovered' });

    fireEvent.focusIn(focused);
    fireEvent.pointerOver(hovered);
    expect(screen.getByRole('tooltip')).toHaveTextContent('Hovered hint');

    fireEvent.pointerOut(hovered, { relatedTarget: document.body });
    expect(screen.getByRole('tooltip')).toHaveTextContent('Focused hint');
  });

  it('flips a right-side tooltip inside the viewport and dismisses it on scroll', () => {
    Object.defineProperty(document.documentElement, 'clientWidth', {
      configurable: true,
      value: 250,
    });
    Object.defineProperty(document.documentElement, 'clientHeight', {
      configurable: true,
      value: 160,
    });
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function getRect() {
      if (this.getAttribute?.('role') === 'tooltip') {
        return {
          bottom: 40,
          height: 40,
          left: 0,
          right: 80,
          top: 0,
          width: 80,
          x: 0,
          y: 0,
          toJSON: () => ({}),
        };
      }
      return {
        bottom: 80,
        height: 20,
        left: 200,
        right: 230,
        top: 60,
        width: 30,
        x: 200,
        y: 60,
        toJSON: () => ({}),
      };
    });
    render(
      <>
        <button type="button" data-tooltip="Clamped hint" data-tooltip-placement="right">
          Trigger
        </button>
        <GlobalTooltip />
      </>,
    );

    fireEvent.pointerOver(screen.getByRole('button', { name: 'Trigger' }));
    expect(screen.getByRole('tooltip')).toHaveStyle({ left: '104px', top: '50px' });

    fireEvent.scroll(window);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('flips the default tooltip below its trigger and clamps it inside the viewport', () => {
    Object.defineProperty(document.documentElement, 'clientWidth', {
      configurable: true,
      value: 200,
    });
    Object.defineProperty(document.documentElement, 'clientHeight', {
      configurable: true,
      value: 100,
    });
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function getRect() {
      if (this.getAttribute?.('role') === 'tooltip') {
        return {
          bottom: 30,
          height: 30,
          left: 0,
          right: 80,
          top: 0,
          width: 80,
          x: 0,
          y: 0,
          toJSON: () => ({}),
        };
      }
      return {
        bottom: 22,
        height: 20,
        left: -10,
        right: 10,
        top: 2,
        width: 20,
        x: -10,
        y: 2,
        toJSON: () => ({}),
      };
    });
    render(
      <>
        <button type="button" data-tooltip="Default hint">Trigger</button>
        <GlobalTooltip />
      </>,
    );

    fireEvent.pointerOver(screen.getByRole('button', { name: 'Trigger' }));

    expect(screen.getByRole('tooltip')).toHaveStyle({ left: '8px', top: '30px' });
  });

  it.each([
    ['pointerdown', () => fireEvent.pointerDown(document.body)],
    ['click', () => fireEvent.click(document.body)],
    ['resize', () => fireEvent.resize(window)],
    ['Escape', () => fireEvent.keyDown(document, { key: 'Escape' })],
  ])('dismisses the active tooltip on %s', (_eventName, dismiss) => {
    render(
      <>
        <button type="button" data-tooltip="Dismissible hint">Trigger</button>
        <GlobalTooltip />
      </>,
    );

    fireEvent.pointerOver(screen.getByRole('button', { name: 'Trigger' }));
    expect(screen.getByRole('tooltip')).toHaveTextContent('Dismissible hint');

    dismiss();
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('removes every global listener on unmount', () => {
    const documentAddSpy = vi.spyOn(document, 'addEventListener');
    const documentRemoveSpy = vi.spyOn(document, 'removeEventListener');
    const windowAddSpy = vi.spyOn(window, 'addEventListener');
    const windowRemoveSpy = vi.spyOn(window, 'removeEventListener');

    const { unmount } = render(<GlobalTooltip />);
    const documentListeners = ['pointerover', 'pointerout', 'focusin', 'focusout', 'pointerdown', 'click', 'keydown'];
    const windowListeners = ['scroll', 'resize'];
    const addedDocumentHandlers = new Map(documentListeners.map((type) => [
      type,
      documentAddSpy.mock.calls.find((call) => call[0] === type && call[2] === true)?.[1],
    ]));
    const addedWindowHandlers = new Map(windowListeners.map((type) => [
      type,
      windowAddSpy.mock.calls.find((call) => call[0] === type && call[2] === true)?.[1],
    ]));

    expect([...addedDocumentHandlers.values()]).not.toContain(undefined);
    expect([...addedWindowHandlers.values()]).not.toContain(undefined);

    unmount();

    documentListeners.forEach((type) => {
      expect(documentRemoveSpy).toHaveBeenCalledWith(type, addedDocumentHandlers.get(type), true);
    });
    windowListeners.forEach((type) => {
      expect(windowRemoveSpy).toHaveBeenCalledWith(type, addedWindowHandlers.get(type), true);
    });
  });
});
