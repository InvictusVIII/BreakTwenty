const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const source = (name) => fs.readFileSync(path.join(__dirname, name), 'utf8');

test('desktop main and preload expose only the cloud authorization surface for Moomoo', () => {
  const combined = `${source('main.js')}\n${source('preload.js')}`;
  const retiredManagerName = ['moomoo', 'Open', 'D', 'Manager'].join('');
  const retiredBridgeName = ['moomoo', 'Open', 'D'].join('');
  const retiredChannelPrefix = ['moomoo', '-', 'opend', ':'].join('');

  assert.equal(combined.includes(retiredManagerName), false);
  assert.equal(combined.includes(retiredBridgeName), false);
  assert.equal(combined.includes(retiredChannelPrefix), false);
  assert.match(combined, /visibleAuth/);
});
