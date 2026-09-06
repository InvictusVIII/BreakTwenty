import React, { StrictMode } from 'react';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import useBodyScrollLock from './useBodyScrollLock';

const LOCK_CLASS = 'app-body-scroll-locked';

function LockHarness({ active = true }) {
  useBodyScrollLock(active);
  return null;
}

afterEach(() => {
  cleanup();
  window.__appUnlockBodyScroll();
  document.body.style.overflow = '';
});

describe('useBodyScrollLock', () => {
  it('keeps the body locked until the last holder releases', () => {
    document.body.style.overflow = 'auto';
    const view = render(
      <>
        <LockHarness />
        <LockHarness />
      </>,
    );

    expect(document.body).toHaveClass(LOCK_CLASS);

    view.rerender(
      <>
        <LockHarness />
        <LockHarness active={false} />
      </>,
    );
    expect(document.body).toHaveClass(LOCK_CLASS);

    view.unmount();
    expect(document.body).not.toHaveClass(LOCK_CLASS);
    expect(document.body.style.overflow).toBe('auto');
  });

  it('preserves active holders across a hidden and visible transition', () => {
    const originalVisibilityState = Object.getOwnPropertyDescriptor(document, 'visibilityState');
    const view = render(<LockHarness />);

    try {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        value: 'hidden',
      });
      document.dispatchEvent(new Event('visibilitychange'));
      document.body.classList.remove(LOCK_CLASS);

      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        value: 'visible',
      });
      document.dispatchEvent(new Event('visibilitychange'));
      expect(document.body).toHaveClass(LOCK_CLASS);

      view.unmount();
      expect(document.body).not.toHaveClass(LOCK_CLASS);
    } finally {
      if (originalVisibilityState) {
        Object.defineProperty(document, 'visibilityState', originalVisibilityState);
      } else {
        delete document.visibilityState;
      }
    }
  });

  it('balances holder cleanup under React Strict Mode', () => {
    const view = render(
      <StrictMode>
        <LockHarness />
      </StrictMode>,
    );

    expect(document.body).toHaveClass(LOCK_CLASS);
    view.unmount();
    expect(document.body).not.toHaveClass(LOCK_CLASS);
  });
});
