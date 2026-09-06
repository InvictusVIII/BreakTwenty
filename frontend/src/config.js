const DEFAULT_API = 'http://localhost:8000/api';

function getRuntimeApi() {
  if (typeof window === 'undefined') {
    return '';
  }
  return window.BREAKTWENTY_RUNTIME_CONFIG?.backendApiUrl || '';
}

export const API = (getRuntimeApi() || import.meta.env.VITE_API_URL || DEFAULT_API).replace(/\/+$/, '');
