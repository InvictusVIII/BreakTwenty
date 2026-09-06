import React, { useEffect, useState } from 'react';
import MarkdownView from '../components/MarkdownView';
import { APP_BRAND_NAME, APP_WEBSITE_URL } from '../constants/brand';
import './Settings.css';
import './Licenses.css';

const PUBLIC_BASE_URL = (import.meta.env.BASE_URL || '/').replace(/\/$/, '');
const LEGAL_LINKS = [
  { label: 'Privacy Policy', url: `${APP_WEBSITE_URL}/privacy/` },
  { label: 'Terms of Use', url: `${APP_WEBSITE_URL}/terms/` },
  { label: 'Security', url: `${APP_WEBSITE_URL}/security/` },
  {
    label: 'BreakTwenty Source-Available License 1.0',
    url: 'https://github.com/InvictusVIII/BreakTwenty/blob/main/LICENSE.md',
  },
];
const NOTICE_DOCUMENTS = [
  {
    label: 'THIRD_PARTY_SOFTWARE_NOTICES.md',
    url: `${PUBLIC_BASE_URL}/THIRD_PARTY_SOFTWARE_NOTICES.md`,
  },
  {
    label: 'THIRD-PARTY-LICENSES.md',
    url: `${PUBLIC_BASE_URL}/emoji/THIRD-PARTY-LICENSES.md`,
  },
];

// Renders the bundled software and asset notices inside the app. The files ship
// in the build (Vite public/ -> build/) and are fetched over the app's own
// origin, which is a local HTTP server in both development and packaged builds.
function Licenses() {
  const [state, setState] = useState({ status: 'loading', text: '' });

  useEffect(() => {
    let cancelled = false;
    Promise.all(NOTICE_DOCUMENTS.map(({ url }) => (
      fetch(url).then((response) => {
        if (!response.ok) throw new Error(`Failed to load licences (${response.status})`);
        return response.text();
      })
    )))
      .then((documents) => {
        if (!cancelled) setState({ status: 'ready', text: documents.join('\n\n---\n\n') });
      })
      .catch(() => { if (!cancelled) setState({ status: 'error', text: '' }); });
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="page-frame is-narrow settings-section licenses-page">
      <div className="panel-shell settings-card licenses-content-card">
        <section className="licenses-policies" aria-labelledby="breaktwenty-policies-title">
          <h2 id="breaktwenty-policies-title" className="settings-card-title">
            Legal &amp; Security
          </h2>
          <p className="settings-desc">
            Review the policies that apply to the current local desktop edition and
            the BreakTwenty website. These links open on breaktwenty.com or GitHub.
          </p>
          <p className="settings-desc">
            The current desktop edition keeps its financial database and saved
            authentication material on your device. The developer does not receive or
            control that local data, and diagnostics are shared only when you
            intentionally export and send them.
          </p>
          <ul className="settings-credits-list licenses-policy-links">
            {LEGAL_LINKS.map(({ label, url }) => (
              <li key={url}>
                <a href={url} target="_blank" rel="noreferrer">{label}</a>
              </li>
            ))}
          </ul>
        </section>
        <section className="licenses-overview" aria-label="Third-party licences summary">
          <h2 className="settings-card-title">Third-party Licences</h2>
          <p className="settings-desc">
            {APP_BRAND_NAME} bundles software dependencies, emoji, and icon artwork
            under their respective third-party licences. The required notices and
            full licence texts ship with the app. Those terms apply only to the
            identified third-party materials and do not make {APP_BRAND_NAME} itself
            open source; original {APP_BRAND_NAME} code is licensed separately under
            the BreakTwenty Source-Available License 1.0.
          </p>
          <ul className="settings-credits-list">
            <li><strong>Twemoji</strong> — © Twitter and contributors; graphics under CC-BY 4.0.</li>
            <li><strong>Noto Emoji</strong> — © Google; SVG resources under Apache-2.0, with the repository SIL OFL 1.1 notice preserved.</li>
            <li><strong>Fluent UI Emoji</strong> — © Microsoft; under MIT.</li>
            <li><strong>Finance icons</strong> — Icons8, Microsoft, spothq &amp; Material Design Icons via Iconify; under MIT, CC0-1.0 &amp; Apache-2.0.</li>
          </ul>
          <p className="settings-desc licenses-overview-note">
            Exchange rates are sourced from the Bank of Canada and the European
            Central Bank (via Frankfurter).
          </p>
        </section>
        {state.status === 'loading' && (
          <p className="settings-desc">Loading licences…</p>
        )}
        {state.status === 'error' && (
          <p className="settings-desc">
            Couldn’t load the bundled notices. You can open them directly:{' '}
            {NOTICE_DOCUMENTS.map(({ label, url }, index) => (
              <React.Fragment key={url}>
                {index > 0 && ', '}
                <a href={url} target="_blank" rel="noreferrer">{label}</a>
              </React.Fragment>
            ))}.
          </p>
        )}
        {state.status === 'ready' && <MarkdownView markdown={state.text} />}
      </div>
    </div>
  );
}

export default Licenses;
