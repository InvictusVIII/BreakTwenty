import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

const { revealSupportArchive } = vi.hoisted(() => ({
  revealSupportArchive: vi.fn(),
}));

vi.mock('./desktopBridge', () => ({ revealSupportArchive }));

import { presentSupportArchive } from './supportArchive';

describe('support archive presentation', () => {
  beforeEach(() => {
    revealSupportArchive.mockReset();
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('uses the opaque desktop reveal bridge without exposing a path', async () => {
    revealSupportArchive.mockResolvedValue({ status: 'ok' });

    await expect(presentSupportArchive('archive-id', 'support.zip')).resolves.toEqual({
      status: 'ok',
      presentation: 'folder',
    });

    expect(revealSupportArchive).toHaveBeenCalledWith('archive-id');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('downloads through the authenticated archive endpoint when no desktop bridge is available', async () => {
    revealSupportArchive.mockResolvedValue({ status: 'unavailable' });
    const blob = new Blob(['support']);
    fetch.mockResolvedValue({
      ok: true,
      headers: new Headers({ 'Content-Disposition': 'attachment; filename="safe-support.zip"' }),
      blob: vi.fn().mockResolvedValue(blob),
    });
    const createObjectURL = vi.fn(() => 'blob:support');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    await expect(presentSupportArchive('archive-id', 'fallback.zip')).resolves.toEqual({
      status: 'ok',
      presentation: 'download',
    });

    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/settings/support-logs/archives/archive-id'));
    expect(click).toHaveBeenCalledOnce();
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:support');
  });

  it('sanitizes a server-supplied download filename before assigning it to an anchor', async () => {
    revealSupportArchive.mockResolvedValue({ status: 'unavailable' });
    fetch.mockResolvedValue({
      ok: true,
      headers: new Headers({
        'Content-Disposition': 'attachment; filename="../../unsafe\\name.zip"',
      }),
      blob: vi.fn().mockResolvedValue(new Blob(['support'])),
    });
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:support') });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    const downloads = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click() {
      downloads.push(this.download);
    });

    await presentSupportArchive('archive-id', 'fallback.zip');

    expect(downloads).toEqual(['-..-unsafe-name.zip']);
  });
});
