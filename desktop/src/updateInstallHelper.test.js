const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { markUpdateInstallWindowVisible, _test } = require('./updateInstallHelper');

const request = {
  markPath: '/tmp/update-helper/breaktwenty-mark-dark.png',
  readyPath: '/tmp/update-helper/app-window-visible',
  version: '1.2.3',
  wordmarkPath: '/tmp/update-helper/breaktwenty-wordmark-dark-320.png',
};

test('canonical app marks match the vetted artifact-free assets', () => {
  const brandRoot = path.join(__dirname, '..', '..', 'frontend', 'public', 'assets', 'brand');
  const digest = (fileName) => crypto
    .createHash('sha256')
    .update(fs.readFileSync(path.join(brandRoot, fileName)))
    .digest('hex');

  assert.equal(
    digest('breaktwenty-mark-dark.png'),
    '2cb86b7da0c167efa95726bb782b209859c81cb7f19613e14d0caa7a8868fe3a',
  );
  assert.equal(
    digest('breaktwenty-mark-light.png'),
    '4290dabb08b0e1285f860a1a2d612966aaf6a3d8a0cd5d020d1cfef17139d3ae',
  );
});

test('Linux helper starts its grace period at handoff and suppresses a fast-update flash', () => {
  const args = _test.brandedHelperArguments(request);
  assert.deepEqual(args, [
    '--ready-path', request.readyPath,
    '--mark-path', request.markPath, '--wordmark-path', request.wordmarkPath,
    '--delay-ms', '3000', '--timeout-seconds', '600', '--version', '1.2.3',
  ]);
  const source = fs.readFileSync(path.join(__dirname, '..', 'linux-update-helper', 'main.c'), 'utf8');
  assert.doesNotMatch(source, /parent[_-]pid|process_is_running/);
  assert.match(source, /show_at = g_get_monotonic_time\(\) \+ \(\(gint64\)delay_ms \* 1000\)/);
  assert.ok(source.indexOf('g_file_test(ready_path, G_FILE_TEST_EXISTS)') < source.indexOf('gtk_widget_show_all(window)'));
  assert.match(source, /gdk_pixbuf_new_from_file_at_scale/);
  assert.match(source, /gtk_spinner_start/);
  assert.match(source, /headless_probe/);
  assert.match(source, /append_probe\(probe_path, "shown"\)/);
  assert.doesNotMatch(source, /zenity|kdialog|xmessage/);
});

test('post-close helpers and the app loading screen use the canonical cleaned dark wordmark family', () => {
  const resourcesPath = path.join('/opt', 'BreakTwenty', 'resources');
  const assets = _test.helperBrandAssetSourcePaths(resourcesPath);
  assert.equal(assets.markPath, path.join(resourcesPath, 'frontend', 'build', 'assets', 'brand', 'breaktwenty-mark-dark.png'));
  assert.equal(assets.wordmarkPath, path.join(resourcesPath, 'frontend', 'build', 'assets', 'brand', 'breaktwenty-wordmark-dark-320.png'));

  const mainSource = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');
  assert.match(mainSource, /assets\/brand\/breaktwenty-mark-dark\.png/);
  assert.match(mainSource, /assets\/brand\/breaktwenty-wordmark-dark-160\.png/);
  assert.match(mainSource, /assets\/brand\/breaktwenty-wordmark-dark-320\.png/);
});

test('the relaunched app acknowledges an actually visible main window through user data', () => {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'breaktwenty-update-visible-'));
  const app = { getPath: (name) => (name === 'userData' ? userData : '') };
  try {
    assert.equal(markUpdateInstallWindowVisible(app), true);
    assert.equal(fs.existsSync(_test.updateReadyPath(app)), true);
  } finally {
    fs.rmSync(userData, { recursive: true, force: true });
  }
});

test('macOS uses a packaged native AppKit helper with the readiness handshake', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'macos-update-helper', 'main.swift'), 'utf8');
  assert.doesNotMatch(source, /parentPID|processIsRunning|Darwin\.kill/);
  assert.match(source, /let showAt = Date\(\)\.addingTimeInterval/);
  assert.match(source, /fileManager\.fileExists\(atPath: readyPath\)/);
  assert.match(source, /NSImage\(contentsOfFile: path\)/);
  assert.match(source, /NSProgressIndicator/);
  assert.match(source, /application\.run\(\)/);
  assert.match(source, /headlessProbe/);
  assert.match(source, /appendProbe\(path: probePath, event: "shown"\)/);
  assert.doesNotMatch(source, /contentTintColor|osascript|JXA|"20"|"Break"|"Twenty"/);
});

test('Windows update handoff uses the packaged native helper with explicit lifecycle arguments', () => {
  const args = _test.windowsHelperArguments({
    ...request,
  });
  assert.deepEqual(args, [
    '--ready-path', request.readyPath,
    '--delay-ms', '3000',
    '--timeout-seconds', '600',
    '--version', '1.2.3',
  ]);
  assert.equal(
    _test.windowsHelperSourcePath('C:\\Program Files\\BreakTwenty\\resources'),
    path.join(
      'C:\\Program Files\\BreakTwenty\\resources',
      'desktop',
      'update-helper',
      'BreakTwentyUpdateHelper.exe',
    ),
  );

  const helperSource = fs.readFileSync(
    path.join(__dirname, '..', 'windows-update-helper', 'Program.cs'),
    'utf8',
  );
  assert.doesNotMatch(helperSource, /parentPid|IsProcessRunning/);
  assert.match(helperSource, /DateTime\.UtcNow\.AddMilliseconds\(delayMilliseconds\)/);
  assert.match(helperSource, /Application\.Run\(new UpdateProgressForm/);
  assert.match(helperSource, /Please wait, BreakTwenty is updating/);
  assert.match(helperSource, /File\.Exists\(readyPath\)/);
  assert.match(helperSource, /BreakTwenty\.UpdateHelper\.Mark\.png/);
  assert.match(helperSource, /BreakTwenty\.UpdateHelper\.Wordmark\.png/);
  assert.match(helperSource, /Stopwatch\.StartNew\(\)/);
  assert.match(helperSource, /animationTimer\.Interval = 16/);
  assert.match(helperSource, /Elapsed\.TotalMilliseconds % 900\.0/);
  assert.match(helperSource, /headlessProbe/);
  assert.match(helperSource, /AppendProbe\(probePath, "shown"\)/);
  assert.match(helperSource, /catch \(Exception error\)[\s\S]*"error-" \+ error\.GetType\(\)\.Name/);
  const brandControlSource = helperSource.slice(helperSource.indexOf('internal sealed class BrandImageControl'));
  assert.ok(
    brandControlSource.indexOf('SetStyle(') < brandControlSource.indexOf('BackColor = Color.Transparent;'),
    'transparent-background support must be enabled before assigning the transparent color',
  );
  assert.doesNotMatch(helperSource, /CreateLabel\(\s*"20"|CreateLabel\(\s*"Break"|CreateLabel\(\s*"Twenty"/);
  assert.doesNotMatch(helperSource, /PowerShell|powershell\.exe/);
});

test('Windows packaging compiles, includes, and verifies the native update helper', () => {
  const buildSource = fs.readFileSync(
    path.join(__dirname, '..', '..', 'scripts', 'build_desktop_packaged_runtime.js'),
    'utf8',
  );
  const packageAction = fs.readFileSync(
    path.join(__dirname, '..', '..', '.github', 'actions', 'desktop-package', 'action.yml'),
    'utf8',
  );
  const helperHarness = fs.readFileSync(
    path.join(__dirname, '..', '..', 'scripts', 'update_helper_harness.js'),
    'utf8',
  );
  const helperWorkflow = fs.readFileSync(
    path.join(__dirname, '..', '..', '.github', 'workflows', 'update-helper-smoke.yml'),
    'utf8',
  );

  assert.match(buildSource, /function buildWindowsUpdateHelper\(\)/);
  assert.match(buildSource, /\/target:winexe/);
  assert.match(buildSource, /\/reference:System\.Windows\.Forms\.dll/);
  assert.match(buildSource, /breaktwenty-mark-dark\.png/);
  assert.match(buildSource, /breaktwenty-wordmark-dark-320\.png/);
  assert.match(buildSource, /function buildLinuxUpdateHelper\(\)/);
  assert.match(buildSource, /gtk\+-3\.0/);
  assert.match(buildSource, /function buildMacosUpdateHelper\(\)/);
  assert.match(buildSource, /xcrun/);
  assert.match(buildSource, /swiftc/);
  assert.match(buildSource, /buildWindowsUpdateHelper\(\);/);
  assert.match(buildSource, /buildLinuxUpdateHelper\(\);/);
  assert.match(buildSource, /buildMacosUpdateHelper\(\);/);
  assert.match(buildSource, /--update-helper-only/);
  assert.match(helperHarness, /fast-update suppression/);
  assert.match(helperHarness, /slow-update visibility/);
  assert.match(helperHarness, /--probe-mode/);
  assert.match(helperHarness, /preview lifecycle events/);
  assert.match(helperHarness, /native window construction/);
  assert.match(helperWorkflow, /ubuntu-latest/);
  assert.match(helperWorkflow, /windows-latest/);
  assert.match(helperWorkflow, /macos-14/);
  assert.match(helperWorkflow, /xvfb-run -a node scripts\/update_helper_harness\.js --gui-smoke/);
  assert.match(helperWorkflow, /runner\.os != 'Linux'[\s\S]*--gui-smoke/);
  assert.match(packageAction, /win-unpacked\/resources\/desktop\/update-helper\/BreakTwentyUpdateHelper\.exe/);
  assert.match(packageAction, /linux-unpacked\/resources\/desktop\/update-helper\/BreakTwentyUpdateHelper/);
  assert.match(packageAction, /packaged macOS update helper/);
  assert.match(packageAction, /BreakTwenty\.UpdateHelper\.Mark\.png/);
  assert.match(packageAction, /Get-AuthenticodeSignature/);
});
