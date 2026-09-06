import React from 'react';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import {
  MemoryRouter,
  Route,
  useLocation,
  useNavigate,
  useNavigationType,
} from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import AppRoutes from './AppRoutes';
import { WELCOME_TOUR_STEPS } from './welcomeTourSteps';

const require = createRequire(import.meta.url);
const parser = require('@babel/parser');
const traverse = require('@babel/traverse').default;
const APP_ROUTE_PATHS = [
  '/',
  '/accounts',
  '/holdings',
  '/transactions',
  '/cash-flow',
  '/settings',
  '/settings/categories',
  '/faq',
  '/licenses',
];
const APP_ROUTE_COMPONENTS = {
  '/': 'Dashboard',
  '/accounts': 'Accounts',
  '/holdings': 'Holdings',
  '/transactions': 'Transactions',
  '/cash-flow': 'CashFlow',
  '/settings': 'Settings',
  '/settings/categories': 'CategoriesSettings',
  '/faq': 'Faq',
  '/licenses': 'Licenses',
};
const APP_ROUTE_DEMO_PROPS = {
  '/': ['tourDemoActive'],
  '/settings': ['promoDemoActive', 'onTogglePromoDemo'],
};

function jsxElementName(element) {
  return element?.openingElement?.name?.type === 'JSXIdentifier'
    ? element.openingElement.name.name
    : null;
}

function jsxAttribute(element, name) {
  return element.openingElement.attributes.find(
    (attribute) => attribute.type === 'JSXAttribute' && attribute.name.name === name,
  );
}

function routePath(element) {
  const attribute = jsxAttribute(element, 'path');
  return attribute?.value?.type === 'StringLiteral' ? attribute.value.value : null;
}

function LocationProbe() {
  const location = useLocation();
  const navigationType = useNavigationType();
  return (
    <output data-testid="location">
      {JSON.stringify({ pathname: location.pathname, state: location.state, navigationType })}
    </output>
  );
}

function BackButton() {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate(-1)}>Back</button>;
}

const routes = (
  <>
    <Route path="/" element={<div>Dashboard route</div>} />
    <Route path="/accounts" element={<div>Accounts route</div>} />
    <Route path="/transactions" element={<div>Transactions route</div>} />
  </>
);

describe('AppRoutes', () => {
  it('keeps the complete app route table behind the catch-all wrapper', () => {
    const appSource = readFileSync(
      path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../App.js'),
      'utf8',
    );

    const ast = parser.parse(appSource, { sourceType: 'module', plugins: ['jsx'] });
    const appRouteTables = [];
    const allRoutes = [];
    traverse(ast, {
      JSXElement(nodePath) {
        const element = nodePath.node;
        if (jsxElementName(element) === 'AppRoutes') appRouteTables.push(element);
        if (jsxElementName(element) === 'Route') allRoutes.push(element);
      },
    });

    expect(appRouteTables).toHaveLength(1);
    const wrappedRoutes = appRouteTables[0].children.filter(
      (child) => child.type === 'JSXElement' && jsxElementName(child) === 'Route',
    );
    expect(wrappedRoutes.map(routePath)).toEqual(APP_ROUTE_PATHS);
    expect(allRoutes).toEqual(wrappedRoutes);
    expect(Object.fromEntries(wrappedRoutes.map((route) => [
      routePath(route),
      jsxElementName(jsxAttribute(route, 'element')?.value?.expression),
    ]))).toEqual(APP_ROUTE_COMPONENTS);
    expect(Object.fromEntries(wrappedRoutes.flatMap((route) => {
      const routeElement = jsxAttribute(route, 'element')?.value?.expression;
      const demoProps = routeElement?.openingElement?.attributes
        .filter((attribute) => (
          attribute.type === 'JSXAttribute'
          && /(demo|promo|tour)/i.test(attribute.name.name)
        ))
        .map((attribute) => attribute.name.name);
      return demoProps?.length ? [[routePath(route), demoProps]] : [];
    }))).toEqual(APP_ROUTE_DEMO_PROPS);

    const welcomeRoutes = WELCOME_TOUR_STEPS
      .filter((step) => step.type === 'tour')
      .map((step) => step.targetRoute);
    welcomeRoutes.forEach((route) => expect(APP_ROUTE_PATHS).toContain(route));

    const cashFlowRoute = wrappedRoutes.find((route) => routePath(route) === '/cash-flow');
    const routeElement = jsxAttribute(cashFlowRoute, 'element')?.value?.expression;
    expect(jsxElementName(routeElement)).toBe('CashFlow');
    expect(jsxAttribute(routeElement, 'onDataChange')?.value?.expression).toMatchObject(
      { type: 'Identifier', name: 'fetchData' },
    );
  });

  it('replaces an unknown location with the dashboard route', async () => {
    render(
      <MemoryRouter initialEntries={['/accounts', '/not-a-route']} initialIndex={1}>
        <AppRoutes>{routes}</AppRoutes>
        <LocationProbe />
        <BackButton />
      </MemoryRouter>,
    );

    expect(await screen.findByText('Dashboard route')).toBeInTheDocument();
    expect(JSON.parse(screen.getByTestId('location').textContent)).toMatchObject({
      pathname: '/',
      state: null,
      navigationType: 'REPLACE',
    });

    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    await waitFor(() => {
      expect(JSON.parse(screen.getByTestId('location').textContent).pathname).toBe('/accounts');
    });
    expect(screen.getByText('Accounts route')).toBeInTheDocument();
  });

  it('keeps valid route handoff state intact', () => {
    const handoffState = {
      cashFlowFilter: {
        categoryIds: [4, 7],
        startDate: '2026-07-01',
        endDate: '2026-07-31',
      },
    };
    render(
      <MemoryRouter initialEntries={[{ pathname: '/transactions', state: handoffState }]}>
        <AppRoutes>{routes}</AppRoutes>
        <LocationProbe />
      </MemoryRouter>,
    );

    expect(screen.getByText('Transactions route')).toBeInTheDocument();
    expect(JSON.parse(screen.getByTestId('location').textContent)).toMatchObject({
      pathname: '/transactions',
      state: handoffState,
    });
  });
});
