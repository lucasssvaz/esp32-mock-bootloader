#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Test esp32-mock-bootloader over a com0com virtual COM pair on Windows.

Requires com0com (https://sourceforge.net/projects/com0com/) installed and
setupc.exe runnable with administrator privileges (creates/removes the pair).

  python scripts/test_windows_com.py

Override ports with ESP32_MOCK_COM_PORT / ESP32_MOCK_COM_PEER (defaults COM18/COM19).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from esp32_mock_bootloader.com0com import Com0ComError, com0com_pair

COM_SERVER = os.environ.get('ESP32_MOCK_COM_PORT', 'COM18')
COM_PEER = os.environ.get('ESP32_MOCK_COM_PEER', 'COM19')


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main() -> int:
    if os.name != 'nt':
        return _fail('This script is for Windows with com0com installed.')

    try:
        with com0com_pair(COM_SERVER, COM_PEER) as pair:
            return _run_flash_test(pair.server, pair.peer)
    except Com0ComError as exc:
        return _fail(str(exc))


def _run_flash_test(com_server: str, com_peer: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path_file = Path(tmp) / 'mock-com.path'
        bin_file = Path(tmp) / 'test.bin'
        bin_file.write_bytes(b'\x00' * 1024)

        proc = subprocess.Popen(
            [
                sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
                '--pty',
                '--pty-path-file', str(path_file),
                '--com-port', com_server,
                '--com-peer', com_peer,
                '--chip', 'auto',
                '--timeout', '120',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for _ in range(100):
                if path_file.is_file() and path_file.read_text(encoding='ascii').strip():
                    break
                if proc.poll() is not None:
                    output = proc.stdout.read() if proc.stdout else ''
                    return _fail(f'Mock exited early:\n{output}')
                time.sleep(0.1)
            else:
                return _fail('Timed out waiting for mock COM endpoint file')

            client_port = path_file.read_text(encoding='ascii').strip()
            if client_port != com_peer:
                return _fail(f'Expected client port {com_peer}, got {client_port!r}')

            print(f'com0com pair {com_server} <-> {com_peer}')
            print(f'Mock on {com_server}, client port {client_port}')
            esptool = subprocess.run(
                [
                    sys.executable, '-m', 'esptool',
                    '--chip', 'esp32',
                    '--port', client_port,
                    '--no-stub',
                    '--before', 'no-reset',
                    '--after', 'no-reset',
                    'write-flash',
                    '0x10000', str(bin_file),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if 'Wrote' not in esptool.stdout:
                return _fail(
                    f'esptool failed (rc={esptool.returncode})\n'
                    f'stdout:\n{esptool.stdout[-800:]}\n'
                    f'stderr:\n{esptool.stderr[-400:]}',
                )
            print('esptool write-flash: OK')
            return 0
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == '__main__':
    raise SystemExit(main())
