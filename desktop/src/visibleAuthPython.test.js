const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  MANAGED_VISIBLE_AUTH_PYTHON,
  resolveVisibleAuthPython,
} = require('./visibleAuthPython');

test('managed visible-auth Python requires an explicit runtime executable', (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-visible-auth-python-'));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const managedPython = path.join(root, 'private-runtime', 'bin', 'python');
  fs.mkdirSync(path.dirname(managedPython), { recursive: true });
  fs.writeFileSync(managedPython, '', 'utf8');

  assert.equal(
    resolveVisibleAuthPython({
      runtimeEnv: { BREAKTWENTY_VISIBLE_AUTH_PYTHON: managedPython },
      commandName: MANAGED_VISIBLE_AUTH_PYTHON,
    }),
    managedPython,
  );
  assert.equal(
    resolveVisibleAuthPython({ commandName: MANAGED_VISIBLE_AUTH_PYTHON }),
    '',
  );
});

test('managed visible-auth Python never searches the source tree', (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-visible-auth-python-'));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const legacyPython = path.join(root, '.venv-desktop-auth', 'bin', 'python');
  fs.mkdirSync(path.dirname(legacyPython), { recursive: true });
  fs.writeFileSync(legacyPython, '', 'utf8');

  assert.equal(
    resolveVisibleAuthPython({
      resourceRoot: root,
      commandName: MANAGED_VISIBLE_AUTH_PYTHON,
    }),
    '',
  );
});
