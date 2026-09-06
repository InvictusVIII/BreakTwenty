const { spawn } = require('node:child_process');
const path = require('node:path');

const { syncProviderCatalog, watchProviderCatalog } = require('./syncProviderCatalog');
const { syncLoadingScreen, watchLoadingScreen } = require('./syncLoadingScreen');

const FRONTEND_ROOT = path.resolve(__dirname, '..');

function viteCommand() {
  return process.platform === 'win32' ? 'vite.cmd' : 'vite';
}

function main() {
  syncProviderCatalog();
  syncLoadingScreen();
  const stopWatchingProviderCatalog = watchProviderCatalog();
  const stopWatchingLoadingScreen = watchLoadingScreen();

  const child = spawn(viteCommand(), process.argv.slice(2), {
    cwd: FRONTEND_ROOT,
    env: process.env,
    stdio: 'inherit',
  });

  const forwardSignal = (signal) => {
    if (child.exitCode === null) {
      child.kill(signal);
    }
  };

  process.on('SIGINT', () => forwardSignal('SIGINT'));
  process.on('SIGTERM', () => forwardSignal('SIGTERM'));

  child.on('error', (error) => {
    stopWatchingProviderCatalog();
    stopWatchingLoadingScreen();
    console.error(`[frontend-start] Failed to start Vite: ${error.message}`);
    process.exit(1);
  });

  child.on('exit', (code, signal) => {
    stopWatchingProviderCatalog();
    stopWatchingLoadingScreen();
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code ?? 0);
  });
}

try {
  main();
} catch (error) {
  console.error(`[frontend-start] ${error.message}`);
  process.exit(1);
}
