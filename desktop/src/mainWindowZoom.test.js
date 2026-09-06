const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {
  CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO,
  CANONICAL_MAIN_WINDOW_PHYSICAL_WIDTH,
  MIN_SURFACED_LABEL_DEVICE_PIXELS,
  SURFACED_LABEL_CSS_PIXELS,
  calculateMainWindowActualSizeZoomFactor,
  calculateMainWindowMinimumZoomFactor,
} = require('./mainWindowZoom');

const MINIMUM_ZOOM_FACTOR = 0.5;
const MAXIMUM_ZOOM_FACTOR = 3;

function calculateZoom(availableWidth, displayScaleFactor = 1) {
  return calculateMainWindowActualSizeZoomFactor({
    availableWidth,
    displayScaleFactor,
    minimumZoomFactor: MINIMUM_ZOOM_FACTOR,
    maximumZoomFactor: MAXIMUM_ZOOM_FACTOR,
  });
}

test('uses the measured effective ratio from the canonical 1920-wide Dashboard view', () => {
  assert.equal(CANONICAL_MAIN_WINDOW_PHYSICAL_WIDTH, 1920);
  assert.equal(CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO, 0.932421863079071);
  assert.equal(calculateZoom(1920), CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO);
});

test('scales Actual Size proportionally while the surfaced-label raster floor is satisfied', () => {
  assert.equal(calculateZoom(2560), CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO * 2560 / 1920);
  assert.equal(calculateZoom(1920), CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO);
});

test('preserves the canonical horizontal CSS viewport above the surfaced-label raster floor', () => {
  const canonicalCssViewportWidth = 1920 / CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO;

  for (const availableWidth of [1920, 2560, 3840]) {
    assert.ok(Math.abs(availableWidth / calculateZoom(availableWidth) - canonicalCssViewportWidth) < 0.001);
  }
});

test('lets responsive layout take over instead of shrinking labels below ten device pixels', () => {
  const minimumZoomFactor = MIN_SURFACED_LABEL_DEVICE_PIXELS / SURFACED_LABEL_CSS_PIXELS;

  for (const availableWidth of [1180, 1280, 1366, 1440]) {
    assert.equal(calculateZoom(availableWidth), minimumZoomFactor);
    assert.ok(availableWidth / calculateZoom(availableWidth) < 1920 / CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO);
  }
});

test('logical display scaling and OS density produce the same physical proportion', () => {
  const canonicalOsScaleFactor = 1.098;
  const canonicalLogicalWidth = 1920 / canonicalOsScaleFactor;
  const canonicalEffectivePixelRatio = calculateZoom(canonicalLogicalWidth) * canonicalOsScaleFactor;

  assert.ok(
    Math.abs(canonicalEffectivePixelRatio - CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO)
      < Number.EPSILON,
  );

  const scaledPhysicalWidth = 2560;
  const scaledOsScaleFactor = 1.25;
  const scaledLogicalWidth = scaledPhysicalWidth / scaledOsScaleFactor;
  const scaledEffectivePixelRatio = calculateZoom(scaledLogicalWidth) * scaledOsScaleFactor;

  assert.ok(
    Math.abs(
      scaledEffectivePixelRatio / scaledPhysicalWidth
      - CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO / 1920,
    ) < Number.EPSILON,
  );
});

test('derives the zoom floor from surfaced-label size and display density', () => {
  assert.equal(MIN_SURFACED_LABEL_DEVICE_PIXELS, 10);
  assert.equal(SURFACED_LABEL_CSS_PIXELS, 14);
  assert.equal(
    calculateMainWindowMinimumZoomFactor({
      displayScaleFactor: 1,
      minimumZoomFactor: MINIMUM_ZOOM_FACTOR,
    }),
    10 / 14,
  );
  assert.equal(
    calculateMainWindowMinimumZoomFactor({
      displayScaleFactor: 2,
      minimumZoomFactor: MINIMUM_ZOOM_FACTOR,
    }),
    MINIMUM_ZOOM_FACTOR,
  );
});

test('keeps zoom safety limits outside the proportional display range', () => {
  assert.equal(calculateZoom(800), 10 / 14);
  assert.equal(calculateZoom(800, 2), MINIMUM_ZOOM_FACTOR);
  assert.equal(calculateZoom(8000), MAXIMUM_ZOOM_FACTOR);
});

test('rapid zoom requests cannot bypass the display-aware floor while saves are suppressed', () => {
  const mainSource = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');
  const handlerStart = mainSource.indexOf("mainWindowWebContents.on('zoom-changed'");
  const handlerEnd = mainSource.indexOf("createdMainWindow.on('close'", handlerStart);
  const handlerSource = mainSource.slice(handlerStart, handlerEnd);

  assert.ok(handlerStart >= 0);
  assert.ok(handlerEnd > handlerStart);
  assert.match(
    handlerSource,
    /const shouldPersistUserZoom = !suppressNextMainWindowZoomSave;/
  );
  assert.doesNotMatch(
    handlerSource,
    /if \(suppressNextMainWindowZoomSave\)\s*\{\s*return;/
  );
  assert.match(
    handlerSource,
    /const displaySafeZoomFactor = clampMainWindowZoomFactorToDisplay\([\s\S]*mainWindowWebContents\.setZoomFactor\(displaySafeZoomFactor\);/
  );
});
