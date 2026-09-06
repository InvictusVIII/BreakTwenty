#!/usr/bin/env node

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const REPO_ROOT = path.resolve(__dirname, '..');
const BUILD_SCRIPT = path.join(REPO_ROOT, 'scripts', 'build_desktop_packaged_runtime.js');
const MARK_PATH = path.join(
  REPO_ROOT,
  'frontend',
  'public',
  'assets',
  'brand',
  'breaktwenty-mark-dark.png',
);
const WORDMARK_PATH = path.join(
  REPO_ROOT,
  'frontend',
  'public',
  'assets',
  'brand',
  'breaktwenty-wordmark-dark-320.png',
);

function helperPath(buildRoot) {
  return path.join(
    buildRoot,
    'desktop',
    'update-helper',
    process.platform === 'win32' ? 'BreakTwentyUpdateHelper.exe' : 'BreakTwentyUpdateHelper',
  );
}

function helperArguments({ delayMs, probeMode = '', probePath = '', readyPath, timeoutSeconds }) {
  const args = [
    '--ready-path', readyPath,
    '--delay-ms', String(delayMs),
    '--timeout-seconds', String(timeoutSeconds),
    '--version', 'test-preview',
  ];
  if (process.platform !== 'win32') {
    args.push('--mark-path', MARK_PATH, '--wordmark-path', WORDMARK_PATH);
  }
  if (probePath) {
    args.push('--probe-path', probePath);
  }
  if (probeMode) {
    args.push('--probe-mode', probeMode);
  }
  return args;
}

function compileHelper(buildRoot) {
  const result = spawnSync(
    process.execPath,
    [BUILD_SCRIPT, '--output', buildRoot, '--update-helper-only'],
    { cwd: REPO_ROOT, encoding: 'utf8', stdio: 'inherit', windowsHide: true },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`Native update helper compilation exited with code ${result.status}.`);
  }
  const executable = helperPath(buildRoot);
  if (!fs.existsSync(executable)) {
    throw new Error(`Compiled update helper is missing at ${executable}.`);
  }
  return executable;
}

function readProbeEvents(probePath) {
  return fs.readFileSync(probePath, 'utf8').trim().split(/\r?\n/).filter(Boolean);
}

async function runScenario(executable, scenarioRoot, {
  delayMs,
  expectedEvents,
  name,
  probeMode = 'headless',
  readyAfterMs,
  windowsHide = true,
}) {
  const readyPath = path.join(scenarioRoot, `${name}.ready`);
  const probePath = path.join(scenarioRoot, `${name}.probe`);
  const startedAt = Date.now();
  const child = spawn(executable, helperArguments({
    delayMs,
    probeMode,
    probePath,
    readyPath,
    timeoutSeconds: 5,
  }), {
    stdio: 'inherit',
    windowsHide,
  });
  const readyTimer = setTimeout(() => {
    fs.writeFileSync(readyPath, '');
  }, readyAfterMs);
  const safetyTimer = setTimeout(() => {
    child.kill();
  }, 8000);

  const exitCode = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', resolve);
  });
  clearTimeout(readyTimer);
  clearTimeout(safetyTimer);
  assert.equal(exitCode, 0, `${name} helper process should exit successfully`);
  assert.deepEqual(readProbeEvents(probePath), expectedEvents, `${name} lifecycle events`);
  const elapsedMs = Date.now() - startedAt;
  if (expectedEvents.includes('shown')) {
    assert.ok(elapsedMs >= delayMs, `${name} should not show before its ${delayMs}ms grace period`);
  } else {
    assert.ok(elapsedMs < delayMs + 500, `${name} should suppress promptly when the app is ready`);
  }
  console.log(`[BreakTwenty] ${name}: ${expectedEvents.join(' -> ')} (${elapsedMs}ms)`);
}

function runPreview(executable, scenarioRoot) {
  const readyPath = path.join(scenarioRoot, 'preview.ready');
  const probePath = path.join(scenarioRoot, 'preview.probe');
  console.log('[BreakTwenty] The native updater window will appear after three seconds and close after eight seconds.');
  const result = spawnSync(executable, helperArguments({
    delayMs: 3000,
    probePath,
    readyPath,
    timeoutSeconds: 8,
  }), { stdio: 'inherit', windowsHide: false });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`Native update helper preview exited with code ${result.status}.`);
  }
  const events = readProbeEvents(probePath);
  if (!events.includes('shown')) {
    throw new Error(`Native update helper exited before showing its window (${events.join(' -> ') || 'no lifecycle events'}).`);
  }
  assert.deepEqual(events, ['started', 'shown', 'timeout'], 'preview lifecycle events');
  console.log(`[BreakTwenty] Native ${process.platform} update helper preview reached its window and completed.`);
}

async function main() {
  const preview = process.argv.slice(2).includes('--preview');
  const guiSmoke = process.argv.slice(2).includes('--gui-smoke');
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-update-helper-'));
  const buildRoot = path.join(tempRoot, 'build');
  try {
    const executable = compileHelper(buildRoot);
    if (preview) {
      runPreview(executable, tempRoot);
      return;
    }
    if (guiSmoke) {
      await runScenario(executable, tempRoot, {
        delayMs: 300,
        expectedEvents: ['started', 'shown', 'ready'],
        name: 'native window construction',
        probeMode: '',
        readyAfterMs: 1000,
        windowsHide: false,
      });
      console.log(`[BreakTwenty] Native ${process.platform} update helper window smoke passed.`);
      return;
    }
    await runScenario(executable, tempRoot, {
      delayMs: 500,
      expectedEvents: ['started', 'suppressed-ready'],
      name: 'fast-update suppression',
      readyAfterMs: 100,
    });
    await runScenario(executable, tempRoot, {
      delayMs: 500,
      expectedEvents: ['started', 'shown', 'ready'],
      name: 'slow-update visibility',
      readyAfterMs: 1000,
    });
    console.log(`[BreakTwenty] Native ${process.platform} update helper lifecycle passed.`);
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`[BreakTwenty] ${error.message}`);
  process.exitCode = 1;
});
