import React, { useEffect, useRef, useState } from 'react';
import {
  FaEnvelope,
  FaGithub,
  FaPatreon,
} from 'react-icons/fa6';
import { SiKofi } from 'react-icons/si';
import { renderBrandText } from '../components/BrandName';
import TriangleIcon from '../components/TriangleIcon';
import { APP_BRAND_NAME } from '../constants/brand';
import './Settings.css';
import './Faq.css';

const PUBLIC_REPO_URL = 'https://github.com/InvictusVIII/BreakTwenty';
const PUBLIC_ISSUES_URL = `${PUBLIC_REPO_URL}/issues/new`;
const PRIVATE_SECURITY_REPORT_URL = `${PUBLIC_REPO_URL}/security/advisories/new`;
const SUPPORT_EMAIL = 'support@breaktwenty.com';
const PATREON_URL = 'https://www.patreon.com/cw/BreakTwenty';
const KOFI_URL = 'https://ko-fi.com/breaktwenty';

const FAQ_ITEMS = [
  {
    id: 'why-breaktwenty',
    question: `Why ${APP_BRAND_NAME}?`,
    answer: [
      'The name comes from the everyday use of breaking a twenty-dollar bill at a store into smaller bills and coins: same money, easier to manage and use.',
      `${APP_BRAND_NAME} does the same for your financial life. It brings your whole net worth picture under one roof, then breaks it into clearer, more manageable pieces: accounts, assets, debts, cash flow, investments, transactions, and more.`,
    ],
  },
  {
    id: 'what-can-i-track',
    question: `What can ${APP_BRAND_NAME} help me keep track of?`,
    answer: [
      `${APP_BRAND_NAME} is built for the messy real-life mix: all your data from various banks and brokerages, crypto, cash, and many other assets that most of the time cannot be tracked digitally, such as property, vehicles, and other valuables.`,
      'It also helps you keep track of your investment holdings and dividend history. Connected accounts update from supported providers, and anything that does not connect yet can still be added manually.',
    ],
  },
  {
    id: 'trust-financial-data',
    question: `How can I trust ${APP_BRAND_NAME} with my financial data?`,
    answer: [
      <>
        {renderBrandText(`${APP_BRAND_NAME}'s local app code is public on `, 'trust-financial-data-github-prefix')}
        <FaqAnswerLink href={PUBLIC_REPO_URL} icon={FaGithub}>GitHub</FaqAnswerLink>
        {', so anyone can inspect how it stores data, handles provider connections, encrypts local files, and creates exports.'}
      </>,
      'You can also build the app yourself from the public source code instead of using a prebuilt download. That way, the app running on your device comes from code you or someone you trust can review.',
    ],
  },
  {
    id: 'connections-work',
    question: 'How do connected accounts update?',
    answer: [
      'Each connection updates in the way that institution allows. Connections that use an API key or a provider gateway running locally on your computer can usually refresh balances, holdings, and transactions quietly in the background after setup without your further input.',
      'Most bank sign-in connections are more hands-on. They may keep working from a recent saved local session for a while, but the institution can still ask for a fresh login, approval, MFA step, or cookie prompt because of the bank\'s own security measures before sharing new data again.',
    ],
  },
  {
    id: 'why-semi-manual-banks',
    question: 'Why do most Canadian banks require manual resync?',
    answer: [
      'Canada does not yet have one shared connection standard that every bank and brokerage exposes to the public. Some institutions offer cleaner access, while others only allow account data through their regular client sign-in.',
      `${APP_BRAND_NAME} uses the best available path for each institution. If a direct connection is not available yet, you can still add the account manually and keep it in your net-worth picture.`,
    ],
  },
  {
    id: 'manual-assets',
    question: 'Can I add accounts, assets, or debt manually?',
    answer: (
      <>
        Yes! You can add manual institutions that are not yet directly supported,
        cash, property, vehicles, and other assets and debts from the{' '}
        <strong>Add to Net Worth</strong> button. Manual entries still appear in
        net worth, allocation, accounts, and cash-flow views where applicable.
      </>
    ),
  },
  {
    id: 'data-storage',
    question: 'Where is my financial data stored?',
    answer: [
      `${APP_BRAND_NAME} is designed as a local, self-hosted finance app. Your financial data lives on your device, protected locally. The developer does not receive, access, or control your database, financial records, credentials, or saved sessions through the current desktop edition.`,
      'Connected features make requests from your device to the institution or integration service you select, while market data, exchange rates, news, downloads, and updates contact their relevant providers. BreakTwenty does not operate a developer-controlled financial-data relay.',
      'Institution diagnostics and the small, sanitized rolling record of rare application failures also stay on your device. Nothing is uploaded to the developer automatically; a diagnostic leaves your device only if you intentionally export and share it.',
      'The app encrypts its database and uses your computer’s built-in security to protect saved connection details.',
      'For the limits of copying or moving that local data, see “How do I back up or move my BreakTwenty data?” below.',
    ],
  },
  {
    id: 'backup-and-move',
    question: `How do I back up or move my ${APP_BRAND_NAME} data?`,
    answer: [
      <>
        Your <strong>breaktwenty.db</strong> file can&apos;t be opened on its own. BreakTwenty also needs a separate
        digital key, which is protected by your computer and user account.
      </>,
      'Because the database and digital key belong together, copying the database—or even the whole BreakTwenty data folder—is not a reliable way to back up or move your data. If you lose your computer, user account, or digital key, BreakTwenty may no longer be able to unlock the database.',
      <>
        BreakTwenty cannot currently restore a complete app backup. Use <strong>Settings → Data Export &amp; Recovery → Export All Data</strong> to retain readable copies of transactions, balance history, current holdings, and net-worth history outside the app. Preserve all four CSV files in the downloaded ZIP.
      </>,
      'The export ZIP and its CSV files are not encrypted and contain sensitive financial information. Store them somewhere secure. Importing transaction or balance CSVs into a manual account is not a full restore and will not recreate every setting, account relationship, institution connection, or saved credential.',
    ],
  },
  {
    id: 'offline-use',
    question: `Can I use ${APP_BRAND_NAME} offline?`,
    answer: [
      'You can open and review data that is already saved on your device, including manual assets, accounts, transactions, and the last synced view of your financial profile.',
      'Anything that needs the outside world still needs internet: syncing connected institutions, refreshing exchange rates or market data, reconnecting providers, and creating new provider sessions.',
    ],
  },
  {
    id: 'sync-frequency',
    question: 'How often does my data update?',
    answer: [
      `You can sync accounts manually from the Accounts page. ${APP_BRAND_NAME} also checks supported connected institutions in the background about every six hours while the app is open.`,
      `If you close the app and then open it again later, ${APP_BRAND_NAME} checks whether at least six hours have passed since the last auto-sync started and runs one if it is due. Manual assets and debts update when you edit their value or add/import transactions.`,
    ],
  },
  {
    id: 'uninstall-data',
    question: `What happens to my data if I uninstall ${APP_BRAND_NAME}?`,
    answer: [
      'Your financial data is stored locally in the app data on your device. Depending on your operating system and uninstall method, removing the app may not automatically remove that local database, logs, or exported files.',
      <>
        For packaged installs, the main app data folder is usually:
        <br />
        <br />
        <strong>Windows:</strong> %APPDATA%\BreakTwenty. Windows also keeps the
        managed browser runtime cache under %LOCALAPPDATA%\BreakTwenty\browser-runtimes.
        <br />
        <strong>Linux:</strong> ~/.config/BreakTwenty
        <br />
        <strong>macOS:</strong> ~/Library/Application Support/BreakTwenty
      </>,
      'Before deleting local app data, follow “How do I back up or move my BreakTwenty data?” and export anything you want to keep. If you want everything gone, remove your own user data folders and any exports or diagnostic bundles you created.',
    ],
  },
  {
    id: 'leave-open',
    question: `Do I need to leave ${APP_BRAND_NAME} open for syncs to run?`,
    answer: [
      'Yes, for automatic background syncs. The app needs to be open and running so it can check whether supported connected institutions are due for an update.',
      'If the app is closed, it is not syncing in the background. When you open it again, it checks whether an auto-sync is due, and you can always start a manual sync from the Accounts page.',
    ],
  },
  {
    id: 'sync-attention',
    question: 'What happens if a connection needs attention?',
    answer: [
      `Sometimes a bank, brokerage, or provider requires a fresh sign-in before it shares new data. When that happens, ${APP_BRAND_NAME} marks the institution as needing attention instead of trying to keep syncing in the background.`,
      <>
        Use the{' '}
        <strong className="faq-answer-accent">Sync</strong>
        {' '}button on the Accounts page, complete the sign-in and MFA approval
        if required, and then wait for the provider sync to finish.
      </>,
    ],
  },
  {
    id: 'sync-failed',
    question: 'What should I do if I see Sync failed or a connection error?',
    answer: [
      'Try syncing again first; banks and brokerages can be temporarily unavailable. If it keeps failing, check your internet connection and try turning off any VPN or proxy, since many financial institutions block or behave differently on VPN networks.',
      <>
        If the institution status changes to{' '}
        <strong className="faq-answer-accent">Sync</strong>
        , reconnect the institution and complete the provider sign-in again.
      </>,
      'If it still fails, follow the reporting steps below and export the institution logs from Diagnostics → Institutions.',
    ],
  },
  {
    id: 'app-diagnostics',
    question: `What should I do if ${APP_BRAND_NAME} freezes or cannot refresh?`,
    answer: [
      `Give ${APP_BRAND_NAME} a moment to recover and reload itself. If it still does not respond, close and reopen the app.`,
      'After reopening, go to Diagnostics → Application and export the captured incident. If there is no incident, choose Export Current App Logs. Then use the reporting steps below.',
    ],
  },
  {
    id: 'report-bug',
    question: 'How can I report a bug or issue?',
    answer: [
      <>
        Go to <strong>Diagnostics</strong> and export the logs that match the problem:{' '}
        <strong>Application</strong> for app freezes, crashes, or refresh problems;{' '}
        <strong>Institutions</strong> for connection or sync problems.
      </>,
      <>
        Review the ZIP before sharing it, then email it privately with a short description of what
        happened to{' '}
        <FaqAnswerLink href={`mailto:${SUPPORT_EMAIL}`} icon={FaEnvelope} newTab={false}>
          {SUPPORT_EMAIL}
        </FaqAnswerLink>
        . Nothing is sent automatically.
      </>,
      <>
        Regular, non-sensitive bugs and feature requests can also be reported through{' '}
        <FaqAnswerLink href={PUBLIC_ISSUES_URL} icon={FaGithub}>GitHub Issues</FaqAnswerLink>
        . Never post diagnostic ZIPs, financial or account details, credentials, or sensitive
        screenshots publicly. For a suspected security vulnerability, use{' '}
        <FaqAnswerLink href={PRIVATE_SECURITY_REPORT_URL} icon={FaGithub}>
          GitHub&apos;s private security report
        </FaqAnswerLink>
        .
      </>,
    ],
  },
  {
    id: 'read-only',
    question: `Is ${APP_BRAND_NAME} read-only?`,
    answer: [
      `${APP_BRAND_NAME} is built to pull financial data into your local app: balances, holdings, transactions, income, and related account history.`,
      'It is not built to move money, pay bills, place trades, or change account settings at your bank or brokerage. In secure sign-in bank windows, you may still see normal institution prompts such as cookies, MFA, or approval screens before the window auto-closes and the provider shares your updated data.',
    ],
  },
  {
    id: 'delete-institution',
    question: 'What happens if I delete an institution?',
    answer: [
      `Deleting an institution removes that connection from ${APP_BRAND_NAME}, including its local accounts, balances, holdings, transactions, sync history, and saved connection details for that institution.`,
      'It does not close your real bank or brokerage account. It only removes the local copy and connection inside the app.',
    ],
  },
  {
    id: 'currencies',
    question: 'Can I use more than one currency?',
    answer: [
      'Yes! Accounts and holdings keep their native currency, while dashboard totals, account groups, cash flow, and investment summaries can show as your selected primary currency.',
      `${APP_BRAND_NAME} uses current and historical exchange rates saved in the app, including daily historical rates for past transactions and income, so older cash flow is converted using rates from the right date instead of today's rate.`,
    ],
  },
  {
    id: 'imports',
    question: 'Can I import old transactions or account history?',
    answer: [
      'Yes. For manual institutions, you can use Add Transactions to import CSV files for transactions or balance history after you create the manual account.',
      'There are also brokerage-specific history imports. Questrade can import monthly statement PDFs to extend month-end account history, and IBKR can import Flex XML reports to bring in older transactions, dividends, and daily account-value history.',
      `If a Questrade statement or IBKR Flex report belongs to a closed or transferred account that no longer appears in the live sync, ${APP_BRAND_NAME} can add it as an imported inactive account so your old net-worth history is still represented.`,
    ],
  },
  {
    id: 'wrong-transaction-category',
    question: 'What if a pulled transaction is in the wrong category?',
    answer: [
      'Open the transaction on the Transactions page and choose the right category. Your edit is saved as a manual category, so future syncs will not overwrite it.',
      `When possible, ${APP_BRAND_NAME} learns from that edit so future transactions from the same recognizable merchant or description use the new category too. Very generic descriptions, like transfers or payments, may still need review so unrelated transactions are not grouped together by mistake.`,
      'If several existing transactions have the same raw description, the transaction details panel can apply the category change to all of those matches at once.',
    ],
  },
  {
    id: 'exports',
    question: 'Can I export my data?',
    answer: `Yes. Use Settings → Data Export & Recovery → Export All Data to download the four readable financial sets together, or use the individual Export buttons. These unencrypted exports are for retaining and reviewing data; they are not a full ${APP_BRAND_NAME} backup or restore. See “How do I back up or move my ${APP_BRAND_NAME} data?” for the complete recovery limitation.`,
  },
  {
    id: 'support-breaktwenty',
    question: `I really like ${APP_BRAND_NAME}. How can I support it?`,
    answer: (
      <>
        Thanks! The best way to support {renderBrandText(APP_BRAND_NAME, 'support-breaktwenty-brand')} is through{' '}
        <FaqAnswerLink href={PATREON_URL} icon={FaPatreon}>Patreon</FaqAnswerLink>
        {' or '}
        <FaqAnswerLink href={KOFI_URL} icon={SiKofi}>Ko-fi</FaqAnswerLink>
        {'. Your help is truly appreciated. Contributions do not purchase additional software rights, features, or services.'}
      </>
    ),
  },
  {
    id: 'tax-financial-advice',
    question: `Can ${APP_BRAND_NAME} file my taxes or give financial advice?`,
    answer: [
      'No. The app helps organize and understand your financial data, but it does not provide tax, legal, investment, or financial advice.',
      'Use it as a tracking and review tool. For tax filings, investment decisions, or financial planning, talk to a qualified professional and verify the numbers against your official statements.',
    ],
  },
];

const FAQ_COLLAPSE_MS = 240;

function FaqAnswerLink({ href, icon: Icon, children, newTab = true }) {
  return (
    <a
      href={href}
      target={newTab ? '_blank' : undefined}
      rel={newTab ? 'noreferrer' : undefined}
      className="faq-answer-link"
    >
      <Icon className="faq-answer-link-icon" aria-hidden="true" focusable="false" />
      <span>{children}</span>
    </a>
  );
}

// FAQ page. User-facing Q&A within the Help route group.
function Faq() {
  const [openItemIds, setOpenItemIds] = useState(() => new Set());
  const [closingItemIds, setClosingItemIds] = useState(() => new Set());
  const closingTimeoutsRef = useRef({});

  useEffect(() => () => {
    Object.values(closingTimeoutsRef.current).forEach(window.clearTimeout);
  }, []);

  const toggleItem = (itemId) => {
    if (closingTimeoutsRef.current[itemId]) {
      window.clearTimeout(closingTimeoutsRef.current[itemId]);
      delete closingTimeoutsRef.current[itemId];
    }

    setOpenItemIds((currentIds) => {
      const nextIds = new Set(currentIds);
      if (nextIds.has(itemId)) {
        nextIds.delete(itemId);
        setClosingItemIds((currentClosingIds) => {
          const nextClosingIds = new Set(currentClosingIds);
          nextClosingIds.add(itemId);
          return nextClosingIds;
        });
        closingTimeoutsRef.current[itemId] = window.setTimeout(() => {
          setClosingItemIds((currentClosingIds) => {
            const nextClosingIds = new Set(currentClosingIds);
            nextClosingIds.delete(itemId);
            return nextClosingIds;
          });
          delete closingTimeoutsRef.current[itemId];
        }, FAQ_COLLAPSE_MS);
      } else {
        nextIds.add(itemId);
        setClosingItemIds((currentClosingIds) => {
          const nextClosingIds = new Set(currentClosingIds);
          nextClosingIds.delete(itemId);
          return nextClosingIds;
        });
      }
      return nextIds;
    });
  };

  return (
    <div className="page-frame settings-section faq-page">
      <div className="faq-list" aria-label="Frequently asked questions">
        {FAQ_ITEMS.map((item) => {
          const isOpen = openItemIds.has(item.id);
          const isClosing = closingItemIds.has(item.id);
          const isVisuallyOpen = isOpen || isClosing;
          const panelId = `faq-answer-${item.id}`;
          const answerParagraphs = Array.isArray(item.answer) ? item.answer : [item.answer];

          return (
            <div
              key={item.id}
              className={`faq-item ${isVisuallyOpen ? 'is-open' : ''}`.trim()}
            >
              <button
                type="button"
                className={`button-shell-opt-out faq-question-trigger ${isVisuallyOpen ? 'is-open is-expanded' : ''}`.trim()}
                aria-expanded={isOpen}
                aria-controls={panelId}
                onClick={() => toggleItem(item.id)}
              >
                <span className="faq-question-chevron">
                  <TriangleIcon direction={isVisuallyOpen ? 'down' : 'right'} />
                </span>
                <span className="faq-question-text">{renderBrandText(item.question, `${item.id}-question`)}</span>
              </button>
              <div
                id={panelId}
                className={`faq-answer-region ${isOpen ? 'is-open' : ''}`.trim()}
                aria-hidden={!isOpen}
              >
                <div className="faq-answer-content">
                  {answerParagraphs.map((paragraph, index) => (
                    <p className="faq-answer-text" key={`${item.id}-${index}`}>
                      {renderBrandText(paragraph, `${item.id}-answer-${index}`)}
                    </p>
                  ))}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default Faq;
