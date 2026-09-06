import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

import { downloadAllData, downloadCsv } from './exportCsv';

describe('data export downloads', () => {
  let downloads;

  beforeEach(() => {
    downloads = [];
    vi.stubGlobal('fetch', vi.fn());
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:data-export'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click() {
      downloads.push(this.download);
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('downloads all four datasets through the archive endpoint', async () => {
    const blob = new Blob(['archive']);
    fetch.mockResolvedValue({
      ok: true,
      headers: new Headers({
        'Content-Disposition': 'attachment; filename="breaktwenty-data-export-2026-08-21.zip"',
      }),
      blob: vi.fn().mockResolvedValue(blob),
    });

    await downloadAllData();

    expect(fetch).toHaveBeenCalledWith(expect.stringMatching(/\/export\/all\.zip$/));
    expect(downloads).toEqual(['breaktwenty-data-export-2026-08-21.zip']);
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:data-export');
  });

  it('preserves individual CSV query parameters and sanitizes filenames', async () => {
    fetch.mockResolvedValue({
      ok: true,
      headers: new Headers({
        'Content-Disposition': 'attachment; filename="../../unsafe\\transactions.csv"',
      }),
      blob: vi.fn().mockResolvedValue(new Blob(['csv'])),
    });

    await downloadCsv('transactions', {
      include_hidden: 'true',
      include_internal_ids: 'true',
    });

    expect(fetch).toHaveBeenCalledWith(expect.stringMatching(
      /\/export\/transactions\.csv\?include_hidden=true&include_internal_ids=true$/,
    ));
    expect(downloads).toEqual(['-..-unsafe-transactions.csv']);
  });

  it('rejects a failed archive response without presenting a download', async () => {
    fetch.mockResolvedValue({
      ok: false,
      status: 500,
      headers: new Headers(),
    });

    await expect(downloadAllData()).rejects.toThrow('Export failed (500)');
    expect(downloads).toEqual([]);
  });
});
