# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Upload with esptool, verify bytes with ``advanced.protocol``.

Typical CI pattern: use esptool for the upload, then the protocol client to
assert the mock flash image contains your firmware.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from esp32_mock_bootloader import constants, mock_bootloader
from esp32_mock_bootloader.advanced import protocol

VERIFY_LENGTH = 0x100


def run(chip: str = 'esp32') -> dict[str, object]:
    if shutil.which('esptool') is None:
        raise RuntimeError('esptool not found on PATH')

    offset = constants.FLASH_APP_OFFSET
    mock = mock_bootloader(chip=chip)
    with tempfile.TemporaryDirectory() as tmp:
        bin_path = Path(tmp) / 'app.bin'
        expected = b'\x5A' * VERIFY_LENGTH
        bin_path.write_bytes(expected)

        upload = subprocess.run(
            [
                'esptool',
                '--chip', chip,
                '--port', mock.url(),
                '--before', 'no-reset',
                '--after', 'no-reset',
                'write-flash',
                hex(offset),
                str(bin_path),
            ],
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
        )
        upload_out = upload.stdout + upload.stderr
        if upload.returncode != 0 or 'Wrote' not in upload_out:
            return {'ok': False, 'stage': 'upload'}

        client = protocol.connect(mock)
        client.activate_stub()
        data, _digest = client.stub_read_flash(offset, VERIFY_LENGTH)
        return {
            'ok': data == expected,
            'stage': 'verify',
            'bytes_match': data == expected,
        }


def main() -> int:
    try:
        info = run()
    except RuntimeError as exc:
        print(exc)
        return 1
    if info.get('stage') == 'upload':
        print('esptool upload failed')
        return 1
    print(f'readback: {"match" if info["bytes_match"] else "mismatch"}')
    return 0 if info['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
