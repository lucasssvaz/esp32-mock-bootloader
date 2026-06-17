# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Two mocks at once — one upload endpoint per chip.

Call mock_bootloader() once per SoC you need. Each handle exposes its own
``url()`` for the matching ``esptool --chip`` invocation.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from esp32_mock_bootloader import constants, mock_bootloader


def run() -> dict[str, object]:
    if shutil.which('esptool') is None:
        raise RuntimeError('esptool not found on PATH')

    esp32 = mock_bootloader(chip='esp32')
    esp32c3 = mock_bootloader(chip='esp32c3')
    uploads: dict[str, bool] = {}
    for chip, mock in (('esp32', esp32), ('esp32c3', esp32c3)):
        with tempfile.TemporaryDirectory() as tmp:
            bin_path = Path(tmp) / 'app.bin'
            bin_path.write_bytes(bytes([0xE9]) + b'\xff' * 511)
            result = subprocess.run(
                [
                    'esptool',
                    '--chip', chip,
                    '--port', mock.url(),
                    '--before', 'no-reset',
                    '--after', 'no-reset',
                    '--no-stub',
                    'write-flash',
                    hex(constants.FLASH_APP_OFFSET),
                    str(bin_path),
                ],
                capture_output=True,
                text=True,
                timeout=60.0,
                check=False,
            )
            output = result.stdout + result.stderr
            uploads[chip] = result.returncode == 0 and 'Wrote' in output
    return {
        'urls': {'esp32': esp32.url(), 'esp32c3': esp32c3.url()},
        'uploads': uploads,
        'ok': all(uploads.values()),
    }


def main() -> int:
    try:
        info = run()
    except RuntimeError as exc:
        print(exc)
        return 1
    for chip, url in info['urls'].items():
        status = 'ok' if info['uploads'][chip] else 'failed'
        print(f'{chip} {url}: {status}')
    return 0 if info['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
