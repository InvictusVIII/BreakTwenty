import { escapeHtml } from './html';

describe('escapeHtml', () => {
  it('escapes tooltip HTML metacharacters and normalizes missing values', () => {
    expect(escapeHtml(`A&B <tag> "quoted" 'single'`)).toBe(
      'A&amp;B &lt;tag&gt; &quot;quoted&quot; &#39;single&#39;',
    );
    expect(escapeHtml(null)).toBe('');
  });
});
