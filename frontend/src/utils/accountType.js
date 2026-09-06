export function getAccountTypeBadgeClass(accountType) {
  const direct = String(accountType || '').trim().toLowerCase();
  return direct ? direct.replaceAll(' ', '_') : 'other';
}
