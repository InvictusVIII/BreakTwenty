import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import { APP_BRAND_NAME } from './constants/brand';
import { installApiTransport } from './utils/apiTransport';
import './controlSlots.css';

if (typeof window !== 'undefined') {
  window.addEventListener('error', (event) => {
    console.error(`${APP_BRAND_NAME} window error:`, event.error || event.message);
  });
  window.addEventListener('unhandledrejection', (event) => {
    console.error(`${APP_BRAND_NAME} unhandled rejection:`, event.reason);
  });
}

async function bootstrap() {
  await installApiTransport();
  // Import App only after transport installation so promo/demo wrapping composes on
  // top of the authenticated fetch boundary instead of retaining native fetch.
  const { default: App, BreakTwentyErrorBoundary } = await import('./App');
  const root = ReactDOM.createRoot(document.getElementById('root'));
  root.render(
    <React.StrictMode>
      <BreakTwentyErrorBoundary>
        <App />
      </BreakTwentyErrorBoundary>
    </React.StrictMode>
  );
}

void bootstrap().catch((error) => {
  console.error(`${APP_BRAND_NAME} startup failed:`, error);
  document.getElementById('root').textContent = `${APP_BRAND_NAME} could not establish its secure local session.`;
});
