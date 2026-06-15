# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""esptool connect fidelity: no protocol warnings on flash-id."""

from __future__ import annotations

import pytest

from pathlib import Path

import esp32_mock_bootloader.testing as mock
from esp32_mock_bootloader import registers

pytestmark = pytest.mark.esptool


@pytest.mark.parametrize('chip', mock.constants.ESPTOOL_CHIPS)
def test_flash_id_matching_chip_no_protocol_warnings(chip: str):
    proc, port = mock.server.start_server(timeout=60.0, chip=chip)
    try:
        result = mock.esptool.run_flash_id(chip, port=port)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output[-800:]
        warns = mock.esptool.forbidden_warnings(output, transport='socket')
        assert warns == [], '\n'.join(warns)
        mac = registers.mac_bytes_for_chip(chip)
        mac_line = ':'.join(f'{b:02x}' for b in mac)
        assert mac_line in output
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.parametrize('chip', ('esp32', 'esp32c3', 'esp8266'))
def test_flash_id_esptool_auto_detects_fixed_mock(chip: str):
    """esptool --chip auto uses ROM probes; mock --chip X must match the virtual SoC."""
    if chip not in mock.constants.ESPTOOL_CHIPS:
        pytest.skip(f'{chip} not supported by installed esptool')
    proc, port = mock.server.start_server(timeout=60.0, chip=chip)
    try:
        result = mock.esptool.run_esptool(
            '--chip', 'auto',
            '--port', f'socket://localhost:{port}',
            '--before', 'no-reset',
            '--after', 'no-reset',
            '--no-stub',
            'flash-id',
            timeout=60.0,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output[-800:]
        warns = mock.esptool.forbidden_warnings(output, transport='socket')
        assert warns == [], '\n'.join(warns)
        mac = registers.mac_bytes_for_chip(chip)
        mac_line = ':'.join(f'{b:02x}' for b in mac)
        assert mac_line in output
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.transport
@pytest.mark.parametrize('chip', ('esp32', 'esp32c3'))
def test_flash_id_pty_no_protocol_warnings(chip: str, tmp_path: Path):
    path_file = tmp_path / 'mock.pty'
    proc = mock.server.start_pty(path_file, timeout=60.0, chip=chip)
    try:
        pty_path = mock.server.read_pty_path(path_file)
        result = mock.esptool.run_flash_id(chip, pty_path=pty_path)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output[-800:]
        warns = mock.esptool.forbidden_warnings(output, transport='pty')
        assert warns == [], '\n'.join(warns)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
