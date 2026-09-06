import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';

export default [
  {
    ignores: ['build/**', 'node_modules/**'],
  },
  {
    files: ['src/**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: {
        ecmaFeatures: {
          jsx: true,
        },
      },
      globals: {
        ...globals.browser,
        ...globals.node,
        ...globals.vitest,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...js.configs.recommended.rules,
      'no-empty': ['error', { allowEmptyCatch: true }],
      'no-unused-vars': ['error', {
        args: 'none',
        caughtErrors: 'none',
        varsIgnorePattern: '^(React|_)$',
      }],
      'no-useless-assignment': 'off',
      'no-restricted-syntax': ['error',
        {
          selector: 'JSXOpeningElement[name.name="select"]',
          message: 'Use components/Dropdown.js instead of native <select>; the app theme requires fully themed dropdown menus.',
        },
        {
          selector: 'JSXOpeningElement[name.name="option"]',
          message: 'Use components/Dropdown.js options instead of native <option>; the app theme requires fully themed dropdown menus.',
        },
        {
          selector: 'JSXOpeningElement[name.name="optgroup"]',
          message: 'Use grouped app-themed Dropdown options instead of native <optgroup>; the app theme requires fully themed dropdown menus.',
        },
        {
          selector: 'JSXOpeningElement[name.name="datalist"]',
          message: 'Use an app-themed picker/dropdown instead of native <datalist>; the app theme requires fully themed menus.',
        },
      ],
      'react-hooks/config': 'error',
      'react-hooks/error-boundaries': 'error',
      'react-hooks/gating': 'error',
      'react-hooks/globals': 'error',
      'react-hooks/immutability': 'error',
      'react-hooks/incompatible-library': 'warn',
      'react-hooks/preserve-manual-memoization': 'error',
      'react-hooks/purity': 'error',
      'react-hooks/refs': 'error',
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/set-state-in-effect': 'error',
      'react-hooks/set-state-in-render': 'error',
      'react-hooks/static-components': 'error',
      'react-hooks/unsupported-syntax': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-hooks/use-memo': 'error',
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
    },
  },
];
