import { useEffect } from 'react';

// Centralised body-scroll lock. Multiple overlays (the category picker, the
// emoji picker, the transaction detail drawer's nested popovers) can request
// the lock concurrently; the body stays locked while at least one consumer
// has it and unlocks cleanly when all release. The previous per-effect
// `document.body.style.overflow = 'hidden'` approach was fragile under
// concurrent overlays and could leave the body stuck locked when an unmount
// missed its cleanup, freezing the whole app.

const lockHolders = new Set();
let originalOverflow = '';
const LOCK_CLASS = 'app-body-scroll-locked';

function ensureGlobalStyle() {
  if (document.getElementById('app-body-scroll-lock-style')) return;
  const style = document.createElement('style');
  style.id = 'app-body-scroll-lock-style';
  style.textContent = `.${LOCK_CLASS} { overflow: hidden !important; }`;
  document.head.appendChild(style);
}

function applyLock() {
  ensureGlobalStyle();
  if (!document.body.classList.contains(LOCK_CLASS)) {
    originalOverflow = document.body.style.overflow;
    document.body.classList.add(LOCK_CLASS);
  }
}

function acquire(holder) {
  lockHolders.add(holder);
  applyLock();
}

function release(holder) {
  lockHolders.delete(holder);
  if (lockHolders.size === 0) {
    document.body.classList.remove(LOCK_CLASS);
    document.body.style.overflow = originalOverflow || '';
    originalOverflow = '';
  }
}

function forceReleaseAll() {
  lockHolders.clear();
  document.body.classList.remove(LOCK_CLASS);
  document.body.style.overflow = originalOverflow || '';
  originalOverflow = '';
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && lockHolders.size > 0) {
      applyLock();
    }
  });
}

export default function useBodyScrollLock(active) {
  useEffect(() => {
    if (!active) return undefined;
    const holder = Symbol('body-scroll-lock-holder');
    acquire(holder);
    return () => {
      release(holder);
    };
  }, [active]);
}

// Diagnostic export — if anything ever feels stuck, calling this from the
// browser console (`window.__appUnlockBodyScroll()`) force-clears the lock.
if (typeof window !== 'undefined') {
  window.__appUnlockBodyScroll = forceReleaseAll;
}
