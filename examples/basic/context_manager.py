# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Same upload pattern with a context manager.

``with mock_bootloader(...)`` stops the mock on block exit — including when a
test fails. You can also rely on foreground auto-stop when the handle is GC'd.
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

    with mock_bootloader(chip=chip) as mock:
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
                'port': mock.port(),
                'ok': result.returncode == 0 and 'Wrote' in output,
            }


def main() -> int:
    try:
        info = run()
    except RuntimeError as exc:
        print(exc)
        return 1
    print(f"port {info['port']}: upload {'ok' if info['ok'] else 'failed'}")
    return 0 if info['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
