import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { persistentStorage } from './persistentStorage';

describe('persistent storage', () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete window.breaktwentyDesktop;
  });

  it('keeps durable app state behind the shared preference boundary', () => {
    const srcRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
    const pending = [srcRoot];
    const directLocalStorageFiles = [];
    while (pending.length > 0) {
      const current = pending.pop();
      fs.readdirSync(current, { withFileTypes: true }).forEach((entry) => {
        const entryPath = path.join(current, entry.name);
        if (entry.isDirectory()) {
          pending.push(entryPath);
        } else if (/\.(?:js|jsx)$/.test(entry.name) && !entry.name.includes('.test.')) {
          const source = fs.readFileSync(entryPath, 'utf8');
          if (source.includes('localStorage')) {
            directLocalStorageFiles.push(path.relative(srcRoot, entryPath).replaceAll(path.sep, '/'));
          }
        }
      });
    }

    expect(directLocalStorageFiles.sort()).toEqual([
      'components/promoDemoEnvironment.js',
      'utils/persistentStorage.js',
    ]);
  });

  afterEach(() => {
    delete window.breaktwentyDesktop;
  });

  it('uses browser local storage outside the desktop shell', () => {
    persistentStorage.setItem('breaktwenty_theme_mode_v1', 'light');

    expect(persistentStorage.getItem('breaktwenty_theme_mode_v1')).toBe('light');
    expect(window.localStorage.getItem('breaktwenty_theme_mode_v1')).toBe('light');
  });

  it('uses desktop-owned preferences and mirrors writes into the current origin', () => {
    const values = new Map([['breaktwenty_last_auto_sync', '1234']]);
    window.breaktwentyDesktop = {
      preferences: {
        getItem: vi.fn((key) => values.get(key) ?? null),
        setItem: vi.fn((key, value) => values.set(key, value)),
        removeItem: vi.fn((key) => values.delete(key)),
        keys: vi.fn(() => [...values.keys()]),
      },
    };

    expect(persistentStorage.getItem('breaktwenty_last_auto_sync')).toBe('1234');
    persistentStorage.setItem('breaktwenty_dashboard_chart_colors_v2', '{"netWorth":"#123abc"}');

    expect(values.get('breaktwenty_dashboard_chart_colors_v2')).toBe('{"netWorth":"#123abc"}');
    expect(window.localStorage.getItem('breaktwenty_dashboard_chart_colors_v2'))
      .toBe('{"netWorth":"#123abc"}');
  });

  it('migrates a current-origin value when the desktop preference is missing', () => {
    const setItem = vi.fn();
    window.localStorage.setItem('breaktwenty_hide_balances', 'true');
    window.breaktwentyDesktop = {
      preferences: {
        getItem: vi.fn(() => null),
        setItem,
        removeItem: vi.fn(),
        keys: vi.fn(() => []),
      },
    };

    expect(persistentStorage.getItem('breaktwenty_hide_balances')).toBe('true');
    expect(setItem).toHaveBeenCalledWith('breaktwenty_hide_balances', 'true');
  });

  it('keeps market fallbacks available after the browser origin changes', () => {
    const values = new Map();
    window.breaktwentyDesktop = {
      preferences: {
        getItem: vi.fn((key) => values.get(key) ?? null),
        setItem: vi.fn((key, value) => values.set(key, value)),
        removeItem: vi.fn((key) => values.delete(key)),
        keys: vi.fn(() => [...values.keys()]),
      },
    };
    const marketStrip = '{"status":"ok","tiles":[{"id":"sp500","value":1}]}';
    const marketNews = '{"status":"ok","articles":[{"title":"Markets"}]}';

    persistentStorage.setItem('breaktwenty_market_strip_payload_v1', marketStrip);
    persistentStorage.setItem('breaktwenty_market_news_payload_v1', marketNews);
    window.localStorage.clear();

    expect(persistentStorage.getItem('breaktwenty_market_strip_payload_v1')).toBe(marketStrip);
    expect(persistentStorage.getItem('breaktwenty_market_news_payload_v1')).toBe(marketNews);
  });
});
