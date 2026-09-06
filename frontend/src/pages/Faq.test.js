import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import Faq from './Faq';

describe('backup and recovery FAQ', () => {
  it('states the developer-custody and intentional-sharing boundary', () => {
    render(<Faq />);

    fireEvent.click(screen.getByRole('button', {
      name: 'Where is my financial data stored?',
    }));

    expect(screen.getByText(/developer does not receive, access, or control your database/i))
      .toBeInTheDocument();
    expect(screen.getByText(/nothing is uploaded to the developer automatically/i))
      .toBeInTheDocument();
    expect(screen.getByText(/only if you intentionally export and share it/i))
      .toBeInTheDocument();
  });

  it('explains the database/key boundary and supported readable export workflow', () => {
    render(<Faq />);

    fireEvent.click(screen.getByRole('button', {
      name: 'How do I back up or move my BreakTwenty data?',
    }));

    expect(screen.getByText(/breaktwenty\.db/i)).toBeInTheDocument();
    expect(screen.getByText(/separate digital key/i)).toBeInTheDocument();
    expect(screen.getByText(/cannot currently restore a complete app backup/i)).toBeInTheDocument();
    expect(screen.getByText(/not encrypted and contain sensitive financial information/i)).toBeInTheDocument();
  });

  it('keeps the voluntary support answer focused on contributions', () => {
    render(<Faq />);

    fireEvent.click(screen.getByRole('button', {
      name: 'I really like BreakTwenty. How can I support it?',
    }));

    const patreonLink = screen.getByRole('link', { name: /Patreon/i });
    expect(patreonLink.closest('p'))
      .toHaveTextContent(/Thanks! The best way to support BreakTwenty is through/i);
    expect(patreonLink.closest('p'))
      .toHaveTextContent(/Your help is truly appreciated.*Contributions do not purchase additional software rights, features, or services/i);
    expect(patreonLink)
      .toHaveAttribute('href', 'https://www.patreon.com/cw/BreakTwenty');
    expect(screen.getByRole('link', { name: /Ko-fi/i }))
      .toHaveAttribute('href', 'https://ko-fi.com/breaktwenty');
  });

  it('provides direct private and public bug-reporting routes', () => {
    render(<Faq />);

    fireEvent.click(screen.getByRole('button', {
      name: 'How can I report a bug or issue?',
    }));

    expect(screen.getByText('Application').closest('p'))
      .toHaveTextContent(/Application for app freezes.*Institutions for connection or sync problems/i);
    const supportEmailLink = screen.getByRole('link', { name: 'support@breaktwenty.com' });
    expect(supportEmailLink).toHaveAttribute('href', 'mailto:support@breaktwenty.com');
    expect(supportEmailLink).not.toHaveAttribute('target');
    expect(supportEmailLink.closest('p'))
      .toHaveTextContent(/Review the ZIP before sharing it.*email it privately.*Nothing is sent automatically/i);
    expect(screen.getByRole('link', { name: 'GitHub Issues' }))
      .toHaveAttribute('href', 'https://github.com/InvictusVIII/BreakTwenty/issues/new');
    expect(screen.getByRole('link', { name: /private security report/i }))
      .toHaveAttribute('href', 'https://github.com/InvictusVIII/BreakTwenty/security/advisories/new');
  });
});
