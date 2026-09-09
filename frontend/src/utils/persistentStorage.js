function browserStorage() {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage || null;
  } catch (_) {
    return null;
  }
}

function desktopPreferenceStorage() {
  if (typeof window === 'undefined') return null;
  const preferences = window.breaktwentyDesktop?.preferences;
  return preferences
    && typeof preferences.getItem === 'function'
    && typeof preferences.setItem === 'function'
    && typeof preferences.removeItem === 'function'
    && typeof preferences.keys === 'function'
    ? preferences
    : null;
}

function readBrowserValue(key) {
  try {
    return browserStorage()?.getItem(String(key)) ?? null;
  } catch (_) {
    return null;
  }
}

function readPersistentValue(key) {
  const normalizedKey = String(key);
  const desktopStorage = desktopPreferenceStorage();
  if (desktopStorage) {
    try {
      const value = desktopStorage.getItem(normalizedKey);
      if (value !== null && value !== undefined) {
        return String(value);
      }
      const browserValue = readBrowserValue(normalizedKey);
      if (browserValue !== null) {
        desktopStorage.setItem(normalizedKey, browserValue);
      }
      return browserValue;
    } catch (_) {
    }
  }
  return readBrowserValue(normalizedKey);
}

function persistentKeys() {
  const keys = new Set();
  const desktopStorage = desktopPreferenceStorage();
  if (desktopStorage) {
    try {
      desktopStorage.keys().forEach((key) => keys.add(String(key)));
    } catch (_) {
    }
  }
  const storage = browserStorage();
  if (storage) {
    try {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key) keys.add(key);
      }
    } catch (_) {
    }
  }
  return [...keys].sort();
}

export const persistentStorage = {
  getItem(key) {
    return readPersistentValue(key);
  },
  setItem(key, value) {
    const normalizedKey = String(key);
    const normalizedValue = String(value);
    const desktopStorage = desktopPreferenceStorage();
    if (desktopStorage) {
      desktopStorage.setItem(normalizedKey, normalizedValue);
    }
    const storage = browserStorage();
    try {
      storage?.setItem(normalizedKey, normalizedValue);
    } catch (_) {
    }
  },
  removeItem(key) {
    const normalizedKey = String(key);
    const desktopStorage = desktopPreferenceStorage();
    if (desktopStorage) {
      desktopStorage.removeItem(normalizedKey);
    }
    const storage = browserStorage();
    try {
      storage?.removeItem(normalizedKey);
    } catch (_) {
    }
  },
  key(index) {
    return persistentKeys()[Number(index)] ?? null;
  },
  get length() {
    return persistentKeys().length;
  },
};
