import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const TOKEN = `${'A'.repeat(43)}=`;

beforeEach(() => {
  vi.resetModules();
  window.localStorage.removeItem('breaktwenty_promo_demo_active_v1');
  window.localStorage.removeItem('breaktwenty_promo_demo_store_v1');
  window.breaktwentyDesktop = {
    getLaunchAuth: vi.fn().mockResolvedValue({
      backendApiUrl: 'http://127.0.0.1:8000/api',
      accessToken: TOKEN,
    }),
  };
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete window.breaktwentyDesktop;
  delete window.BREAKTWENTY_RUNTIME_CONFIG;
  window.localStorage.removeItem('breaktwenty_promo_demo_active_v1');
  window.localStorage.removeItem('breaktwenty_promo_demo_store_v1');
});

describe('launch-auth API transport', () => {
  it('adds the renderer bearer only to the exact backend API boundary', async () => {
    const nativeFetch = vi.fn().mockResolvedValue(new Response('{}'));
    window.fetch = nativeFetch;
    const { installApiTransport } = await import('./apiTransport');
    await installApiTransport();

    expect(window.BREAKTWENTY_RUNTIME_CONFIG.backendApiUrl).toBe('http://127.0.0.1:8000/api');

    await window.fetch('http://127.0.0.1:8000/api/health');
    await window.fetch('http://127.0.0.1:8000/apiary/nope');
    await window.fetch('https://example.test/api/health');

    const authenticatedHeaders = new Headers(nativeFetch.mock.calls[0][1].headers);
    expect(authenticatedHeaders.get('Authorization')).toBe(`Bearer ${TOKEN}`);
    expect(nativeFetch.mock.calls[1][1]).toBeUndefined();
    expect(nativeFetch.mock.calls[2][1]).toBeUndefined();
  });

  it('fails closed before sending a bearer when desktop supplies a hostile backend URL', async () => {
    const rejected = [
      'https://127.0.0.1:8000/api',
      'http://backend.test/api',
      'http://localhost.example/api',
      'http://127.0.0.2/api',
      'http://user:password@localhost:8000/api',
      'http://localhost:8000/api?next=evil',
      'http://localhost:8000/api#fragment',
      'http://localhost:8000/api/v1',
      'http://localhost\\@evil.test/api',
    ];
    for (const backendApiUrl of rejected) {
      vi.resetModules();
      const nativeFetch = vi.fn();
      window.fetch = nativeFetch;
      window.breaktwentyDesktop.getLaunchAuth.mockResolvedValueOnce({
        backendApiUrl,
        accessToken: TOKEN,
      });
      const { installApiTransport } = await import('./apiTransport');

      await expect(installApiTransport()).rejects.toThrow(/private local API boundary/);
      expect(nativeFetch).not.toHaveBeenCalled();
      expect(window.fetch).toBe(nativeFetch);
    }
  });

  it('preserves Request semantics while overriding any caller-supplied bearer', async () => {
    const nativeFetch = vi.fn().mockResolvedValue(new Response('{}'));
    window.fetch = nativeFetch;
    const { installApiTransport } = await import('./apiTransport');
    await installApiTransport();
    const request = new Request('http://127.0.0.1:8000/api/settings', {
      method: 'POST',
      headers: { Authorization: 'Bearer attacker', 'Content-Type': 'application/json' },
      body: '{}',
    });

    await window.fetch(request);

    const forwarded = nativeFetch.mock.calls[0][0];
    expect(forwarded).toBeInstanceOf(Request);
    expect(forwarded.method).toBe('POST');
    expect(forwarded.headers.get('Authorization')).toBe(`Bearer ${TOKEN}`);
    expect(forwarded.headers.get('Content-Type')).toBe('application/json');
  });

  it('remains beneath the inactive promo fetch wrapper so normal API calls stay authenticated', async () => {
    const nativeFetch = vi.fn().mockResolvedValue(new Response('{}'));
    window.fetch = nativeFetch;
    const { installApiTransport } = await import('./apiTransport');
    await installApiTransport();
    const { installPromoDemoFetch } = await import('../components/promoDemoEnvironment');
    installPromoDemoFetch();

    await window.fetch('http://127.0.0.1:8000/api/not-a-promo-route');

    const headers = new Headers(nativeFetch.mock.calls[0][1].headers);
    expect(headers.get('Authorization')).toBe(`Bearer ${TOKEN}`);
  });

  it('routes apiFetch through the active promo boundary without reaching the native backend fetch', async () => {
    const nativeFetch = vi.fn().mockResolvedValue(new Response('{}'));
    window.fetch = nativeFetch;
    const { apiFetch, installApiTransport } = await import('./apiTransport');
    await installApiTransport();
    const { installPromoDemoFetch, setPromoDemoActive } = await import('../components/promoDemoEnvironment');
    installPromoDemoFetch();
    setPromoDemoActive(true);

    const response = await apiFetch('http://127.0.0.1:8000/api/accounts');
    const accounts = await response.json();

    expect(response.status).toBe(200);
    expect(accounts.some((account) => account.provider === 'real_estate')).toBe(true);
    expect(nativeFetch).not.toHaveBeenCalled();
    setPromoDemoActive(false);
  });

  it('replaces headerless EventSource with an authenticated fetch stream', async () => {
    let controller;
    const stream = new ReadableStream({
      start(nextController) {
        controller = nextController;
      },
    });
    const nativeFetch = vi.fn().mockResolvedValue(new Response(stream, { status: 200 }));
    window.fetch = nativeFetch;
    const { installApiTransport } = await import('./apiTransport');
    await installApiTransport();

    const source = new window.EventSource('http://127.0.0.1:8000/api/events/stream');
    const message = new Promise((resolve) => { source.onmessage = resolve; });
    await vi.waitFor(() => expect(nativeFetch).toHaveBeenCalledOnce());
    controller.enqueue(new TextEncoder().encode('data: {"type":"sync_activity"}\n\n'));
    const event = await message;
    source.close();

    expect(event.data).toBe('{"type":"sync_activity"}');
    const headers = new Headers(nativeFetch.mock.calls[0][1].headers);
    expect(headers.get('Authorization')).toBe(`Bearer ${TOKEN}`);
    expect(headers.get('Accept')).toBe('text/event-stream');
  });
});
