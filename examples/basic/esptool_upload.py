# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Upload to the mock with esptool.

Typical flow:
  1. mock_bootloader(chip=...)  -> running mock (foreground by default)
  2. mock.url()                 -> socket://127.0.0.1:PORT
  3. esptool --port <url> ...   -> your upload client talks to the mock
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from esp32_mock_bootloader import constants, mock_bootloader


def run(chip: str = 'esp32') -> dict[str, object]:
    if shutil.which('esptool') is None:
        raise RuntimeError('esptool not found on PATH')

    mock = mock_bootloader(chip=chip)
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
        return {
            'url': mock.url(),
            'returncode': result.returncode,
            'ok': result.returncode == 0 and 'Wrote' in output,
        }


def main() -> int:
    try:
        info = run()
    except RuntimeError as exc:
        print(exc)
        return 1
    print(f"upload via {info['url']}: {'ok' if info['ok'] else 'failed'}")
    return 0 if info['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
