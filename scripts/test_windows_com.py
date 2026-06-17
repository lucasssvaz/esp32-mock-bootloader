#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Test esp32-mock-bootloader over a com0com virtual COM pair on Windows.

Requires com0com (https://sourceforge.net/projects/com0com/) installed and
setupc.exe runnable with administrator privileges (creates/removes the pair).

  python scripts/test_windows_com.py

Override ports with ESP32_MOCK_SERIAL_BIND / ESP32_MOCK_PORT
(legacy ESP32_MOCK_COM_PORT / ESP32_MOCK_COM_PEER also work; defaults COM18/COM19).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from esp32_mock_bootloader.com0com import Com0ComError, com0com_pair
from esp32_mock_bootloader import constants, protocol_client
from tests.helpers import esptool

COM_BIND = os.environ.get('ESP32_MOCK_SERIAL_BIND') or os.environ.get('ESP32_MOCK_COM_PORT', 'COM18')
COM_PORT = os.environ.get('ESP32_MOCK_PORT') or os.environ.get('ESP32_MOCK_COM_PEER', 'COM19')


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main() -> int:
    if os.name != 'nt':
        return _fail('This script is for Windows with com0com installed.')

    try:
        with com0com_pair(COM_BIND, COM_PORT) as pair:
            return _run_flash_test(pair.server, pair.peer)
    except Com0ComError as exc:
        return _fail(str(exc))


def _run_flash_test(serial_bind: str, client_port: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path_file = Path(tmp) / 'mock-com.path'
        bin_file = Path(tmp) / 'test.bin'
        bin_file.write_bytes(b'\x00' * 1024)

        proc = subprocess.Popen(
            [
                sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
                '--pty',
                '--port-file', str(path_file),
                '--port', client_port,
                '--chip', 'esp32',
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

            endpoint = path_file.read_text(encoding='ascii').strip()
            if endpoint != client_port:
                return _fail(f'Expected client port {client_port}, got {endpoint!r}')

            print(f'com0com pair {serial_bind} <-> {client_port}')
            print(f'Mock on {serial_bind}, client port {client_port}')

            flash_id = subprocess.run(
                [
                    sys.executable, '-m', 'esptool',
                    '--chip', 'esp32',
                    '--port', client_port,
                    '--no-stub',
                    '--before', 'no-reset',
                    '--after', 'no-reset',
                    'flash-id',
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            fid_output = flash_id.stdout + flash_id.stderr
            if flash_id.returncode != 0:
                return _fail(
                    f'flash-id failed (rc={flash_id.returncode})\n'
                    f'stdout:\n{flash_id.stdout[-800:]}\n'
                    f'stderr:\n{flash_id.stderr[-400:]}',
                )
            vid_pid_lines = [
                line.strip() for line in fid_output.splitlines()
                if 'VID/PID' in line
            ]
            if vid_pid_lines:
                print('VID/PID lines (expected on com0com):')
                for line in vid_pid_lines:
                    print(f'  {line}')
            warns = esptool.forbidden_warnings(fid_output, transport='pty')
            if warns:
                return _fail('Unexpected esptool warnings on flash-id:\n' + '\n'.join(warns))

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
