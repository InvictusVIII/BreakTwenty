const ISOLATED_JOURNEY_VIEWPORTS = Object.freeze([
  Object.freeze({ name: 'canonical', width: 1440, height: 900 }),
  Object.freeze({ name: 'compact', width: 1100, height: 760 }),
]);

const ISOLATED_JOURNEY_ROUTES = Object.freeze([
  Object.freeze({
    name: 'dashboard',
    path: '/',
    selector: '.dashboard-page-content',
    populatedSelector: '.dashboard-layout .panel-shell',
    loadingSelector: '.app-route-loading',
    visual: true,
  }),
  Object.freeze({
    name: 'accounts',
    path: '/accounts',
    selector: '.accounts-section-layout',
    populatedSelector: '.accounts-group-title',
    loadingSelector: '.app-route-loading',
    visual: true,
  }),
  Object.freeze({
    name: 'investments',
    path: '/holdings',
    selector: '.investments-hub-shell',
    populatedSelector: '.holdings-summary-grid',
    loadingSelector: '.app-route-loading',
    visual: true,
  }),
  Object.freeze({
    name: 'transactions',
    path: '/transactions',
    selector: '.transactions-page',
    populatedSelector: '.transactions-row',
    loadingSelector: '.transactions-loading',
    visual: true,
  }),
  Object.freeze({
    name: 'cash-flow',
    path: '/cash-flow',
    selector: '.cash-flow-page',
    populatedSelector: '.cf-headline-row',
    loadingSelector: '.cf-loading',
    visual: true,
  }),
  Object.freeze({
    name: 'settings',
    path: '/settings',
    selector: '.settings-grid',
    populatedSelector: '.settings-card',
    loadingSelector: '.app-route-loading',
    visual: true,
  }),
  Object.freeze({
    name: 'category-settings',
    path: '/settings/categories',
    selector: '.categories-settings',
    populatedSelector: '.cs-groups',
    loadingSelector: '.cs-loading',
  }),
  Object.freeze({
    name: 'help',
    path: '/faq',
    selector: '.faq-page',
    populatedSelector: '.faq-item',
    loadingSelector: '.app-route-loading',
  }),
  Object.freeze({
    name: 'licenses',
    path: '/licenses',
    selector: '.licenses-page',
    populatedSelector: '.licenses-content-card',
    loadingSelector: '.app-route-loading',
  }),
]);

module.exports = {
  ISOLATED_JOURNEY_ROUTES,
  ISOLATED_JOURNEY_VIEWPORTS,
};
