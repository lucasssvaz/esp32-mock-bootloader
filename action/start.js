'use strict';

const { input, pipInstall, run, saveState, setOutput } = require('./lib');

async function main() {
  const chip = input('chip', 'auto');
  const port = input('port', '9876');
  const startupTimeout = input('startup-timeout', '30');

  pipInstall();

  run('esp32-mock-bootloader', [
    'start',
    '--chip', chip,
    '--port', port,
    '--startup-timeout', startupTimeout,
  ]);

  const url = run('esp32-mock-bootloader', ['url', '--port', port]).stdout.trim();

  setOutput('port', port);
  setOutput('url', url);
  saveState({ port });
}

main().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});
