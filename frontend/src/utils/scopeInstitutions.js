import { readJsonResponse } from './apiResponse';

export async function fetchScopeInstitutions({ apiBase, fetchImpl = fetch, signal } = {}) {
  const institutionsResponse = await fetchImpl(`${apiBase}/institutions/scope`, { signal });
  const institutions = await readJsonResponse(institutionsResponse, {
    label: 'Scope institutions',
    validate: (value) => Array.isArray(value) && value.every((institution) => (
      institution
      && typeof institution === 'object'
      && Array.isArray(institution.accounts)
      && Array.isArray(institution.accountIds)
    )),
  });

  return institutions.map((institution) => ({
    ...institution,
    key: institution.key || `institution-${institution.id}`,
    accounts: institution.accounts.map((account) => ({
      ...account,
      institution: account.institution || institution.name,
      institution_id: account.institution_id ?? institution.id,
      provider: account.provider || institution.provider,
    })),
    accountIds: institution.accountIds.length
      ? institution.accountIds
      : institution.accounts.map((account) => account.id),
  }));
}
