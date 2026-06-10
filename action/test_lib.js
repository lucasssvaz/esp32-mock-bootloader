'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const lib = require('./lib');

function withEnv(overrides, fn) {
  const saved = {};
  for (const key of Object.keys(overrides)) {
    saved[key] = process.env[key];
    if (overrides[key] === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = overrides[key];
    }
  }
  try {
    fn();
  } finally {
    for (const key of Object.keys(saved)) {
      if (saved[key] === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = saved[key];
      }
    }
  }
}

withEnv({ INPUT_CHIP: 'esp32', INPUT_PORT: '12345' }, () => {
  assert.strictEqual(lib.input('chip', 'auto'), 'esp32');
  assert.strictEqual(lib.input('port', '9876'), '12345');
  assert.strictEqual(lib.input('missing', 'fallback'), 'fallback');
});

withEnv({}, () => {
  lib.saveState({ port: '55555' });
  const loaded = lib.loadState();
  assert.ok(loaded);
  assert.strictEqual(loaded.port, '55555');
  fs.unlinkSync(lib.STATE_FILE);
});

assert.strictEqual(lib.loadState(), null);

withEnv({ GITHUB_OUTPUT: undefined }, () => {
  lib.setOutput('url', 'socket://127.0.0.1:1');
});

const outputFile = path.join(os.tmpdir(), `mock-action-output-${process.pid}.txt`);
withEnv({ GITHUB_OUTPUT: outputFile }, () => {
  lib.setOutput('url', 'socket://127.0.0.1:1');
  assert.ok(fs.existsSync(outputFile));
  assert.ok(fs.readFileSync(outputFile, 'utf-8').includes('url=socket://'));
  fs.unlinkSync(outputFile);
});

console.log('action/lib.js: OK');
