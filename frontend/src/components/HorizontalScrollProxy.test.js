import React from 'react';
import {
  act,
  cleanup,
  fireEvent,
  render,
} from '@testing-library/react';
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import HorizontalScrollProxy from './HorizontalScrollProxy';

const DIRECT_OPTIONS = {
  className: 'test-horizontal-proxy',
  innerClassName: 'test-horizontal-proxy-inner',
  contentWidthProperty: '--test-horizontal-content-width',
  targetViewportProperty: '--test-horizontal-viewport-width',
  targetScrollProperty: '--test-horizontal-scroll-left',
  resetOnTargetWidthChange: true,
  clampTolerance: 0,
  resolveTarget: (controller) => controller.closest('[data-horizontal-target]'),
  getContentElements: ({ target }) => [target.querySelector('[data-horizontal-content]')],
};

const PROPORTIONAL_OPTIONS = {
  ...DIRECT_OPTIONS,
  mapping: 'proportional',
  controllerMeasurementProperty: '--test-horizontal-content-width',
  getContentWidth: ({ target }) => target.scrollWidth,
  getVisibilityBuffer: () => 10,
  getMinimumControllerRange: () => 200,
};

const DEFERRED_OPTIONS = {
  ...DIRECT_OPTIONS,
  resetOnTargetWidthChange: false,
  deferTargetScrollSync: true,
};

class TestResizeObserver {
  static instances = [];

  constructor(callback) {
    this.callback = callback;
    this.observed = [];
    this.disconnected = false;
    TestResizeObserver.instances.push(this);
  }

  observe(element) {
    this.observed.push(element);
  }

  disconnect() {
    this.disconnected = true;
  }

  trigger() {
    this.callback();
  }
}

let clientWidthDescriptor;
let scrollWidthDescriptor;
let visualViewportDescriptor;
let visualViewport;
let animationFrames;
let nextAnimationFrameId;

function numberFromData(element, key) {
  const value = Number(element.dataset?.[key]);
  return Number.isFinite(value) ? value : null;
}

function flushAnimationFrames() {
  const callbacks = [...animationFrames.values()];
  animationFrames.clear();
  act(() => {
    callbacks.forEach((callback) => callback(0));
  });
}

function renderProxy(options, { targetWidth = 100, contentWidth = 300 } = {}) {
  return render(
    <div data-horizontal-target data-client-width={targetWidth} data-scroll-width={contentWidth}>
      <div data-horizontal-content data-scroll-width={contentWidth} data-rect-width={contentWidth} />
      <HorizontalScrollProxy options={options} />
    </div>,
  );
}

beforeEach(() => {
  TestResizeObserver.instances = [];
  vi.stubGlobal('ResizeObserver', TestResizeObserver);

  clientWidthDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth');
  scrollWidthDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollWidth');
  Object.defineProperty(Element.prototype, 'clientWidth', {
    configurable: true,
    get() {
      const dataWidth = numberFromData(this, 'clientWidth');
      if (dataWidth !== null) return dataWidth;
      return this.classList?.contains('test-horizontal-proxy') ? 100 : 0;
    },
  });
  Object.defineProperty(Element.prototype, 'scrollWidth', {
    configurable: true,
    get() {
      const dataWidth = numberFromData(this, 'scrollWidth');
      if (dataWidth !== null) return dataWidth;
      if (this.classList?.contains('test-horizontal-proxy')) {
        return Number.parseFloat(this.style.getPropertyValue('--test-horizontal-content-width')) || 100;
      }
      return this.clientWidth;
    },
  });
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function getRect() {
    const width = numberFromData(this, 'rectWidth') ?? this.clientWidth;
    return {
      bottom: 0,
      height: 0,
      left: 0,
      right: width,
      top: 0,
      width,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    };
  });

  animationFrames = new Map();
  nextAnimationFrameId = 1;
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
    const frameId = nextAnimationFrameId;
    nextAnimationFrameId += 1;
    animationFrames.set(frameId, callback);
    return frameId;
  });
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation((frameId) => {
    animationFrames.delete(frameId);
  });

  visualViewport = new EventTarget();
  visualViewportDescriptor = Object.getOwnPropertyDescriptor(window, 'visualViewport');
  Object.defineProperty(window, 'visualViewport', {
    configurable: true,
    value: visualViewport,
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (clientWidthDescriptor) {
    Object.defineProperty(Element.prototype, 'clientWidth', clientWidthDescriptor);
  } else {
    delete Element.prototype.clientWidth;
  }
  if (scrollWidthDescriptor) {
    Object.defineProperty(Element.prototype, 'scrollWidth', scrollWidthDescriptor);
  } else {
    delete Element.prototype.scrollWidth;
  }
  if (visualViewportDescriptor) {
    Object.defineProperty(window, 'visualViewport', visualViewportDescriptor);
  } else {
    delete window.visualViewport;
  }
});

describe('HorizontalScrollProxy', () => {
  it('measures, clamps, syncs directly, and resets when the target width changes', () => {
    const view = renderProxy(DIRECT_OPTIONS);
    const target = view.container.querySelector('[data-horizontal-target]');
    const proxy = view.container.querySelector('.test-horizontal-proxy');
    const observer = TestResizeObserver.instances[0];

    expect(proxy).toHaveClass('is-visible');
    expect(proxy.style.getPropertyValue('--test-horizontal-content-width')).toBe('300px');
    expect(target.style.getPropertyValue('--test-horizontal-viewport-width')).toBe('100px');
    expect(observer.observed).toEqual(expect.arrayContaining([
      target,
      view.container.querySelector('[data-horizontal-content]'),
    ]));

    target.scrollLeft = 40;
    fireEvent.scroll(target);
    flushAnimationFrames();
    expect(proxy.scrollLeft).toBe(40);
    expect(target.style.getPropertyValue('--test-horizontal-scroll-left')).toBe('40px');

    proxy.scrollLeft = 260;
    fireEvent.scroll(proxy);
    flushAnimationFrames();
    expect(target.scrollLeft).toBe(200);
    expect(proxy.scrollLeft).toBe(200);

    target.scrollLeft = 60;
    proxy.scrollLeft = 60;
    target.dataset.clientWidth = '150';
    act(() => observer.trigger());
    expect(target.scrollLeft).toBe(0);
    expect(proxy.scrollLeft).toBe(0);
    expect(target.style.getPropertyValue('--test-horizontal-viewport-width')).toBe('150px');
  });

  it('preserves proportional mapping when the controller has a minimum scroll range', () => {
    const view = renderProxy(PROPORTIONAL_OPTIONS, { targetWidth: 100, contentWidth: 180 });
    const target = view.container.querySelector('[data-horizontal-target]');
    const proxy = view.container.querySelector('.test-horizontal-proxy');

    expect(proxy).toHaveClass('is-visible');
    expect(proxy.style.getPropertyValue('--test-horizontal-content-width')).toBe('300px');

    target.scrollLeft = 40;
    fireEvent.scroll(target);
    flushAnimationFrames();
    expect(proxy.scrollLeft).toBe(100);

    proxy.scrollLeft = 200;
    fireEvent.scroll(proxy);
    flushAnimationFrames();
    expect(target.scrollLeft).toBe(80);
  });

  it('defers target sync, preserves scroll on resize, and removes runtime ownership on cleanup', () => {
    const view = renderProxy(DEFERRED_OPTIONS);
    const target = view.container.querySelector('[data-horizontal-target]');
    const proxy = view.container.querySelector('.test-horizontal-proxy');
    const observer = TestResizeObserver.instances[0];
    const targetRemoveListener = vi.spyOn(target, 'removeEventListener');
    const proxyRemoveListener = vi.spyOn(proxy, 'removeEventListener');
    const visualViewportRemoveListener = vi.spyOn(visualViewport, 'removeEventListener');

    target.scrollLeft = 50;
    fireEvent.scroll(target);
    expect(proxy.scrollLeft).toBe(0);
    flushAnimationFrames();
    expect(proxy.scrollLeft).toBe(50);

    target.dataset.clientWidth = '150';
    act(() => observer.trigger());
    expect(target.scrollLeft).toBe(50);
    expect(proxy.scrollLeft).toBe(50);

    proxy.scrollLeft = 90;
    fireEvent.scroll(proxy);
    const pendingFrameId = [...animationFrames.keys()][0];
    view.unmount();

    expect(window.cancelAnimationFrame).toHaveBeenCalledWith(pendingFrameId);
    expect(observer.disconnected).toBe(true);
    expect(target.style.getPropertyValue('--test-horizontal-viewport-width')).toBe('');
    expect(target.style.getPropertyValue('--test-horizontal-scroll-left')).toBe('');
    expect(targetRemoveListener).toHaveBeenCalledWith('scroll', expect.any(Function));
    expect(proxyRemoveListener).toHaveBeenCalledWith('scroll', expect.any(Function));
    expect(visualViewportRemoveListener).toHaveBeenCalledWith('resize', expect.any(Function));
  });
});
