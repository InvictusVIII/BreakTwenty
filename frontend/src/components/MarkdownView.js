import React, { useMemo } from 'react';
import { replaceBrandNameHtml } from './BrandName';
import './MarkdownView.css';

// Minimal Markdown -> HTML renderer for the bundled, developer-authored docs
// (e.g. THIRD-PARTY-LICENSES.md). It is intentionally small and only covers the
// constructs those docs use: headings, paragraphs, unordered lists, GFM tables,
// fenced code blocks, blockquotes, horizontal rules, and inline bold / code /
// links. Content is a trusted in-repo asset (never user input) and all text is
// HTML-escaped before formatting, so dangerouslySetInnerHTML is safe here.

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function renderInline(text) {
  const codeSegments = [];
  let html = escapeHtml(text);
  html = html.replace(/`([^`]+)`/g, (_, code) => {
    const index = codeSegments.length;
    codeSegments.push(`<code>${code}</code>`);
    return `@@BREAKTWENTY_CODE_${index}@@`;
  });
  html = replaceBrandNameHtml(html);
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(
    /\[([^\]]+)\]\(([^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer">$1</a>',
  );
  html = html.replace(/@@BREAKTWENTY_CODE_(\d+)@@/g, (_, index) => codeSegments[Number(index)] || '');
  return html;
}

function isTableSeparator(line) {
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line || '');
}

function splitRow(line) {
  let row = line.trim();
  if (row.startsWith('|')) row = row.slice(1);
  if (row.endsWith('|')) row = row.slice(0, -1);
  return row.split('|').map((cell) => cell.trim());
}

function isBlockBoundary(line) {
  return (
    /^\s*$/.test(line)
    || /^(#{1,6})\s/.test(line)
    || /^```/.test(line)
    || /^\s*[-*]\s+/.test(line)
    || /^\s*>\s?/.test(line)
    || /^\s*---+\s*$/.test(line)
    || /^\s*\|/.test(line)
  );
}

function markdownToHtml(markdown) {
  const lines = String(markdown).replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {
      const body = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence
      out.push(`<pre><code>${escapeHtml(body.join('\n'))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s*---+\s*$/.test(line)) {
      out.push('<hr/>');
      i += 1;
      continue;
    }

    if (/^\s*\|/.test(line) && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const header = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      const thead = `<thead><tr>${header.map((c) => `<th>${renderInline(c)}</th>`).join('')}</tr></thead>`;
      const tbody = `<tbody>${rows
        .map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join('')}</tr>`)
        .join('')}</tbody>`;
      out.push(`<table>${thead}${tbody}</table>`);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const body = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      out.push(`<blockquote>${renderInline(body.join(' '))}</blockquote>`);
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''));
        i += 1;
      }
      out.push(`<ul>${items.map((it) => `<li>${renderInline(it)}</li>`).join('')}</ul>`);
      continue;
    }

    if (/^\s*$/.test(line)) {
      i += 1;
      continue;
    }

    const paragraph = [];
    while (i < lines.length && !isBlockBoundary(lines[i])) {
      paragraph.push(lines[i]);
      i += 1;
    }
    if (paragraph.length) {
      out.push(`<p>${renderInline(paragraph.join(' '))}</p>`);
    }
  }

  return out.join('\n');
}

function MarkdownView({ markdown }) {
  const html = useMemo(() => markdownToHtml(markdown || ''), [markdown]);
  return <div className="markdown-body" dangerouslySetInnerHTML={{ __html: html }} />;
}

export default MarkdownView;
