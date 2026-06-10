'use strict';

const { input, loadState, run } = require('./lib');

async function post() {
  const state = loadState();
  const port = state?.port || input('port', '9876');
  run('esp32-mock-bootloader', ['stop', '--port', port], { ignoreError: true });
  console.log(`Mock bootloader post step: stopped daemon on port ${port}`);
}

post().catch((err) => {
  console.error(err.message || err);
  process.exit(0);
});
