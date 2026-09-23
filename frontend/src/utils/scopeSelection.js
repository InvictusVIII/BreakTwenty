function formatScopeCount(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

export function formatScopeSelectionSummary({
  totalInstitutions,
  selectedInstitutions,
  selectedAccounts,
}) {
  if (selectedAccounts === 0) {
    return 'No sources selected';
  }

  const accountLabel = formatScopeCount(selectedAccounts, 'Account');

  if (selectedInstitutions === totalInstitutions) {
    return `${formatScopeCount(totalInstitutions, 'Institution')} · ${accountLabel}`;
  }

  return `${selectedInstitutions} of ${totalInstitutions} institutions · ${accountLabel}`;
}
