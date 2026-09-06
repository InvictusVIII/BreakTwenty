import { API } from '../config';
import { revealSupportArchive } from './desktopBridge';

function responseFilename(response, fallback) {
  const disposition = response.headers.get('Content-Disposition') || '';
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  const candidate = String(match?.[1] || fallback || 'breaktwenty-support.zip').trim();
  const safeName = candidate
    .replace(/[\\/\0\r\n]/g, '-')
    .replace(/^\.+/, '')
    .slice(0, 180);
  return safeName || 'breaktwenty-support.zip';
}

export async function presentSupportArchive(archiveId, archiveFilename) {
  const normalizedId = String(archiveId || '').trim().toLowerCase();
  if (!normalizedId) {
    throw new Error('Support archive is unavailable.');
  }

  const revealResult = await revealSupportArchive(normalizedId);
  if (revealResult?.status === 'ok') {
    return { status: 'ok', presentation: 'folder' };
  }

  const response = await fetch(
    `${API}/settings/support-logs/archives/${encodeURIComponent(normalizedId)}`,
  );
  if (!response.ok) {
    throw new Error('Support archive could not be downloaded.');
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = responseFilename(response, archiveFilename);
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
  }
  return { status: 'ok', presentation: 'download' };
}
