import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useRightTrayReservation, useTheme } from '../appState';
import { API } from '../config';
import { APP_BRAND_NAME } from '../constants/brand';
import { DEFAULT_USER_TIMEZONE, getAvailableTimezones } from '../utils/timezone';
import { getBrandImageAssets } from '../utils/brandImageAssets';
import { brandAssetVersions } from '../generatedBrandAssets';
import BrandName, { renderBrandText } from './BrandName';
import Dropdown from './Dropdown';
import { WELCOME_TOUR_STEPS } from './welcomeTourSteps';
import { installTourDemoFetch, setTourDemoActive, setTourHint } from './tourDemoData';
import './WelcomeModal.css';

// Install the demo-data fetch shim once (a no-op passthrough until the tour activates it).
installTourDemoFetch();

// Coach-card geometry (keep CARD_WIDTH in sync with .welcome-coach width in the CSS).
const CARD_WIDTH = 360;
const CARD_GAP = 28;
const VIEWPORT_PAD = 16;
const EST_CARD_HEIGHT = 240;
const SPOTLIGHT_PAD = 6;
const SPOTLIGHT_EDGE_PAD = 2;
// First-run onboarding. A centered intro, then a spotlight tour that, for each step, drives
// the real router to that page and darkens only the sidebar around the spotlight; the coach
// card describes + points at the rail button. While the tour is open,
// "demo mode" feeds every page a small dummy dataset (see tourDemoData.js) so the pages look
// populated even on a brand-new account; it deactivates on finish and the real data is
// refetched. Finishing drops the user into adding their first account and marks onboarding
// complete server-side so it never auto-reappears. Re-openable via the dev trigger in App.js.
function WelcomeModal({
  onComplete,
  onAddAccount,
  onRefreshData,
  onSaveDefaults,
  primaryCurrency = 'CAD',
  currencyOptions = [],
  userTimezone = DEFAULT_USER_TIMEZONE,
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const { mode: themeMode } = useTheme();
  const [stepIndex, setStepIndex] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [rect, setRect] = useState(null);
  const [railRect, setRailRect] = useState(null);
  const [mainRect, setMainRect] = useState(null);
  const [selectedTimezoneOverride, setSelectedTimezoneOverride] = useState(null);
  const [selectedCurrencyOverride, setSelectedCurrencyOverride] = useState(null);

  const current = WELCOME_TOUR_STEPS[stepIndex];
  const isFirst = stepIndex === 0;
  const isLast = stepIndex === WELCOME_TOUR_STEPS.length - 1;
  const tourSteps = WELCOME_TOUR_STEPS.filter((s) => s.type === 'tour');
  const tourIndex = current.type === 'tour' ? tourSteps.indexOf(current) : -1;
  const timezoneOptions = useMemo(
    () => getAvailableTimezones().map((timezone) => ({ value: timezone, label: timezone })),
    [],
  );
  const resolvedCurrencyOptions = useMemo(
    () => (currencyOptions.length > 0 ? currencyOptions : ['CAD'])
      .map((currency) => ({ value: currency, label: currency })),
    [currencyOptions],
  );
  const selectedTimezone = selectedTimezoneOverride || userTimezone || DEFAULT_USER_TIMEZONE;
  const selectedCurrency = selectedCurrencyOverride || primaryCurrency || 'CAD';
  const welcomeBrandAssets = getBrandImageAssets(themeMode, brandAssetVersions);

  // Hold a right-tray reservation alive across consecutive tray steps (Income → Transactions)
  // so the work area doesn't bounce wide-then-narrow while one page's tray unmounts before the
  // next page's tray opens — that bounce is the step-5→6 flicker.
  useRightTrayReservation('welcome-tour-bridge', current.type === 'tour' && Boolean(current.hasTray));

  // Turn demo data on for the life of the tour and repopulate the app immediately; the
  // page-level effects keyed on the central data then cascade the demo into each page. This
  // MUST run once on mount and clean up once on unmount — depending on `onRefreshData` would
  // re-run it whenever App's `fetchData` identity changes (it does, as its deps update), which
  // re-activated demo mode right after Skip/Finish turned it off, so the real data never
  // returned. The ref keeps the callback current without making it a dependency.
  const onRefreshDataRef = useRef(onRefreshData);
  useEffect(() => {
    onRefreshDataRef.current = onRefreshData;
  }, [onRefreshData]);
  useEffect(() => {
    setTourDemoActive(true);
    if (onRefreshDataRef.current) onRefreshDataRef.current();
    return () => { setTourDemoActive(false); setTourHint(null); };
  }, []);

  const goNext = useCallback(
    () => setStepIndex((i) => Math.min(i + 1, WELCOME_TOUR_STEPS.length - 1)),
    [],
  );
  const goBack = useCallback(() => setStepIndex((i) => Math.max(i - 1, 0)), []);
  const saveDefaults = useCallback(async () => {
    if (!onSaveDefaults) return;
    await onSaveDefaults({
      timezone: selectedTimezone,
      primaryCurrency: selectedCurrency,
    });
  }, [onSaveDefaults, selectedCurrency, selectedTimezone]);

  const startTour = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    try {
      await saveDefaults();
      setSubmitting(false);
      goNext();
    } catch (e) {
      setError(e?.message || 'Something went wrong');
      setSubmitting(false);
    }
  }, [goNext, saveDefaults]);

  const complete = useCallback(async ({ thenAdd = false } = {}) => {
    setSubmitting(true);
    setError(null);
    try {
      await saveDefaults();
      const resp = await fetch(`${API}/onboarding/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!resp.ok) {
        const b = await resp.json().catch(() => ({}));
        throw new Error(b?.detail || 'Something went wrong');
      }
      setTourDemoActive(false); // back to real data before the refetch + close
      setTourHint(null);
      navigate('/');
      if (thenAdd && onAddAccount) onAddAccount();
      if (onComplete) await onComplete();
    } catch (e) {
      setError(e?.message || 'Something went wrong');
      setSubmitting(false);
    }
  }, [navigate, onAddAccount, onComplete, saveDefaults]);

  // Drive the real router to each tour step's page so the live (demo-populated) page shows, and
  // publish any page hint before navigating. useLayoutEffect (not useEffect) so the route change
  // commits in the SAME paint as the card/spotlight update — otherwise the old page shows for a
  // frame under the new step's card.
  useLayoutEffect(() => {
    if (current.type === 'tour') {
      setTourHint(current.hint || null);
      navigate(current.targetRoute);
    } else {
      setTourHint(null);
    }
  }, [current, navigate]);

  useLayoutEffect(() => {
    if (current.type !== 'tour' || !Number.isFinite(current.scrollTop)) {
      return undefined;
    }

    let frameId = null;
    let secondFrameId = null;
    const applyScroll = () => {
      const main = document.querySelector('.app-main');
      if (main) {
        main.scrollTo({ top: current.scrollTop, behavior: 'auto' });
      } else {
        window.scrollTo({ top: current.scrollTop, behavior: 'auto' });
      }
    };

    frameId = window.requestAnimationFrame(() => {
      applyScroll();
      secondFrameId = window.requestAnimationFrame(applyScroll);
    });

    return () => {
      if (frameId != null) window.cancelAnimationFrame(frameId);
      if (secondFrameId != null) window.cancelAnimationFrame(secondFrameId);
    };
  }, [current, location.pathname]);

  useEffect(() => {
    if (current.type === 'tour' && onRefreshDataRef.current) {
      onRefreshDataRef.current();
    }
  }, [current]);

  // Measure the spotlight target (the rail button). Re-measures after the route settles
  // (location dep) and on the next frame, so the rect tracks the active-state layout exactly.
  useLayoutEffect(() => {
    let cancelled = false;
    if (current.type !== 'tour') {
      const raf = window.requestAnimationFrame(() => {
        if (!cancelled) {
          setRect(null);
          setRailRect(null);
          setMainRect(null);
        }
      });
      return () => {
        cancelled = true;
        window.cancelAnimationFrame(raf);
      };
    }
    const measure = () => {
      if (cancelled) return;
      const el = document.querySelector(`[data-tour-id="${current.targetRoute}"]`);
      const rail = document.querySelector('.floating-nav-rail');
      const main = document.querySelector('.app-main');
      setRect(el ? el.getBoundingClientRect() : null);
      setRailRect(rail ? rail.getBoundingClientRect() : null);
      setMainRect(main ? main.getBoundingClientRect() : null);
    };
    const raf = window.requestAnimationFrame(measure);
    window.addEventListener('resize', measure);
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(raf);
      window.removeEventListener('resize', measure);
    };
  }, [current, location.pathname]);

  // Keyboard: Esc skips, arrows move between steps.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') complete();
      else if (e.key === 'ArrowRight' && !isLast) {
        if (current.type === 'intro') startTour();
        else goNext();
      }
      else if (e.key === 'ArrowLeft' && !isFirst) goBack();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [complete, current.type, goNext, goBack, isFirst, isLast, startTour]);

  // Let the user scroll the page behind the tour to see content below the fold (the
  // full-screen click-blocker otherwise swallows wheel events).
  const handleTourWheel = useCallback((e) => {
    const main = document.querySelector('.app-main');
    if (main) main.scrollTop += e.deltaY;
    else window.scrollBy(0, e.deltaY);
  }, []);

  const skipButton = (
    <button type="button" className="welcome-skip button-shell-opt-out" disabled={submitting} onClick={() => complete()}>
      Skip tour
    </button>
  );

  if (current.type === 'intro') {
    return (
      <div className="modal-overlay welcome-overlay" role="dialog" aria-modal="true" aria-label={`Welcome to ${APP_BRAND_NAME}`}>
        <div className="modal-content welcome-modal welcome-intro" onClick={(e) => e.stopPropagation()}>
          <div className="welcome-header">
            <img
              className="welcome-mark"
              src={welcomeBrandAssets.mark.src}
              alt=""
              aria-hidden="true"
              draggable="false"
            />
            <h2 className="welcome-title welcome-title-with-wordmark">
              <span>Welcome to</span>
              <img
                className="welcome-title-wordmark"
                src={welcomeBrandAssets.wordmark.src}
                srcSet={welcomeBrandAssets.wordmark.srcSet}
                alt={APP_BRAND_NAME}
                draggable="false"
              />
            </h2>
            <p className="welcome-tagline">See clearly. Move deliberately!</p>
            <p className="welcome-subtitle">Remember when breaking a twenty-dollar bill at the store meant turning it into smaller, more useful money, enough for the decent shopping and the leftover change in your pocket?</p>
            <p className="welcome-subtitle welcome-subtitle-primary"><BrandName /> works the same way with your financial life: it consolidates your <span className="welcome-subtitle-emphasis">assets/liabilities</span> in one place, then breaks everything down into <span className="welcome-subtitle-emphasis">clear, useful pieces</span> so you can see how your everyday choices move you closer to the life you’re building!</p>
            <p className="welcome-subtitle welcome-subtitle-faint"><BrandName /> currently supports most major Canadian financial institutions.</p>
          </div>
          <div className="welcome-body">
            <div className="welcome-defaults" aria-label="Starting defaults">
              <div className="welcome-default-row">
                <span className="welcome-default-label">Timezone</span>
                <Dropdown
                  className="welcome-default-dropdown"
                  value={selectedTimezone}
                  disabled={submitting}
                  options={timezoneOptions}
                  ariaLabel="Timezone"
                  onChange={setSelectedTimezoneOverride}
                  maxHeight={240}
                />
              </div>
              <div className="welcome-default-row">
                <span className="welcome-default-label">Primary Currency</span>
                <Dropdown
                  className="welcome-default-dropdown currency-dropdown"
                  value={selectedCurrency}
                  disabled={submitting}
                  options={resolvedCurrencyOptions}
                  ariaLabel="Primary Currency"
                  onChange={setSelectedCurrencyOverride}
                />
              </div>
            </div>
            {error && <p className="modal-error-block">{error}</p>}
            <div className="welcome-actions">
              {skipButton}
              <button type="button" className="btn-primary app-control-root" disabled={submitting} onClick={startTour}>
                <span className="app-control-label">{submitting ? 'Saving…' : 'Take the tour'}</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (current.type === 'finish') {
    return (
      <div className="modal-overlay welcome-overlay" role="dialog" aria-modal="true" aria-label="You are all set">
        <div className="modal-content welcome-modal welcome-finish" onClick={(e) => e.stopPropagation()}>
          <div className="welcome-header">
            <img
              className="welcome-mark"
              src={welcomeBrandAssets.mark.src}
              alt=""
              aria-hidden="true"
              draggable="false"
            />
            <h2 className="welcome-title">You're all set!</h2>
            <p className="welcome-subtitle">Hope you enjoy using <BrandName />. Add your first account and start building a clearer picture of your finances.</p>
          </div>
          <div className="welcome-body">
            {error && <p className="modal-error-block">{error}</p>}
            <div className="welcome-actions">
              <button type="button" className="btn-secondary app-control-root" disabled={submitting} onClick={() => complete()}>
                <span className="app-control-label">Explore on my own</span>
              </button>
              <button type="button" className="btn-primary app-control-root" disabled={submitting} onClick={() => complete({ thenAdd: true })}>
                <span className="app-control-label">{submitting ? 'Saving…' : 'Add your first account'}</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Tour step: spotlight the rail button + float a coach card beside it.
  const spotlightBox = rect
    ? (() => {
      const rawTop = rect.top - SPOTLIGHT_PAD;
      const rawLeft = rect.left - SPOTLIGHT_PAD;
      const top = Math.max(SPOTLIGHT_EDGE_PAD, rawTop);
      const left = Math.max(SPOTLIGHT_EDGE_PAD, rawLeft);
      return {
        top,
        left,
        width: rect.width + SPOTLIGHT_PAD * 2 - Math.max(0, left - rawLeft),
        height: rect.height + SPOTLIGHT_PAD * 2 - Math.max(0, top - rawTop),
      };
    })()
    : null;
  const spotlightStyle = spotlightBox
    ? { top: spotlightBox.top, left: spotlightBox.left, width: spotlightBox.width, height: spotlightBox.height }
    : null;
  const pageScrimStyle = mainRect
    ? { top: mainRect.top, left: mainRect.left, width: mainRect.width, height: mainRect.height }
    : null;
  const sidebarScrims = railRect && spotlightBox
    ? (() => {
      const rail = {
        top: railRect.top,
        right: railRect.right,
        bottom: railRect.bottom,
        left: railRect.left,
      };
      const hole = {
        top: Math.max(rail.top, spotlightBox.top),
        right: Math.min(rail.right, spotlightBox.left + spotlightBox.width),
        bottom: Math.min(rail.bottom, spotlightBox.top + spotlightBox.height),
        left: Math.max(rail.left, spotlightBox.left),
      };
      return [
        { key: 'top', top: rail.top, left: rail.left, width: rail.right - rail.left, height: Math.max(0, hole.top - rail.top) },
        { key: 'bottom', top: hole.bottom, left: rail.left, width: rail.right - rail.left, height: Math.max(0, rail.bottom - hole.bottom) },
        { key: 'left', top: hole.top, left: rail.left, width: Math.max(0, hole.left - rail.left), height: Math.max(0, hole.bottom - hole.top) },
        { key: 'right', top: hole.top, left: hole.right, width: Math.max(0, rail.right - hole.right), height: Math.max(0, hole.bottom - hole.top) },
      ].filter((item) => item.width > 0 && item.height > 0);
    })()
    : [];

  let coachClass = 'welcome-coach';
  let coachStyle;
  let scrollMaxHeight;
  if (rect) {
    const top = Math.max(VIEWPORT_PAD, Math.min(rect.top, window.innerHeight - EST_CARD_HEIGHT - VIEWPORT_PAD));
    let left = rect.right + CARD_GAP;
    let pointSide = 'left';
    if (left + CARD_WIDTH > window.innerWidth - VIEWPORT_PAD) {
      left = Math.max(VIEWPORT_PAD, rect.left - CARD_WIDTH - CARD_GAP);
      pointSide = 'right';
    }
    // Aim the beak at the button's vertical center, measured from the card's top.
    const arrowTop = Math.max(16, Math.min(rect.top + rect.height / 2 - top, EST_CARD_HEIGHT - 16));
    coachClass += pointSide === 'left' ? ' point-left' : ' point-right';
    coachStyle = { top, left, '--welcome-arrow-top': `${arrowTop}px` };
    // Bound the card to the space from its top to the viewport bottom so tall content scrolls.
    scrollMaxHeight = `${Math.max(180, window.innerHeight - top - VIEWPORT_PAD)}px`;
  } else {
    // Fallback to a centered card if the target button can't be located.
    coachStyle = { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' };
    scrollMaxHeight = `calc(100vh - ${2 * VIEWPORT_PAD}px)`;
  }

  return (
    <div className="welcome-tour" role="dialog" aria-modal="true" aria-label={current.title}>
      <div className="welcome-tour-blocker" aria-hidden="true" onWheel={handleTourWheel} />
      {pageScrimStyle && <div className="welcome-tour-page-scrim" style={pageScrimStyle} aria-hidden="true" />}
      {sidebarScrims.map(({ key, ...style }) => (
        <div key={key} className="welcome-tour-sidebar-scrim" style={style} aria-hidden="true" />
      ))}
      {spotlightStyle && <div className="welcome-tour-spotlight" style={spotlightStyle} aria-hidden="true" />}
      <div className={coachClass} style={coachStyle}>
        <div className="welcome-coach-scroll" style={{ maxHeight: scrollMaxHeight }}>
          <span className="welcome-coach-step">Step {tourIndex + 1} of {tourSteps.length}</span>
          <h2 className="welcome-coach-title">{current.title}</h2>
          <p className="welcome-coach-desc">{renderBrandText(current.description, `${current.targetRoute}-description`)}</p>
          {current.note && (
            <p className={`welcome-coach-desc ${current.noteTone === 'primary' ? 'is-primary' : ''}`.trim()}>
              {renderBrandText(current.note, `${current.targetRoute}-note`)}
            </p>
          )}
          {current.noteFollowup && (
            <p className={`welcome-coach-desc ${current.noteTone === 'primary' ? 'is-primary' : ''}`.trim()}>
              {renderBrandText(current.noteFollowup, `${current.targetRoute}-note-followup`)}
            </p>
          )}
          {error && <p className="modal-error-block">{error}</p>}
          <div className="welcome-coach-foot">
            <div className="welcome-dots" aria-hidden="true">
              {tourSteps.map((s, i) => (
                <span key={s.targetRoute} className={`welcome-dot ${i === tourIndex ? 'is-active' : ''}`.trim()} />
              ))}
            </div>
            <div className="welcome-coach-actions">
              {skipButton}
              <button type="button" className="btn-secondary app-control-root" onClick={goBack}>
                <span className="app-control-label">Back</span>
              </button>
              <button type="button" className="btn-primary app-control-root" onClick={goNext}>
                <span className="app-control-label">{tourIndex === tourSteps.length - 1 ? 'Finish' : 'Next'}</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default WelcomeModal;
