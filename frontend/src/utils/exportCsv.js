import { API } from '../config';
import { APP_BRAND_SLUG } from '../constants/brand';

function responseFilename(response, fallback) {
  const disposition = response.headers.get('Content-Disposition') || '';
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  const candidate = String(match?.[1] || fallback || `${APP_BRAND_SLUG}-export`).trim();
  const safeName = candidate
    .replace(/[\\/\0\r\n]/g, '-')
    .replace(/^\.+/, '')
    .slice(0, 180);
  return safeName || `${APP_BRAND_SLUG}-export`;
}

async function downloadExport(url, fallbackFilename) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Export failed (${response.status})`);
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = responseFilename(response, fallbackFilename);
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
  }
}

// Download an app CSV export. `params` may be a URLSearchParams or a plain
// object of query params (e.g. transaction filters, include_hidden). Fetches the
// blob and triggers a browser/Electron save so it works cross-origin in dev.
export async function downloadCsv(dataset, params) {
  const search = params instanceof URLSearchParams
    ? params
    : new URLSearchParams(params || {});
  const query = search.toString();
  await downloadExport(
    `${API}/export/${dataset}.csv${query ? `?${query}` : ''}`,
    `${APP_BRAND_SLUG}-${dataset}.csv`,
  );
}

export async function downloadAllData() {
  await downloadExport(
    `${API}/export/all.zip`,
    `${APP_BRAND_SLUG}-data-export.zip`,
  );
}
