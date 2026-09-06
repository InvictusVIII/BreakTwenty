import React, { useCallback, useMemo, useRef, useState } from 'react';
import { MdPalette } from 'react-icons/md';
import useDismissibleLayer from '../hooks/useDismissibleLayer';
import {
  clampColorValue,
  DEFAULT_PORTFOLIO_NET_WORTH_COLOR,
  hexToRgb,
  hsvToHex,
  normalizeChartColor,
  rgbToHsv,
} from '../utils/portfolioViewUtils';

export function ChartColorPopover({ title, children, className = '' }) {
  const [isOpen, setIsOpen] = useState(false);
  const popoverRef = useRef(null);
  const closePopover = useCallback(() => setIsOpen(false), []);

  useDismissibleLayer({
    open: isOpen,
    ref: popoverRef,
    onDismiss: closePopover,
  });

  return (
    <div className={`chart-color-popover ${isOpen ? 'is-open' : ''} ${className}`.trim()} ref={popoverRef}>
      <button
        type="button"
        className="chart-color-trigger app-control-root"
        aria-label={`${title} colors`}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        onClick={() => setIsOpen((previous) => !previous)}
      >
        <span className="app-control-icon" aria-hidden="true"><MdPalette /></span>
      </button>
      <div
        className={`chart-color-panel ${isOpen ? 'is-open' : ''}`.trim()}
        role="dialog"
        aria-label={`${title} colors`}
        aria-hidden={!isOpen}
      >
        {isOpen && (
          <>
            <div className="chart-color-panel-title">{title}</div>
            <div className="chart-color-list">{children}</div>
          </>
        )}
      </div>
    </div>
  );
}

function ChartSpectrumPicker({ color, onChange }) {
  const saturationRef = useRef(null);
  const hueRef = useRef(null);
  const normalizedColor = normalizeChartColor(color, DEFAULT_PORTFOLIO_NET_WORTH_COLOR);
  const hsv = useMemo(() => rgbToHsv(hexToRgb(normalizedColor)), [normalizedColor]);
  const hueColor = hsvToHex(hsv.h, 1, 1);

  const updateSaturation = useCallback((event) => {
    const rect = saturationRef.current?.getBoundingClientRect();
    if (!rect) return;
    const saturation = clampColorValue((event.clientX - rect.left) / rect.width);
    const value = clampColorValue(1 - ((event.clientY - rect.top) / rect.height));
    onChange(hsvToHex(hsv.h, saturation, value));
  }, [hsv.h, onChange]);

  const updateHue = useCallback((event) => {
    const rect = hueRef.current?.getBoundingClientRect();
    if (!rect) return;
    const hue = clampColorValue((event.clientX - rect.left) / rect.width) * 360;
    onChange(hsvToHex(hue, hsv.s, hsv.v));
  }, [hsv.s, hsv.v, onChange]);

  const handleSaturationPointerDown = (event) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    updateSaturation(event);
  };

  const handleHuePointerDown = (event) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    updateHue(event);
  };

  return (
    <div className="chart-spectrum-picker">
      <div
        ref={saturationRef}
        className="chart-spectrum"
        style={{ '--picker-hue-color': hueColor }}
        role="application"
        aria-label="Color shade picker"
        onPointerDown={handleSaturationPointerDown}
        onPointerMove={(event) => {
          if (event.buttons === 1) updateSaturation(event);
        }}
      >
        <span
          className="chart-spectrum-thumb"
          style={{
            left: `${hsv.s * 100}%`,
            top: `${(1 - hsv.v) * 100}%`,
            '--selected-chart-color': normalizedColor,
          }}
          aria-hidden="true"
        />
      </div>
      <div
        ref={hueRef}
        className="chart-hue-slider"
        role="slider"
        tabIndex={0}
        aria-label="Hue"
        aria-valuemin={0}
        aria-valuemax={360}
        aria-valuenow={Math.round(hsv.h)}
        onPointerDown={handleHuePointerDown}
        onPointerMove={(event) => {
          if (event.buttons === 1) updateHue(event);
        }}
      >
        <span
          className="chart-hue-thumb"
          style={{
            left: `${(hsv.h / 360) * 100}%`,
            '--selected-chart-color': hueColor,
          }}
          aria-hidden="true"
        />
      </div>
    </div>
  );
}

export function ChartColorRow({ label, color, defaultColor, onApply, onReset }) {
  const value = normalizeChartColor(color, defaultColor) || DEFAULT_PORTFOLIO_NET_WORTH_COLOR;
  const [draftColor, setDraftColor] = useState(value);
  const [isEditorOpen, setIsEditorOpen] = useState(false);

  const normalizedDraftColor = normalizeChartColor(draftColor);
  const normalizedDefaultColor = normalizeChartColor(defaultColor);
  const previewColor = isEditorOpen ? (normalizedDraftColor || value) : value;
  const canApply = Boolean(isEditorOpen && normalizedDraftColor && normalizedDraftColor !== value);

  const handleApply = () => {
    if (normalizedDraftColor && normalizedDraftColor === normalizedDefaultColor) {
      onReset();
    } else if (normalizedDraftColor) {
      onApply(normalizedDraftColor);
    }
    setIsEditorOpen(false);
  };

  return (
    <div className={`chart-color-row ${isEditorOpen ? 'is-expanded' : ''}`.trim()}>
      <div className="chart-color-row-header">
        <span className="chart-color-row-label" title={label}>{label}</span>
        <div className="chart-color-row-actions">
          <button
            type="button"
            className="chart-color-square"
            style={{ '--selected-chart-color': previewColor }}
            aria-label={`Edit ${label} color`}
            aria-expanded={isEditorOpen}
            onClick={() => {
              setDraftColor(value);
              setIsEditorOpen((previous) => !previous);
            }}
          />
          <button
            type="button"
            className="chart-color-reset app-control-root"
            onClick={() => {
              setDraftColor(defaultColor);
              setIsEditorOpen(true);
            }}
          >
            <span className="app-control-label">Default</span>
          </button>
        </div>
      </div>
      {isEditorOpen && (
        <div className="chart-color-picker">
          <ChartSpectrumPicker color={previewColor} onChange={setDraftColor} />
          <div className="chart-color-editor">
            <span
              className="chart-color-preview"
              style={{ '--selected-chart-color': previewColor }}
              aria-hidden="true"
            />
            <input
              className={`chart-color-hex${draftColor && !normalizedDraftColor ? ' is-invalid' : ''}`}
              type="text"
              value={draftColor}
              spellCheck={false}
              maxLength={7}
              aria-label={`${label} hex color`}
              onChange={(event) => setDraftColor(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && canApply) {
                  handleApply();
                }
              }}
            />
            <button
              type="button"
              className="btn-primary chart-color-apply app-control-root"
              onClick={handleApply}
              disabled={!canApply}
            >
              <span className="app-control-label">Apply</span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
