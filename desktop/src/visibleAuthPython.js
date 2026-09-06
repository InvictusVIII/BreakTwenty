const fs = require('node:fs');
const path = require('node:path');

const MANAGED_VISIBLE_AUTH_PYTHON = 'breaktwenty-visible-auth-python';

function commandAvailable(commandName) {
  return Boolean(commandName) && (!commandName.includes(path.sep) || fs.existsSync(commandName));
}

function isPythonCommand(commandName) {
  return commandName === MANAGED_VISIBLE_AUTH_PYTHON
    || /python(?:3)?(?:\.exe)?$/i.test(path.basename(commandName || ''));
}

function resolveVisibleAuthPython({
  runtimeEnv = {},
  commandName = '',
} = {}) {
  const desktopAuthDir = String(runtimeEnv.BREAKTWENTY_DESKTOP_AUTH_DIR || '').trim();
  const candidates = [
    runtimeEnv.BREAKTWENTY_VISIBLE_AUTH_PYTHON,
    runtimeEnv.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON,
    process.env.BREAKTWENTY_VISIBLE_AUTH_PYTHON,
    process.env.BREAKTWENTY_EMBEDDED_BACKEND_PYTHON,
    desktopAuthDir ? path.join(desktopAuthDir, 'visible-auth-venv', 'Scripts', 'python.exe') : '',
    desktopAuthDir ? path.join(desktopAuthDir, 'visible-auth-venv', 'bin', 'python') : '',
    commandName === MANAGED_VISIBLE_AUTH_PYTHON ? '' : commandName,
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (commandAvailable(candidate)) {
      return candidate;
    }
  }
  return '';
}

module.exports = {
  MANAGED_VISIBLE_AUTH_PYTHON,
  commandAvailable,
  isPythonCommand,
  resolveVisibleAuthPython,
};
