import { readFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const require = createRequire(import.meta.url);
const parser = require('@babel/parser');
const traverse = require('@babel/traverse').default;
const componentDir = path.dirname(fileURLToPath(import.meta.url));
const DATA_ADAPTERS = [
  'promoDemoEnvironment.js',
  'tourDemoData.js',
];
const DEMO_ADAPTER_CONSUMERS = {
  promoDemoEnvironment: [
    'App.js',
    'components/InstitutionSettingsModal.js',
    'utils/desktopBridge.js',
  ],
  tourDemoData: [
    'App.js',
    'components/WelcomeModal.js',
    'pages/Accounts.js',
    'pages/CashFlow.js',
    'pages/Dashboard.js',
    'pages/Holdings.js',
    'pages/Transactions.js',
  ],
};

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(entryPath);
    if (!entry.name.endsWith('.js') || entry.name.endsWith('.test.js')) return [];
    return [entryPath];
  });
}

describe('demo environment frontend parity', () => {
  it.each(DATA_ADAPTERS)('%s stays a data-only adapter with no forked UI', (filename) => {
    const source = readFileSync(path.join(componentDir, filename), 'utf8');
    const ast = parser.parse(source, { sourceType: 'module', plugins: ['jsx'] });
    const jsxNodes = [];
    const forbiddenImports = [];
    const documentReferences = [];

    traverse(ast, {
      ImportDeclaration(nodePath) {
        const importPath = nodePath.node.source.value;
        if (![
          '../config',
          '../constants/',
          '../utils/',
        ].some((allowedPath) => importPath.startsWith(allowedPath))) {
          forbiddenImports.push(importPath);
        }
      },
      Identifier(nodePath) {
        if (nodePath.node.name === 'document' && nodePath.isReferencedIdentifier()) {
          documentReferences.push(nodePath.node);
        }
      },
      JSXElement(nodePath) {
        jsxNodes.push(nodePath.node);
      },
      JSXFragment(nodePath) {
        jsxNodes.push(nodePath.node);
      },
    });

    expect(forbiddenImports).toEqual([]);
    expect(jsxNodes).toEqual([]);
    expect(documentReferences).toEqual([]);
  });

  it('keeps the welcome overlay separate from canonical route implementations', () => {
    const source = readFileSync(path.join(componentDir, 'WelcomeModal.js'), 'utf8');
    const ast = parser.parse(source, { sourceType: 'module', plugins: ['jsx'] });
    const pageImports = [];

    traverse(ast, {
      ImportDeclaration(nodePath) {
        const importPath = nodePath.node.source.value;
        if (importPath.includes('/pages/')) pageImports.push(importPath);
      },
    });

    expect(pageImports).toEqual([]);
  });

  it('allows demo-aware behavior only at the reviewed integration points', () => {
    const sourceRoot = path.resolve(componentDir, '..');
    const consumers = {
      promoDemoEnvironment: [],
      tourDemoData: [],
    };

    sourceFiles(sourceRoot).forEach((filePath) => {
      const source = readFileSync(filePath, 'utf8');
      if (!source.includes('DemoEnvironment') && !source.includes('tourDemoData')) return;
      const ast = parser.parse(source, { sourceType: 'module', plugins: ['jsx'] });
      traverse(ast, {
        ImportDeclaration(nodePath) {
          const importPath = nodePath.node.source.value;
          Object.keys(consumers).forEach((adapter) => {
            if (importPath.endsWith(adapter)) {
              consumers[adapter].push(path.relative(sourceRoot, filePath));
            }
          });
        },
      });
    });

    expect(consumers).toEqual(DEMO_ADAPTER_CONSUMERS);
  });
});
