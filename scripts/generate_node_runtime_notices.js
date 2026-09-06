#!/usr/bin/env node

const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const checkOnly = process.argv.includes('--check');
const targets = [
  {
    area: 'frontend',
    output: 'frontend/public/THIRD_PARTY_SOFTWARE_NOTICES.md',
    title: 'BreakTwenty Frontend Third-Party Software Notices',
  },
  {
    area: 'desktop',
    output: 'desktop/NODE_RUNTIME_NOTICES.md',
    title: 'BreakTwenty Desktop Node Runtime Notices',
  },
];

const normalizeRepository = (repository) => {
  const value = typeof repository === 'string' ? repository : repository?.url;
  if (typeof value !== 'string') return '';
  const normalized = value
    .replace(/^git\+/, '')
    .replace(/^git:\/\/github\.com\//, 'https://github.com/')
    .replace(/^ssh:\/\/git@github\.com[:/]/, 'https://github.com/')
    .replace(/^git@github\.com:/, 'https://github.com/')
    .replace(/\.git$/, '');
  return /^[a-z0-9_.-]+\/[a-z0-9_.-]+$/i.test(normalized)
    ? `https://github.com/${normalized}`
    : normalized;
};

const packageRows = (area) => {
  const lock = JSON.parse(fs.readFileSync(path.join(root, area, 'package-lock.json'), 'utf8'));
  const rows = [];
  const seen = new Set();
  for (const [relative, locked] of Object.entries(lock.packages || {})) {
    if (!relative || locked.dev === true) continue;
    const directory = path.join(root, area, relative);
    const metadataPath = path.join(directory, 'package.json');
    if (!fs.existsSync(metadataPath)) {
      throw new Error(`${area}: installed package metadata is missing for ${relative}`);
    }
    const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
    const name = metadata.name || relative.replace(/^node_modules\//, '');
    const version = locked.version || metadata.version;
    const key = `${name}@${version}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const license = locked.license || metadata.license;
    if (typeof license !== 'string' || !license.trim()) {
      throw new Error(`${area}: ${key} has no declared licence`);
    }
    const files = fs.readdirSync(directory)
      .filter((file) => /^(licen[cs]e|copying|notice)(\.|$)/i.test(file))
      .sort((left, right) => left.localeCompare(right));
    rows.push({
      area,
      directory,
      files,
      license: license.trim(),
      name,
      repository: normalizeRepository(metadata.repository),
      version,
      author: typeof metadata.author === 'string' ? metadata.author : '',
    });
  }
  return rows.sort((left, right) => left.name.localeCompare(right.name) || left.version.localeCompare(right.version));
};

const indented = (content) => content
  .replace(/\r\n/g, '\n')
  .trimEnd()
  .split('\n')
  .map((line) => `    ${line}`)
  .join('\n');

const render = ({ area, title }) => {
  const rows = packageRows(area);
  const lines = [
    `# ${title}`,
    '',
    'This generated inventory covers production JavaScript packages bundled into BreakTwenty.',
    'Each package remains under its own licence; those terms do not apply to original BreakTwenty code.',
    '',
    '| Package | Version | Declared licence |',
    '| --- | --- | --- |',
    ...rows.map((row) => `| ${row.name.replace(/\|/g, '\\|')} | ${row.version} | ${row.license.replace(/\|/g, '\\|')} |`),
    '',
    '## Bundled licence and notice texts',
    '',
  ];
  for (const row of rows) {
    lines.push(`### ${row.name} ${row.version}`);
    lines.push('');
    if (row.repository) lines.push(`Source: <${row.repository}>`, '');
    if (row.files.length === 0) {
      const author = row.author ? ` Author metadata: ${row.author}.` : '';
      lines.push(
        `The published package declares ${row.license} but contains no separate licence file.${author}`,
        '',
      );
      continue;
    }
    for (const file of row.files) {
      lines.push(`#### ${file}`, '', indented(fs.readFileSync(path.join(row.directory, file), 'utf8')), '');
    }
  }
  return `${lines.join('\n').trimEnd()}\n`;
};

let stale = false;
for (const target of targets) {
  const outputPath = path.join(root, target.output);
  const content = render(target);
  if (checkOnly) {
    const current = fs.existsSync(outputPath) ? fs.readFileSync(outputPath, 'utf8') : '';
    if (current !== content) {
      console.error(`Node runtime notices are stale: ${target.output}`);
      stale = true;
    }
  } else {
    fs.mkdirSync(path.dirname(outputPath), { recursive: true });
    fs.writeFileSync(outputPath, content, 'utf8');
    console.log(`Wrote ${target.output}`);
  }
}
if (stale) process.exit(1);
