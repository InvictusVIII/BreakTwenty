import { useCallback, useEffect, useState } from 'react';

const STORAGE_KEY = 'breaktwenty_hide_balances';
const EVENT_NAME = 'breaktwenty:balances-hidden-change';

function readStoredValue() {
  if (typeof window === 'undefined') return false;
  return window.localStorage.getItem(STORAGE_KEY) === 'true';
}

export function getBalancesHidden() {
  return readStoredValue();
}

export function setBalancesHidden(nextValue) {
  if (typeof window === 'undefined') return;
  const normalized = Boolean(nextValue);
  window.localStorage.setItem(STORAGE_KEY, String(normalized));
  window.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: normalized }));
}

export default function useBalancesHidden() {
  const [hidden, setHidden] = useState(readStoredValue);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;

    const handleCustom = (event) => {
      setHidden(Boolean(event?.detail));
    };
    const handleStorage = (event) => {
      if (event.key !== STORAGE_KEY) return;
      setHidden(event.newValue === 'true');
    };

    window.addEventListener(EVENT_NAME, handleCustom);
    window.addEventListener('storage', handleStorage);
    return () => {
      window.removeEventListener(EVENT_NAME, handleCustom);
      window.removeEventListener('storage', handleStorage);
    };
  }, []);

  const toggle = useCallback(() => {
    setBalancesHidden(!readStoredValue());
  }, []);

  return [hidden, toggle];
}
