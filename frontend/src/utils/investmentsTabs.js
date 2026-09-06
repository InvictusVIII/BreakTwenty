export function shouldResetInvestmentsTab(activeTab, tabVisibility, holdingsReady) {
  return holdingsReady && tabVisibility[activeTab] === false;
}

export function shouldShowInvestmentsTab(tabId, activeTab, tabVisibility, holdingsReady) {
  return tabVisibility[tabId] !== false || (!holdingsReady && tabId === activeTab);
}
