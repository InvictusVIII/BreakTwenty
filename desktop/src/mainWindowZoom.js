const CANONICAL_MAIN_WINDOW_PHYSICAL_WIDTH = 1920;
const CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO = 0.932421863079071;
const MIN_SURFACED_LABEL_DEVICE_PIXELS = 10;
const SURFACED_LABEL_CSS_PIXELS = 14;

function calculateMainWindowMinimumZoomFactor({
  displayScaleFactor,
  minimumZoomFactor,
}) {
  const normalizedDisplayScaleFactor = Number.isFinite(displayScaleFactor) && displayScaleFactor > 0
    ? displayScaleFactor
    : 1;
  const normalizedMinimumZoomFactor = Number.isFinite(minimumZoomFactor) && minimumZoomFactor > 0
    ? minimumZoomFactor
    : 0;
  const labelRasterZoomFloor = MIN_SURFACED_LABEL_DEVICE_PIXELS
    / (SURFACED_LABEL_CSS_PIXELS * normalizedDisplayScaleFactor);

  return Math.max(normalizedMinimumZoomFactor, labelRasterZoomFloor);
}

function calculateMainWindowActualSizeZoomFactor({
  availableWidth,
  displayScaleFactor = 1,
  minimumZoomFactor,
  maximumZoomFactor,
}) {
  const proportionalZoomFactor = CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO
    * availableWidth
    / CANONICAL_MAIN_WINDOW_PHYSICAL_WIDTH;
  const effectiveMinimumZoomFactor = calculateMainWindowMinimumZoomFactor({
    displayScaleFactor,
    minimumZoomFactor,
  });

  return Math.min(
    maximumZoomFactor,
    Math.max(effectiveMinimumZoomFactor, proportionalZoomFactor),
  );
}

module.exports = {
  CANONICAL_MAIN_WINDOW_EFFECTIVE_PIXEL_RATIO,
  CANONICAL_MAIN_WINDOW_PHYSICAL_WIDTH,
  MIN_SURFACED_LABEL_DEVICE_PIXELS,
  SURFACED_LABEL_CSS_PIXELS,
  calculateMainWindowActualSizeZoomFactor,
  calculateMainWindowMinimumZoomFactor,
};
