// First-run welcome tour configuration. Edit copy and order here.
//
// `type`:        'intro' and 'finish' are centered cards; 'tour' is a spotlight step that
//                navigates to its page and points at the rail button.
// `targetRoute`: must equal a nav item's `to` (rendered as data-tour-id on the rail button
//                in App.js) so the tour can navigate there and the spotlight can locate it.
//
// While the tour is open the app runs in "demo mode" (see tourDemoData.js), so each page is
// populated with dummy data; demo mode turns off when the tour finishes. Steps are ordered to
// glide the spotlight straight down the rail.
import { APP_BRAND_NAME } from '../constants/brand';
import { getSeedCategory } from '../constants/seedCategoryTaxonomy';

export const WELCOME_TOUR_STEPS = [
  { type: 'intro' },
  {
    type: 'tour',
    targetRoute: '/',
    scrollTop: 0,
    title: 'Your money at a glance',
    description:
      "Here's your full picture: Net worth history, Asset allocation, Cash flow, and other key indicators that show where things stand for you at a glance.",
  },
  {
    type: 'tour',
    targetRoute: '/accounts',
    scrollTop: 0,
    title: 'Want more details?',
    description:
      'This page lists every institution, asset, and liability you have added. Drill into each one to review balances and changes over time, sync institutions when needed, and open any row to see the details behind the totals.',
    note:
      'Note: Canadian banks generally do not offer consumer-facing public API access yet, so some connections require manual resyncing to update balances and transaction history.',
    noteTone: 'primary',
    hint: { page: 'accounts', institutionId: 103 },
    hasTray: true,
  },
  {
    type: 'tour',
    targetRoute: '/cash-flow',
    title: 'Keeping a finger on the pulse of your spending',
    description:
      'Tracking your monthly cash flow has never been easier!',
    note:
      `Check your overall cash flow, dive into individual expense and income categories, review recurring payments ${APP_BRAND_NAME} detects, and see whether you're on track to your budget goals.`,
    // Opens the Food & Drink spending category's detail tray (parent category id).
    hint: { page: 'cashflow', spendingCategoryId: getSeedCategory('food_and_drink').id },
  },
  {
    type: 'tour',
    targetRoute: '/holdings',
    scrollTop: 0,
    title: 'See your investments grow',
    description:
      `Have ${APP_BRAND_NAME} keep an eye on all your positions across multiple accounts and brokers, pulling them into one unified portfolio overview.`,
    hint: { tab: 'overview' },
  },
  {
    type: 'tour',
    targetRoute: '/holdings',
    scrollTop: 0,
    title: 'Watch your nest egg bear fruit',
    description:
      'Track dividends, distributions, and interest as they roll in, with withholding tax shown alongside them. Open any month to see which positions paid you and how your income stream is growing over time!',
    // Asks Holdings to open the Income → Dividends view and pre-select a month's bar.
    hint: { tab: 'income', incomeSubTab: 'dividends', selectBar: true },
    hasTray: true,
  },
  {
    type: 'tour',
    targetRoute: '/transactions',
    scrollTop: 0,
    title: 'The record behind everything',
    description:
      'Transactions are the foundation for your cash flow, budgets, account history, and insights. Search, filter, retag, annotate, and correct the auto-assigned categories that power the rest of your financial picture.',
    note:
      `Note: ${APP_BRAND_NAME} does its best to place transactions into relevant categories, but auto-categorization is never perfect. Review the feed and adjust categories so they match what each transaction actually represents, when necessary.`,
    noteFollowup:
      `${APP_BRAND_NAME} will then remember your preference and recategorize future similar transactions accordingly.`,
    noteTone: 'primary',
    // Opens a transaction's detail drawer to show it's editable.
    hint: { page: 'transactions', openDetail: true },
    hasTray: true,
  },
  { type: 'finish' },
];
