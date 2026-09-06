import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const parser = require('@babel/parser');
const traverse = require('@babel/traverse').default;
const sourceRoot = path.dirname(fileURLToPath(import.meta.url));
const TEST_MODULE_PATTERN = /\.(?:test|spec)\.[cm]?[jt]sx?$/;

const PANEL_NATIVE_BUTTON_CLASSES = [
  'accounts-detail-account-row',
  'accounts-detail-transactions-viewall',
  'accounts-parent-leading-chevron',
  'add-networth-back',
  'app-detail-drawer-close',
  'app-instructions',
  'category-picker-manage',
  'cf-detail-viewall',
  'cf-panel-header',
  'cf-recurring-action',
  'cs-form-icon-current',
  'cs-row-action',
  'dashboard-widget-move-btn',
  'edit-name-btn',
  'edit-name-cancel',
  'edit-name-save',
  'faq-question-trigger',
  'holdings-section-toggle',
  'income-detail-group-toggle',
  'inst-sync-btn',
  'investments-subnav-tab',
  'manual-inst-add',
  'manual-inst-logo-remove',
  'modal-close',
  'modal-secret-toggle',
  'provider-sync-status-action',
  'settings-account-add-btn',
  'settings-asset-delete',
  'settings-asset-icon-btn',
  'sortable-table-header-button',
  'transactions-search-inline-clear',
  'tx-detail-icon-button',
  'welcome-skip',
];

const COMPLEX_SURFACED_BUTTON_CLASSES = [
  'add-networth-tile',
  'category-picker-item',
  'chart-color-square',
  'dashboard-customize-arrange-toggle',
  'dashboard-widget-visibility-switch',
  'emoji-picker-cell',
  'floating-nav-btn',
  'floating-nav-toggle',
  'holdings-pnl-toggle',
  'income-timeline-menu-item',
  'institution-account-selector-account-row',
  'institution-account-selector-tile',
  'market-strip-option',
  'support-logs-dev-toggle-switch',
  'support-logs-picker-option',
  'timeline-range-picker-cell',
  'timeline-range-picker-day',
  'timeline-range-picker-summary-item',
  'transaction-import-rail-toggle',
  'transactions-toolbar-menu-item',
];

function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(entryPath);
    return /\.(?:js|jsx)$/.test(entry.name) && !TEST_MODULE_PATTERN.test(entry.name) ? [entryPath] : [];
  });
}

function styleFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return styleFiles(entryPath);
    return entry.name.endsWith('.css') ? [entryPath] : [];
  });
}

function ruleBodiesForClass(source, className) {
  const uncommented = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const rulePattern = /([^{}]+)\{([^{}]*)\}/g;
  const bodies = [];
  let match;

  while ((match = rulePattern.exec(uncommented)) !== null) {
    if (match[1].includes(`.${className}`)) {
      bodies.push(match[2]);
    }
  }

  return bodies;
}

function classSource(attribute, source) {
  return attribute?.value ? source.slice(attribute.value.start, attribute.value.end) : '';
}

function openingClassSource(openingElement, source) {
  const attribute = openingElement.attributes.find(
    (candidate) => candidate.type === 'JSXAttribute' && candidate.name.name === 'className'
  );
  return classSource(attribute, source);
}

function hasClass(classSourceText, className) {
  return new RegExp(`(?:^|[\\s'"\\\`])${className}(?:[\\s'"\\\`])`).test(classSourceText);
}

let jsxElementInventory = null;

function getJsxElementInventory() {
  if (jsxElementInventory) return jsxElementInventory;

  jsxElementInventory = [];
  for (const filePath of sourceFiles(sourceRoot)) {
    const source = fs.readFileSync(filePath, 'utf8');
    const ast = parser.parse(source, { sourceType: 'module', plugins: ['jsx'] });

    traverse(ast, {
      JSXElement(nodePath) {
        jsxElementInventory.push({
          filePath,
          node: nodePath.node,
          openingElement: nodePath.node.openingElement,
          source,
        });
      },
    });
  }
  return jsxElementInventory;
}

function inspectJsxElements(callback) {
  getJsxElementInventory().forEach(callback);
}

function inspectButtons(callback) {
  inspectJsxElements(({ filePath, node, openingElement, source }) => {
    if (openingElement.name.type !== 'JSXIdentifier' || openingElement.name.name !== 'button') return;
    callback({ filePath, node, openingElement, source });
  });
}

test('standard surfaced CTA buttons use the shared direct-slot contract', () => {
  const violations = [];

  inspectButtons(({ filePath, node, openingElement, source }) => {
    const classes = openingClassSource(openingElement, source);
    if (!/\bbtn-(?:primary|secondary|danger)\b/.test(classes)) return;

    if (!/\bapp-control-root\b/.test(classes)) {
      violations.push(`${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line} missing app-control-root`);
      return;
    }

    const contentSource = source.slice(openingElement.end, node.closingElement.start);
    if (!/\bapp-control-label\b/.test(contentSource)) {
      violations.push(`${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line} missing app-control-label`);
    }
  });

  expect(violations).toEqual([]);
});

test('shared control roots contain only direct label, icon, and chevron slots', () => {
  const violations = [];
  let rootCount = 0;

  inspectButtons(({ filePath, node, openingElement, source }) => {
    const classes = openingClassSource(openingElement, source);
    if (!/\bapp-control-root\b/.test(classes)) return;
    rootCount += 1;

    for (const child of node.children) {
      if (child.type === 'JSXText' && !child.value.trim()) continue;
      if (child.type === 'JSXExpressionContainer' && child.expression.type === 'JSXEmptyExpression') continue;

      if (child.type === 'JSXElement') {
        const childClasses = openingClassSource(child.openingElement, source);
        if (/\bapp-control-(?:label|icon|chevron)\b/.test(childClasses)) continue;
      }

      if (
        child.type === 'JSXExpressionContainer'
        && /\bapp-control-(?:label|icon|chevron)\b/.test(source.slice(child.start, child.end))
      ) {
        continue;
      }

      violations.push(
        `${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line} has non-slot direct content`
      );
    }
  });

  expect(rootCount).toBeGreaterThan(100);
  expect(violations).toEqual([]);
});

test('surfaced control roots do not render empty icon or chevron slots', () => {
  const violations = [];

  inspectButtons(({ filePath, node, openingElement, source }) => {
    const classes = openingClassSource(openingElement, source);
    if (!/\bapp-control-root\b/.test(classes)) return;

    for (const child of node.children) {
      if (child.type !== 'JSXElement') continue;
      const childClasses = openingClassSource(child.openingElement, source);
      if (!/\bapp-control-(?:icon|chevron)\b/.test(childClasses)) continue;
      const contentSource = source.slice(child.openingElement.end, child.closingElement?.start || child.end).trim();
      if (!contentSource) {
        violations.push(`${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line} empty ${childClasses}`);
      }
    }
  });

  expect(violations).toEqual([]);
});

test('account type badges render through one shared component and font-metric trim', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const addTransactionCss = fs.readFileSync(
    path.join(sourceRoot, 'components', 'AddTransactionModal.css'),
    'utf8'
  );
  const violations = [];
  let badgeCount = 0;

  inspectJsxElements(({ filePath, node, openingElement, source }) => {
    const classes = openingClassSource(openingElement, source);
    if (!hasClass(classes, 'account-type')) return;
    badgeCount += 1;

    const relativePath = path.relative(sourceRoot, filePath);
    if (relativePath !== path.join('components', 'AccountTypeBadge.js')) {
      violations.push(`${relativePath}:${openingElement.loc.start.line} renders raw account-type badge`);
      return;
    }

    const hasLabelSlot = node.children.some((child) => (
      child.type === 'JSXElement'
      && hasClass(openingClassSource(child.openingElement, source), 'account-type-label')
    ));

    if (!hasLabelSlot) {
      violations.push(`${relativePath}:${openingElement.loc.start.line} missing account-type-label`);
    }
  });

  expect(badgeCount).toBe(1);
  expect(violations).toEqual([]);
  expect(appCss).not.toContain('--account-type-badge-label-visual-offset-block');
  expect(appCss).toMatch(/--account-type-badge-bg-accent-mix:\s*0%;/);
  expect(appCss).toMatch(/--account-type-badge-color-accent-mix:\s*100%;/);
  expect(appCss).toMatch(/--account-type-badge-border-accent-mix:\s*100%;/);
  expect(appCss).toMatch(
    /\.account-type,[\s\S]*?\.settings-value-history-tag\s*\{[^}]*--account-type-badge-bg:\s*color-mix\([^}]*var\(--account-type-badge-accent\) var\(--account-type-badge-bg-accent-mix\)[^}]*--account-type-badge-color:\s*color-mix\([^}]*var\(--account-type-badge-accent\) var\(--account-type-badge-color-accent-mix\)[^}]*--account-type-badge-border-color:\s*color-mix\([^}]*var\(--account-type-badge-accent\) var\(--account-type-badge-border-accent-mix\)/
  );
  expect(appCss).toMatch(
    /\.account-type\s*\{[^}]*background:\s*var\(--account-type-badge-bg\);[^}]*color:\s*var\(--account-type-badge-color\);[^}]*border:\s*var\(--account-type-badge-border-width\) solid var\(--account-type-badge-border-color\);/
  );
  expect(appCss).toMatch(
    /\.account-type\.is-chequing,[\s\S]*?\.account-type\.is-cash\s*\{[^}]*--account-type-badge-accent:\s*var\(--account-type-banking\);/
  );
  expect(appCss).toMatch(
    /\.account-type-label\s*\{[^}]*display:\s*block;[^}]*line-height:\s*inherit;[^}]*text-box:\s*trim-both cap alphabetic;/
  );
  expect(addTransactionCss).toMatch(
    /\.add-txn-import-account \.app-dropdown-trigger > \.app-dropdown-value,\s*\.add-txn-import-account-dropdown-popover \.app-dropdown-option > \.app-control-label\s*\{[^}]*overflow:\s*visible;/
  );
});

test('BreakTwenty-owned institution logo stages use tokenized art filters', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const runtimeSource = fs.readFileSync(path.join(sourceRoot, 'theme', 'runtimeTokens.js'), 'utf8');
  const logoSource = fs.readFileSync(path.join(sourceRoot, 'components', 'InstitutionLogo.js'), 'utf8');

  expect(logoSource).toContain('STAGED_LOGO_ASSETS');
  expect(logoSource).toContain('stageKey = null');
  expect(logoSource).toContain('--institution-logo-filter-saturation');
  expect(appCss).toMatch(/--institution-logo-filter-default-saturation:\s*1;/);
  expect(appCss).toMatch(
    /\.institution-logo-stage\s*\{[^}]*--institution-logo-filter-saturation:\s*var\(--institution-logo-filter-default-saturation\);/
  );
  expect(appCss).toMatch(/\.institution-logo-stage\s*\{[^}]*overflow:\s*visible;/);
  expect(appCss).toMatch(
    /\.institution-logo-stage-img,[\s\S]*?\.institution-logo-stage > svg\s*\{[^}]*filter:\s*saturate\(var\(--institution-logo-filter-saturation\)\)\s*contrast\(var\(--institution-logo-filter-contrast\)\)\s*brightness\(var\(--institution-logo-filter-brightness\)\);/
  );
  expect(runtimeSource).toMatch(/'--institution-logo-cash-saturation':\s*'[\d.]+'.*/);
  expect(runtimeSource).toMatch(/'--institution-logo-cash-contrast':\s*'[\d.]+'.*/);
  expect(runtimeSource).toMatch(/'--institution-logo-cash-brightness':\s*'[\d.]+'.*/);
  expect(runtimeSource).toMatch(
    /light:\s*Object\.freeze\([\s\S]*'--institution-logo-stage-padding': '0px'/
  );
  expect(appCss).toMatch(/--institution-logo-art-radius:\s*6px;/);
  expect(appCss).toMatch(
    /\.institution-logo-art\s*\{[^}]*border-radius:\s*var\(--institution-logo-art-radius\);[^}]*flex-shrink:\s*0;/
  );
  expect(appCss).toMatch(
    /\.institution-logo-fallback\s*\{[^}]*border-radius:\s*var\(--institution-logo-art-radius\);[^}]*font-size:\s*var\(--institution-logo-fallback-font-size\);/
  );
  expect(logoSource).not.toMatch(
    /\b(?:background|borderRadius|color|flexShrink|fontFamily|fontSize|fontWeight|objectFit)\s*:/
  );
});

test('institution source tiles use the shared hover-only tooltip contract', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const selectorSource = fs.readFileSync(
    path.join(sourceRoot, 'components', 'InstitutionAccountSelector.js'),
    'utf8'
  );

  expect(selectorSource).toContain('data-tooltip={institution.name}');
  expect(selectorSource).toContain('data-tooltip-hover-only');
  expect(selectorSource).not.toContain('createPortal');
  expect(selectorSource).not.toContain('showTileTooltip');
  expect(appCss).not.toContain('.institution-account-selector-tile-tooltip');
});

test('sidebar PNG masks share one CSS recipe with only the asset supplied at runtime', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const appSource = fs.readFileSync(path.join(sourceRoot, 'App.js'), 'utf8');

  expect(appSource).toContain('function PngMaskSidebarIcon');
  expect(appSource).toContain("'--floating-nav-icon-mask-image': `url(${asset})`");
  expect(appSource).not.toMatch(/\b(?:WebkitMaskImage|maskImage|WebkitMaskRepeat|maskRepeat)\s*:/);
  expect(appCss).toMatch(
    /\.floating-nav-btn-icon > \.floating-nav-icon-mask\s*\{[^}]*display:\s*inline-block;[^}]*background-color:\s*currentColor;[^}]*-webkit-mask-image:\s*var\(--floating-nav-icon-mask-image\);[^}]*mask-image:\s*var\(--floating-nav-icon-mask-image\);/
  );
});

test('panel-native actions and instruction disclosures remain outside the surfaced-control slot migration', () => {
  const violations = [];

  inspectButtons(({ filePath, openingElement, source }) => {
    const classes = openingClassSource(openingElement, source);
    if (!PANEL_NATIVE_BUTTON_CLASSES.some((className) => classes.includes(className))) return;
    if (!classes.includes('app-control-root')) return;
    violations.push(`${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line}`);
  });

  expect(violations).toEqual([]);
});

test('instruction disclosures use their dedicated direct-slot contract', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const violations = [];
  let disclosureCount = 0;

  inspectButtons(({ filePath, node, openingElement, source }) => {
    const classes = openingClassSource(openingElement, source);
    if (!classes.includes('app-instructions')) return;
    disclosureCount += 1;

    if (classes.includes('app-control-root')) {
      violations.push(`${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line} uses app-control-root`);
    }

    const directChildClasses = node.children.flatMap((child) => {
      if (child.type !== 'JSXElement') return [];
      return [openingClassSource(child.openingElement, source)];
    });
    if (!directChildClasses.some((childClasses) => childClasses.includes('app-instructions-label'))) {
      violations.push(`${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line} missing app-instructions-label`);
    }
    if (!directChildClasses.some((childClasses) => childClasses.includes('app-instructions-chevron'))) {
      violations.push(`${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line} missing app-instructions-chevron`);
    }
  });

  expect(disclosureCount).toBe(4);
  expect(violations).toEqual([]);
  expect(appCss).not.toContain('--app-instructions-inner-top-radius');
  expect(appCss).toMatch(
    /\.settings-instructions-dropdown\s*\{[^}]*position:\s*relative;/
  );
  expect(appCss).toMatch(
    /\.settings-instructions-dropdown(?::not\([^)]*\))?:has\(> \.app-instructions\.is-open\)::after\s*\{[^}]*content:\s*"";[^}]*position:\s*absolute;[^}]*inset:\s*0;[^}]*border:\s*var\(--button-state-shell-selected-border-width\) solid var\(--surface-button-selected-border-color\);[^}]*pointer-events:\s*none;/
  );
  expect(appCss).toMatch(
    /\.settings-instructions-dropdown(?::not\([^)]*\))?:has\(> \.app-instructions\.is-open\)\s*\{[^}]*overflow:\s*hidden;/
  );
  expect(appCss).toMatch(
    /\.settings-instructions-dropdown > \.app-instructions\.is-open(?::not\([^)]*\))?\s*\{[^}]*--button-state-physical-selected-border-color:\s*var\(--surface-button-selected-border-color\);[^}]*border:\s*0;[^}]*border-radius:\s*var\(--panel-control-radius\)\s*var\(--panel-control-radius\)\s*0\s*0;/
  );
  expect(appCss).toMatch(
    /\.app-instructions\.is-open(?::not\([^)]*\))?::after\s*\{[^}]*border:\s*0;[^}]*box-shadow:\s*none;/
  );
  expect(appCss).toMatch(
    /\.settings-instructions-dropdown > \.app-instructions\.is-open(?::not\([^)]*\))? \+ \.settings-instructions\s*\{[^}]*width:\s*100%;[^}]*box-sizing:\s*border-box;[^}]*border:\s*0;[^}]*border-radius:\s*0\s*0\s*var\(--surface-text-field-radius\)\s*var\(--surface-text-field-radius\);/
  );
});

test('every native button is explicitly classified as slotted, complex surfaced, or panel-native', () => {
  const violations = [];

  inspectButtons(({ filePath, openingElement, source }) => {
    const classes = openingClassSource(openingElement, source);
    if (classes.includes('app-control-root')) return;
    if (COMPLEX_SURFACED_BUTTON_CLASSES.some((className) => classes.includes(className))) return;
    if (PANEL_NATIVE_BUTTON_CLASSES.some((className) => classes.includes(className))) return;
    violations.push(
      `${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line} unclassified ${classes || '(no className)'}`
    );
  });

  expect(violations).toEqual([]);
});

test('zoom-stable controls use font-metric labels and static SVG slots', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const dashboardCss = fs.readFileSync(path.join(sourceRoot, 'pages', 'Dashboard.css'), 'utf8');
  const dashboardSource = fs.readFileSync(path.join(sourceRoot, 'pages', 'Dashboard.js'), 'utf8');
  const controlCss = fs.readFileSync(path.join(sourceRoot, 'controlSlots.css'), 'utf8');
  const indexCss = fs.readFileSync(path.join(sourceRoot, 'index.css'), 'utf8');
  const appJs = fs.readFileSync(path.join(sourceRoot, 'App.js'), 'utf8');
  const triangleSource = fs.readFileSync(
    path.join(sourceRoot, 'components', 'TriangleIcon.js'),
    'utf8'
  );
  const accountsIconSource = fs.readFileSync(
    path.join(sourceRoot, 'assets', 'icons', 'accounts-icon.svg'),
    'utf8'
  );
  const typographySource = fs.readFileSync(
    path.join(sourceRoot, 'theme', 'typography.js'),
    'utf8'
  );
  const allCss = styleFiles(sourceRoot)
    .map((filePath) => fs.readFileSync(filePath, 'utf8'))
    .join('\n');

  expect(allCss).not.toMatch(/--control-glyph-center-offset/);
  expect(allCss).not.toMatch(/--(?:control|top-control|panel-control|floating-nav)-[a-z-]*optical-offset/);
  expect(allCss).not.toMatch(/--overview-metric-[a-z-]*nudge/);
  expect(controlCss).not.toContain('--control-label-raster-offset-block');
  expect(appCss).not.toContain('--control-label-visual-offset-block');
  expect(controlCss).toMatch(
    /\.app-control-root\s*\{[^}]*display:\s*inline-grid !important;[^}]*grid-template-columns:\s*none;[^}]*grid-template-rows:\s*auto;[^}]*grid-auto-flow:\s*column;[^}]*grid-auto-columns:\s*minmax\(0,\s*max-content\);[^}]*align-content:\s*center !important;[^}]*align-items:\s*center !important;[^}]*justify-content:\s*center !important;/
  );
  expect(controlCss).toMatch(
    /\.app-control-root\s*\{[^}]*height:\s*var\(--control-height\);[^}]*min-height:\s*var\(--control-height\);[^}]*max-height:\s*var\(--control-height\);[^}]*padding-block:\s*var\(--space-0\) !important;[^}]*padding-inline:\s*var\(--control-object-edge-inline-current\) !important;/
  );
  expect(appCss).toContain('--control-object-gap:');
  expect(appCss).toContain('--control-object-edge-label-inline:');
  expect(appCss).toContain('--control-object-edge-icon-inline:');
  expect(controlCss).toContain('--control-object-edge-inline-current: var(--control-object-edge-label-inline);');
  expect(controlCss).toContain('--control-object-gap-current: var(--control-object-gap);');
  expect(controlCss).toMatch(
    /\.app-control-root:has\(> \.app-control-label\):not\(:has\(> \.app-control-icon\)\):not\(:has\(> \.app-control-chevron\)\)\s*\{[^}]*--control-object-edge-inline-current:\s*var\(--control-object-edge-label-inline\);[^}]*--control-object-gap-current:\s*var\(--space-0\);/
  );
  expect(controlCss).toMatch(
    /\.app-control-root:has\(> \.app-control-icon\):has\(> \.app-control-label\),\s*\.app-control-root:has\(> \.app-control-icon\):has\(> \.app-control-chevron\)\s*\{[^}]*--control-object-edge-inline-current:\s*var\(--control-object-edge-icon-inline\);/
  );
  expect(controlCss).toMatch(
    /\.app-control-root:has\(> :is\(\.app-control-icon, \.app-control-label, \.app-control-chevron\):only-child\)\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\);[^}]*justify-content:\s*stretch !important;[^}]*gap:\s*0;/
  );
  expect(controlCss).not.toContain('--control-side-slot-size');
  expect(controlCss).not.toContain('--control-label-ink-offset-inline');
  expect(controlCss).not.toContain('--control-label-ink-offset-block');
  expect(controlCss).not.toMatch(/inset-(?:block|inline)-start:\s*var\(--control/);
  expect(controlCss).not.toContain('padding: var(--space-0) var(--control-padding-inline);');
  expect(controlCss).not.toContain('padding: var(--space-0) var(--control-compact-padding-inline);');
  expect(controlCss).toMatch(
    /\.page-shell-toolbar \.app-control-root,[^{]*\{[^}]*height:\s*var\(--control-height\);[^}]*min-height:\s*var\(--control-height\);[^}]*max-height:\s*var\(--control-height\);[^}]*padding:\s*var\(--space-0\) var\(--control-object-edge-inline-current\);/
  );
  expect(controlCss).toMatch(
    /\.panel-shell \.app-control-root:is\([^}]*height:\s*var\(--control-height\);[^}]*min-height:\s*var\(--control-height\);[^}]*max-height:\s*var\(--control-height\);[^}]*padding:\s*var\(--space-0\) var\(--control-object-edge-inline-current\);/
  );
  expect(controlCss).toMatch(
    /\.app-control-root\[aria-expanded="true"\] > \.app-control-chevron > svg\s*\{[^}]*transform:\s*rotate\(180deg\);/
  );
  expect(controlCss).not.toContain('var(--app-device-pixel)');
  expect(controlCss).not.toContain('round(');
  expect(controlCss).toContain('width: var(--control-icon-size);');
  expect(controlCss).toContain('width: var(--control-chevron-size);');
  expect(controlCss).toMatch(
    /\.app-control-root > :is\(\.app-control-icon, \.app-control-label, \.app-control-chevron\)\s*\{[^}]*display:\s*grid;[^}]*place-items:\s*center;[^}]*align-self:\s*center;[^}]*justify-self:\s*center;/
  );
  expect(controlCss).toMatch(
    /\.app-control-root > \.app-control-label\s*\{[^}]*display:\s*block;[^}]*font-family:\s*var\(--control-label-family\);[^}]*font-size:\s*var\(--control-label-size\);[^}]*line-height:\s*var\(--control-label-line-height\);[^}]*text-box:\s*trim-both cap alphabetic;[^}]*overflow:\s*clip;[^}]*overflow-clip-margin:\s*var\(--control-label-ink-overflow\);/
  );
  const controlLabelRule = controlCss.match(
    /\.app-control-root > \.app-control-label\s*\{(?<body>[^}]*)\}/
  )?.groups?.body;
  expect(controlLabelRule).toBeTruthy();
  expect(controlLabelRule).not.toMatch(/(?:^|\n)\s*(?:height|min-height|max-height)\s*:/);
  expect(controlLabelRule).not.toMatch(/(?:^|\n)\s*(?:padding|margin)(?:-[a-z]+)?\s*:/);
  expect(controlLabelRule).not.toMatch(/(?:^|\n)\s*(?:position|inset(?:-[a-z]+)?|translate)\s*:/);
  expect(controlLabelRule).not.toContain('transform:');
  expect(controlLabelRule).toContain('text-box: trim-both cap alphabetic;');
  expect(controlLabelRule).toContain('overflow: clip;');
  expect(controlLabelRule).toContain('overflow-clip-margin: var(--control-label-ink-overflow);');
  expect(controlCss).not.toContain('font: inherit;');
  expect(typographySource).toContain("'--type-button-label-size': 'var(--font-size-14)'");
  expect(typographySource).toContain(
    "'--type-sidebar-button-label-size': 'var(--font-size-14)'"
  );
  expect(typographySource).toContain(
    "'--type-sidebar-button-label-weight': 'var(--font-weight-semibold)'"
  );
  expect(typographySource).toContain(
    "'--type-topbar-trigger-label-size': 'var(--type-button-label-size)'"
  );
  expect(typographySource).toContain(
    "'--type-button-label-line-height': 'var(--line-height-normal)'"
  );
  expect(controlCss).toContain('--control-label-line-height: var(--type-button-label-line-height);');
  expect(appCss).toMatch(
    /\.floating-nav-label\s*\{[^}]*display:\s*grid;[^}]*place-items:\s*center start;[^}]*font-size:\s*var\(--type-sidebar-button-label-size\);[^}]*line-height:\s*var\(--type-sidebar-button-label-line-height\);/
  );
  const floatingNavLabelRule = appCss.match(/\.floating-nav-label\s*\{(?<body>[^}]*)\}/)
    ?.groups?.body;
  expect(floatingNavLabelRule).toBeTruthy();
  expect(floatingNavLabelRule).not.toMatch(
    /(?:^|\n)\s*(?:height|min-height|max-height|inset-block-start|transform|position)\s*:/
  );
  expect(appCss).not.toContain('--floating-nav-label-cap-align-padding-block-start');
  expect(appCss).toMatch(
    /\.floating-nav-label\s*\{[^}]*padding:\s*var\(--space-0\)\s*0\.3rem\s*var\(--space-0\)\s*0\.7rem;/
  );
  expect(appCss).toContain('--control-chevron-svg-size: 1.3rem;');
  expect(appCss).toContain('--top-control-chevron-svg-size: var(--control-chevron-svg-size);');
  expect(appCss).toContain('--panel-control-chevron-svg-size: var(--control-chevron-svg-size);');
  expect(appCss).toContain('--overview-metric-icon-size:');
  expect(appCss).toContain('--overview-metric-icon-svg-size:');
  expect(dashboardCss).toMatch(
    /\.overview-metric-icon\s*\{[^}]*width:\s*var\(--overview-metric-icon-size\);[^}]*height:\s*var\(--overview-metric-icon-size\);[^}]*display:\s*grid;[^}]*place-items:\s*center;/
  );
  expect(dashboardCss).toMatch(
    /\.overview-metric-icon svg\s*\{[^}]*display:\s*block;[^}]*width:\s*var\(--overview-metric-icon-svg-size\);[^}]*height:\s*var\(--overview-metric-icon-svg-size\);/
  );
  expect(dashboardCss).not.toMatch(/\.overview-metric-icon svg\s*\{[^}]*transform:/);
  expect(dashboardSource).not.toContain('<Icon size=');
  expect(controlCss).toContain('--control-chevron-size: var(--panel-control-chevron-svg-size);');
  expect(controlCss).not.toMatch(
    /\.settings-dropdown-trigger\.app-control-root\s*\{[^}]*--control-chevron-size:/
  );
  expect(appCss).toMatch(
    /\.floating-nav-toggle-icon\s*\{[^}]*width:\s*var\(--floating-nav-toggle-icon-size\);[^}]*height:\s*var\(--floating-nav-toggle-icon-size\);[^}]*display:\s*block;/
  );
  expect(appCss).toMatch(
    /\.floating-nav-btn-icon > \.floating-nav-icon-graphic\s*\{[^}]*width:\s*calc\(var\(--floating-nav-icon-size\) \* var\(--floating-nav-icon-scale\)\);[^}]*height:\s*calc\(var\(--floating-nav-icon-size\) \* var\(--floating-nav-icon-scale\)\);[^}]*display:\s*block;/
  );
  expect(appCss).toContain('--app-effective-device-scale: 1;');
  expect(appCss).toContain('--app-device-pixel: 1px;');
  expect(appCss).toMatch(
    /--button-state-rim-width-sm-effective:\s*max\([^;]*round\(nearest,\s*var\(--button-state-rim-width-sm\),\s*var\(--app-device-pixel\)\)/
  );
  expect(appCss).toMatch(
    /--active-border-visibility-shadow:\s*0 0 0 var\(--app-device-pixel\)/
  );
  expect(appCss).not.toContain('--app-theme-toggle-state-size-effective');
  expect(appCss).not.toContain('--app-theme-toggle-button-width-effective');
  expect(appCss).not.toContain('--app-theme-toggle-icon-size-effective');
  expect(appCss).toMatch(
    /\.app-theme-toggle-thumb\s*\{[^}]*inset-inline-start:\s*var\(--app-theme-toggle-thumb-x\);[^}]*transform:\s*none;/
  );
  expect(appCss).toMatch(
    /\.app-theme-toggle-thumb > svg\s*\{[^}]*width:\s*var\(--top-control-icon-button-svg-size\);[^}]*height:\s*var\(--top-control-icon-button-svg-size\);[^}]*display:\s*block;/
  );
  expect(appCss).toContain('.app-theme-mode-icon');
  expect(appJs).toContain("'--app-effective-device-scale'");
  expect(appJs).toContain("'--app-device-pixel'");
  expect(appJs).not.toContain('getBBox');
  expect(appJs).not.toContain('--control-glyph-center-offset');
  expect(appJs).not.toContain('scheduleControlSlotGeometryNormalization');
  expect(appJs).not.toContain('normalizeControlSlotGeometry');
  expect(appJs).not.toContain('setMeasuredControlOffset');
  expect(appJs).not.toContain('controlSlotObserver');
  expect(appJs).not.toContain('range.selectNodeContents(label);');
  expect(appJs).not.toContain('--control-label-raster-offset-block');
  expect(appJs).not.toContain('normalizeControlLabelTextCenter');
  expect(appJs).not.toContain('getControlLabelTextRect');
  expect(appJs).not.toContain('getControlLabelInkOffsets');
  expect(appJs).not.toContain('getRasterizedControlTextCenter');
  expect(controlCss).toContain('text-box: trim-both cap alphabetic;');
  expect(appCss).toContain('--control-label-ink-overflow: 0.3em;');
  expect(appCss).not.toContain('--control-label-text-box');
  expect(controlCss).not.toContain('.scope-selector-summary-anchor');
  expect(controlCss).toMatch(
    /\.scope-selector-trigger\.app-control-root\s*\{[^}]*grid-template-columns:[^}]*var\(--control-icon-slot-size\)[^}]*max-content[^}]*var\(--control-chevron-slot-size\);[^}]*justify-content:\s*center !important;/
  );
  expect(appJs).toContain("invariant: 'content-group-center'");
  expect(appJs).toContain("invariant: 'slot-block-center'");
  expect(appJs).not.toContain("invariant: 'label-ink-center'");
  expect(appJs).not.toContain("invariant: 'rendered-text-center'");
  expect(appJs).toContain("invariant: 'glyph-box-center'");
  expect(appJs).toContain("invariant: 'sidebar-glyph-box-center'");
  expect(appJs).toContain("invariant: 'theme-thumb-glyph-box-center'");
  expect(triangleSource).toContain("down: 'M5 9.33h14l-7 8Z'");
  expect(triangleSource).toContain("right: 'M9.33 5l8 7-8 7Z'");
  expect(accountsIconSource).toContain('viewBox="-1 -1 28 28"');
  expect(controlCss).toContain('font-synthesis: none;');
  expect(controlCss).toContain('> svg {');
  expect(indexCss).not.toContain('font-display: swap;');
  expect(indexCss.match(/font-display: block;/g)).toHaveLength(4);
  expect(indexCss).not.toContain('ascent-override');
  expect(indexCss).not.toContain('descent-override');
  expect(indexCss).not.toContain('line-gap-override');
});

test('slotted disclosure controls render one canonical chevron and rotate it from aria state', () => {
  const controlCss = fs.readFileSync(path.join(sourceRoot, 'controlSlots.css'), 'utf8');
  const chevronSource = fs.readFileSync(
    path.join(sourceRoot, 'components', 'ControlChevron.js'),
    'utf8'
  );
  const triangleSource = fs.readFileSync(
    path.join(sourceRoot, 'components', 'TriangleIcon.js'),
    'utf8'
  );
  const transactionsCss = fs.readFileSync(
    path.join(sourceRoot, 'pages', 'Transactions.css'),
    'utf8'
  );
  const violations = [];
  let chevronCount = 0;

  inspectButtons(({ filePath, node, openingElement, source }) => {
    const classes = openingClassSource(openingElement, source);
    if (!classes.includes('app-control-root')) return;

    for (const child of node.children) {
      const childSource = source.slice(child.start, child.end);
      if (!childSource.includes('app-control-chevron')) continue;
      chevronCount += 1;
      if (!childSource.includes('<ControlChevron')) {
        violations.push(`${path.relative(sourceRoot, filePath)}:${openingElement.loc.start.line}`);
      }
    }
  });

  expect(chevronCount).toBeGreaterThan(5);
  expect(violations).toEqual([]);
  expect(chevronSource).toContain('<TriangleIcon direction="down" className="app-control-chevron-glyph" />');
  expect(triangleSource).toContain("down: 'M5 9.33h14l-7 8Z'");
  expect(controlCss).toMatch(
    /\.app-control-root > \.app-control-chevron > \.app-control-chevron-glyph\s*\{[^}]*fill:\s*currentColor;[^}]*stroke:\s*none;/
  );
  expect(controlCss).toMatch(
    /\.app-control-root\[aria-expanded="true"\] > \.app-control-chevron > svg\s*\{[^}]*transform:\s*rotate\(180deg\);/
  );
  expect(transactionsCss).not.toContain('--control-chevron-size');
});

test('component CSS cannot reposition migrated labels or chevrons', () => {
  const componentCss = styleFiles(sourceRoot)
    .filter((filePath) => path.basename(filePath) !== 'controlSlots.css')
    .map((filePath) => ({
      filePath,
      source: fs.readFileSync(filePath, 'utf8'),
    }));
  const labelClasses = [
    'app-control-label',
    'app-dropdown-value',
    'app-view-filter-trigger-label',
    'dashboard-action-label',
    'dashboard-date-chip-text',
    'currency-picker-value',
    'investments-view-filter-trigger-label',
    'investments-currency-trigger-value',
    'scope-selector-summary',
    'settings-dropdown-value',
    'single-date-value',
    'support-logs-picker-text',
    'timeline-selector-summary',
    'transactions-page-size-value',
    'transactions-type-trigger-summary',
  ];
  const chevronClasses = [
    'add-txn-category-chevron',
    'app-control-chevron',
    'app-dropdown-chevron',
    'investments-filter-trigger-chevron',
    'scope-selector-chevron',
    'settings-dropdown-icon',
    'support-logs-picker-icon',
    'timeline-selector-chevron',
    'transactions-page-size-trigger-chevron',
    'transactions-type-trigger-chevron',
  ];
  const forbiddenLabelPlacement = /(?:^|;)\s*(?:display|align-items|justify-content|place-items|font-family|font-size|font-weight|line-height|letter-spacing|height|min-height|max-height|inset-block(?:-start|-end)?|top|bottom|transform)\s*:/;
  const forbiddenChevronGeometry = /(?:^|;)\s*(?:--control-chevron-(?:size|slot-size)|display|align-items|align-self|justify-content|justify-self|position|inset(?:-[a-z-]+)?|top|right|bottom|left|width|min-width|max-width|height|min-height|max-height|line-height|transform)\s*:/;
  const violations = [];

  componentCss.forEach(({ filePath, source }) => {
    labelClasses.forEach((className) => {
      ruleBodiesForClass(source, className).forEach((body) => {
        if (forbiddenLabelPlacement.test(body)) {
          violations.push(`${path.relative(sourceRoot, filePath)} .${className} label placement`);
        }
      });
    });
    chevronClasses.forEach((className) => {
      ruleBodiesForClass(source, className).forEach((body) => {
        if (forbiddenChevronGeometry.test(body) || /transition[^;]*transform/.test(body)) {
          violations.push(`${path.relative(sourceRoot, filePath)} .${className} chevron geometry`);
        }
      });
    });
  });

  expect(violations).toEqual([]);
});

test('specialized round controls preserve their edge and square-shell geometry', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const controlCss = fs.readFileSync(path.join(sourceRoot, 'controlSlots.css'), 'utf8');
  const appViewFiltersSource = fs.readFileSync(
    path.join(sourceRoot, 'components', 'AppViewFiltersMenu.js'),
    'utf8'
  );
  const scopeSelectorSource = fs.readFileSync(
    path.join(sourceRoot, 'components', 'InstitutionAccountSelector.js'),
    'utf8'
  );
  const timelineTriggerSource = fs.readFileSync(
    path.join(sourceRoot, 'components', 'TimelineTrigger.js'),
    'utf8'
  );
  const dashboardSource = fs.readFileSync(path.join(sourceRoot, 'pages', 'Dashboard.js'), 'utf8');

  expect(controlCss).toMatch(
    /\.app-theme-toggle-btn\.app-control-root:first-child > \.app-control-icon:only-child\s*\{\s*justify-self:\s*start;/
  );
  expect(controlCss).toMatch(
    /\.app-theme-toggle-btn\.app-control-root:last-child > \.app-control-icon:only-child\s*\{\s*justify-self:\s*end;/
  );
  expect(appCss).toContain('--view-filter-control-icon-svg-size: var(--top-control-icon-svg-size);');
  expect(controlCss).toMatch(
    /\.app-view-filter-panel \.app-control-root\s*\{[^}]*--control-icon-size:\s*var\(--view-filter-control-icon-svg-size\);/
  );
  expect(controlCss).toMatch(
    /\.chart-color-trigger\.app-control-root,\s*\.market-strip-config-trigger\.app-control-root\s*\{[^}]*--control-fixed-size:\s*var\(--panel-round-icon-control-size\);[^}]*width:\s*var\(--control-fixed-size\);[^}]*height:\s*var\(--control-fixed-size\);/
  );
  expect(controlCss).toMatch(
    /\.timeline-range-picker-nav\.app-control-root\s*\{[^}]*--control-fixed-size:\s*var\(--panel-control-min-height\);[^}]*--control-icon-slot-size:\s*100%;[^}]*width:\s*var\(--control-fixed-size\);[^}]*height:\s*var\(--control-fixed-size\);/
  );
  expect(controlCss).toMatch(
    /\.timeline-selector-trigger\.app-control-root,\s*\.transactions-type-trigger\.app-control-root\s*\{[^}]*grid-template-columns:[^}]*var\(--control-icon-slot-size\)[^}]*max-content[^}]*var\(--control-chevron-slot-size\);[^}]*justify-content:\s*center !important;/
  );
  expect(controlCss).toMatch(
    /\.dashboard-cashflow-header \.cf-timeline-trigger\.app-control-root\s*\{[^}]*grid-template-columns:[^}]*max-content[^}]*var\(--control-chevron-slot-size\);/
  );
  expect(appViewFiltersSource).toContain("import { LuListFilter } from 'react-icons/lu';");
  expect(appViewFiltersSource).not.toContain('MdTune');
  expect(scopeSelectorSource).toContain("import { LuLandmark } from 'react-icons/lu';");
  expect(scopeSelectorSource).not.toContain('BsBank2');
  expect(timelineTriggerSource).toContain("import { LuCalendarDays } from 'react-icons/lu';");
  expect(dashboardSource).toContain('LuSlidersHorizontal');
});

test('scope picker and custom range controls use the correct surfaced chrome', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const transactionsCss = fs.readFileSync(path.join(sourceRoot, 'pages', 'Transactions.css'), 'utf8');
  const transactionsSource = fs.readFileSync(path.join(sourceRoot, 'pages', 'Transactions.js'), 'utf8');
  const holdingsCss = fs.readFileSync(path.join(sourceRoot, 'pages', 'Holdings.css'), 'utf8');
  const appViewFiltersSource = fs.readFileSync(path.join(sourceRoot, 'components', 'AppViewFiltersMenu.js'), 'utf8');

  expect(appViewFiltersSource).not.toContain('PANEL_MAX_WIDTH_REM');
  expect(appViewFiltersSource).toContain("width: 'max-content'");
  expect(appViewFiltersSource).toContain("left: 'auto'");
  expect(appViewFiltersSource).toContain('right: `${Math.max(0, portalRect.right - viewportRight + scrollX)}px`');
  expect(appViewFiltersSource).not.toContain('getBoundingClientRect().width');
  expect(appViewFiltersSource).not.toContain('new ResizeObserver(() => updatePanelPosition())');

  expect(appCss).toMatch(
    /\.app-view-filter-panel\s*\{[^}]*interpolate-size:\s*allow-keywords;[^}]*width:\s*max-content;[^}]*max-width:\s*calc\(100vw - 1rem\);[^}]*transition:[^}]*width 0\.18s ease,[^}]*max-width 0\.18s ease;/
  );
  expect(appCss).toContain('--view-filter-label-column-width: 5.75rem;');
  expect(appCss).toMatch(
    /\.app-view-filter-row\s*\{[^}]*grid-template-columns:[^}]*minmax\(var\(--view-filter-label-column-width\),\s*max-content\)[^}]*minmax\(max-content,\s*1fr\);[^}]*justify-content:\s*stretch;/
  );
  expect(appCss).toMatch(
    /\.app-view-filter-label\s*\{[^}]*justify-self:\s*start;/
  );
  expect(appCss).toMatch(
    /\.app-view-filter-control\s*\{[^}]*width:\s*max-content;[^}]*max-width:\s*none;[^}]*justify-self:\s*center;/
  );
  expect(appCss).toMatch(
    /\.app-view-filter-panel \.app-control-root > \.app-control-label\s*\{[^}]*min-width:\s*max-content;[^}]*max-width:\s*none;[^}]*overflow:\s*visible;[^}]*text-overflow:\s*clip;/
  );

  expect(appCss).toMatch(
    /\.app-view-filter-panel \.toolbar-filter-slot\s*\{[^}]*width:\s*max-content;[^}]*max-width:\s*none;[^}]*flex:\s*0 0 auto;/
  );
  expect(appCss).toMatch(
    /\.app-view-filter-panel \.investments-filter-popover,[^{]*\.app-view-filter-panel \.accounts-group-popover\s*\{[^}]*width:\s*max-content;[^}]*max-width:\s*none;[^}]*flex:\s*0 0 auto;/
  );
  expect(transactionsSource).toContain(
    'className="investments-filter-popover transactions-toolbar-popover transactions-toolbar-popover-type"'
  );
  expect(transactionsSource).toContain(
    'className="investments-filter-popover transactions-toolbar-popover transactions-toolbar-popover-timeline"'
  );
  expect(transactionsSource).toContain(
    'className="investments-filter-popover transactions-toolbar-popover transactions-toolbar-popover-institutions"'
  );
  expect(appCss).toMatch(
    /\.app-view-filter-panel :is\(\s*\.transactions-toolbar-popover-type \.transactions-type-trigger,[^)]*\.transactions-toolbar-popover-timeline \.transactions-timeline-trigger\s*\)\s*\{[^}]*width:\s*100%;[^}]*max-width:\s*100%;/
  );
  expect(appCss).toMatch(
    /\.app-view-filter-panel \.scope-selector-trigger,[^{]*\.app-view-filter-panel \.investments-filter-trigger,[^{]*\.app-view-filter-panel \.accounts-group-trigger,[^{]*\.app-view-filter-panel \.dashboard-action-trigger,[^{]*\.app-view-filter-panel \.app-dropdown-trigger,[^{]*\.app-view-filter-panel \.single-date-trigger,[^{]*\.app-view-filter-panel \.timeline-selector-trigger\s*\{[^}]*width:\s*max-content;[^}]*min-width:\s*0;[^}]*height:\s*var\(--panel-control-min-height\);[^}]*max-width:\s*none;[^}]*border-radius:\s*var\(--panel-control-radius\);[^}]*transition:[^}]*width 0\.18s ease,[^}]*max-width 0\.18s ease;/
  );
  expect(appCss).not.toMatch(
    /\.dashboard-(?:scope|timeline)-popover [^{]+\{[^}]*max-width:\s*min\((?:16|20|26|28)rem/
  );
  expect(transactionsCss).not.toMatch(
    /\.transactions-toolbar-popover(?:-type|-timeline)?[^{]*\{[^}]*max-width:\s*min\((?:18|26)rem/
  );
  expect(holdingsCss).not.toMatch(
    /\.income-timeline-trigger\s*\{[^}]*max-width:\s*min\(26rem/
  );
  expect(appCss).toMatch(
    /\.institution-account-selector-tab\s*\{[^}]*border:\s*var\(--surface-button-border-width\) solid var\(--surface-button-border-color\);[^}]*background:\s*var\(--surface-button-bg\);[^}]*box-shadow:\s*var\(--surface-button-shadow\);/
  );
  expect(appCss).toMatch(
    /\.timeline-range-picker-summary-item:not\(button\)\s*\{[^}]*background:\s*var\(--surface-inset-layer-bg\);[^}]*background-origin:\s*border-box;[^}]*background-clip:\s*border-box;[^}]*box-shadow:\s*none;/
  );
  expect(appCss).toMatch(
    /\.timeline-range-picker-calendar-header\s*\{[^}]*grid-template-columns:[^}]*var\(--panel-control-min-height\)[^}]*minmax\(0,\s*1fr\)[^}]*var\(--panel-control-min-height\);/
  );
});

test('panel period controls keep the surfaced Cash Flow trigger and static Expiration readout distinct', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const controlCss = fs.readFileSync(path.join(sourceRoot, 'controlSlots.css'), 'utf8');
  const holdingsSource = fs.readFileSync(path.join(sourceRoot, 'pages', 'Holdings.js'), 'utf8');

  expect(controlCss).not.toContain(
    '.panel-shell .cf-timeline-popover .cf-timeline-trigger.app-control-root'
  );
  expect(holdingsSource).toContain(
    'className="chart-view-stepper-label options-calendar-period-label"'
  );
  expect(holdingsSource).not.toContain('options-calendar-timeline-trigger');
  expect(appCss).not.toContain('--panel-calendar-padding-block');
  expect(appCss).not.toContain('--panel-calendar-control-radius');
});

test('visibility and export controls use the same centered icon-only slot contract', () => {
  const appCss = fs.readFileSync(path.join(sourceRoot, 'App.css'), 'utf8');
  const controlCss = fs.readFileSync(path.join(sourceRoot, 'controlSlots.css'), 'utf8');
  const toolbarIconSource = fs.readFileSync(
    path.join(sourceRoot, 'components', 'ToolbarActionIcon.js'),
    'utf8'
  );
  const componentSources = [
    path.join(sourceRoot, 'components', 'BalancesToggleButton.js'),
    path.join(sourceRoot, 'components', 'CsvExportButton.js'),
  ].map((filePath) => fs.readFileSync(filePath, 'utf8'));

  componentSources.forEach((source) => {
    expect(source).toContain('top-toolbar-icon-btn');
    expect(source).toContain('app-control-root');
    expect(source).toContain('<span className="app-control-icon" aria-hidden="true">');
    expect(source).not.toContain("from 'react-icons/md'");
  });
  expect(componentSources[0]).toContain('<VisibilityToolbarIcon hidden={balancesHidden} />');
  expect(componentSources[1]).toContain('<ExportToolbarIcon />');
  expect(toolbarIconSource).toContain("viewBox: '0 0.5 24 24'");
  expect(toolbarIconSource).toContain("viewBox: '0 -0.5 24 24'");
  expect(toolbarIconSource).toContain('className="app-toolbar-action-icon"');
  expect(appCss).toMatch(
    /--top-control-icon-button-size-effective:\s*round\(\s*nearest,\s*var\(--top-control-icon-button-size\),\s*var\(--app-device-pixel\)\s*\);/
  );
  expect(appCss).toMatch(
    /--top-control-icon-button-svg-size-effective:\s*round\(\s*nearest,\s*var\(--top-control-icon-button-svg-size\),\s*var\(--app-device-pixel\)\s*\);/
  );
  expect(controlCss).toMatch(
    /\.page-shell-toolbar \.top-toolbar-icon-btn\.app-control-root\s*\{[^}]*--control-icon-size:\s*var\(--top-control-icon-button-svg-size-effective\);[^}]*--control-fixed-size:\s*var\(--top-control-icon-button-size-effective\);/
  );
  expect(controlCss).toMatch(
    /\.app-control-root > :is\(\.app-control-icon, \.app-control-chevron\):only-child\s*\{[^}]*grid-column:\s*1\s*\/\s*-1;[^}]*width:\s*100%;/
  );
  expect(controlCss).toMatch(
    /\.app-control-root:has\(> :is\(\.app-control-icon, \.app-control-label, \.app-control-chevron\):only-child\)\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\);[^}]*justify-content:\s*stretch !important;[^}]*gap:\s*0;/
  );
});

test('page status notices keep the canonical themed alert structure', () => {
  const componentSource = fs.readFileSync(
    path.join(sourceRoot, 'components', 'AppStatusNotice.js'),
    'utf8'
  );
  const componentCss = fs.readFileSync(
    path.join(sourceRoot, 'components', 'AppStatusNotice.css'),
    'utf8'
  );

  expect(componentSource).toContain('className="app-status-notice" role="alert"');
  expect(componentSource).toContain('className="modal-close app-status-notice-dismiss"');
  expect(componentSource).toContain('<MdWarningAmber');
  expect(componentCss).toMatch(
    /\.app-status-notice\s*\{[^}]*border:\s*var\(--surface-inset-border-width\) solid var\(--color-network-warning\);[^}]*background:\s*var\(--surface-inset-bg\);/
  );
  expect(componentCss).toMatch(
    /\.app-status-notice-title\s*\{[^}]*font-size:\s*var\(--type-panel-title-size\);/
  );
  expect(componentCss).toMatch(
    /\.app-status-notice-message\s*\{[^}]*font-size:\s*var\(--type-body-muted-size\);/
  );
});
