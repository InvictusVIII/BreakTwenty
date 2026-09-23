// Canadian-primary account types (+ common US). Category + asset/liability sign are
// derived from these on the backend/Accounts page, so manual forms collect only the type.
export const BANKING_ACCOUNT_TYPES = [
  { value: 'cash', label: 'Cash' },
  { value: 'chequing', label: 'Chequing' },
  { value: 'savings', label: 'Savings' },
  { value: 'tfsa', label: 'TFSA' },
  { value: 'rrsp', label: 'RRSP' },
  { value: 'resp', label: 'RESP' },
  { value: 'rdsp', label: 'RDSP' },
  { value: 'fhsa', label: 'FHSA' },
  { value: 'lira', label: 'LIRA' },
  { value: 'lrsp', label: 'LRSP' },
  { value: 'rrif', label: 'RRIF' },
  { value: 'lif', label: 'LIF' },
  { value: 'lrif', label: 'LRIF' },
  { value: 'prif', label: 'PRIF' },
  { value: 'rpp', label: 'RPP' },
  { value: 'dpsp', label: 'DPSP' },
  { value: 'spp', label: 'SPP' },
  { value: 'margin', label: 'Margin' },
  { value: 'nreg', label: 'Non-Registered' },
  { value: '401k', label: '401(k)' },
  { value: 'ira', label: 'IRA' },
  { value: 'roth_ira', label: 'Roth IRA' },
  { value: 'credit_card', label: 'Credit Card' },
  { value: 'line_of_credit', label: 'Line of Credit' },
  { value: 'heloc', label: 'HELOC' },
  { value: 'loan', label: 'Loan' },
  { value: 'mortgage', label: 'Mortgage' },
  { value: 'student_loan', label: 'Student Loan' },
  { value: 'auto_loan', label: 'Auto Loan' },
];

export const CRYPTO_ACCOUNT_TYPES = [
  { value: 'crypto', label: 'Crypto' },
  { value: 'cash', label: 'Cash' },
];

const MANUAL_INSTITUTION_ICON_ACCEPT_TYPES = new Set(['image/png', 'image/svg+xml', 'image/jpeg', 'image/webp']);

export const MANUAL_INSTITUTION_ICON_HINT = 'Use a square PNG, SVG, JPEG, or WebP under 1 MB.';

export function validateManualInstitutionIconFile(file) {
  if (!file) return '';
  if (!MANUAL_INSTITUTION_ICON_ACCEPT_TYPES.has(file.type)) return 'Icon must be a PNG, SVG, JPEG, or WebP image.';
  if (file.size > 1024 * 1024) return 'Icon too large (max 1 MB).';
  return '';
}
