'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const STATE_FILE = path.join(
  process.env.RUNNER_TEMP || '/tmp',
  'esp32-mock-bootloader-action.json',
);

function input(name, fallback = '') {
  const key = `INPUT_${name.replace(/-/g, '_').toUpperCase()}`;
  return process.env[key] ?? fallback;
}

function run(cmd, args, { ignoreError = false } = {}) {
  const result = spawnSync(cmd, args, {
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0 && !ignoreError) {
    const detail = (result.stderr || result.stdout || '').trim();
    throw new Error(detail || `${cmd} ${args.join(' ')} failed`);
  }
  return result;
}

function pipInstall() {
  const version = input('version');
  const packageRoot = input('package-root') || process.env.GITHUB_WORKSPACE || '';
  if (version) {
    run('python', ['-m', 'pip', 'install', `esp32-mock-bootloader==${version}`]);
  } else if (packageRoot) {
    run('python', ['-m', 'pip', 'install', packageRoot]);
  } else {
    throw new Error('package-root or version input is required');
  }
}

function setOutput(name, value) {
  const file = process.env.GITHUB_OUTPUT;
  if (!file) {
    return;
  }
  fs.appendFileSync(file, `${name}=${value}${require('os').EOL}`);
}

function saveState(data) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(data));
}

function loadState() {
  if (!fs.existsSync(STATE_FILE)) {
    return null;
  }
  return JSON.parse(fs.readFileSync(STATE_FILE, 'utf-8'));
}

module.exports = {
  STATE_FILE,
  input,
  run,
  pipInstall,
  setOutput,
  saveState,
  loadState,
};
