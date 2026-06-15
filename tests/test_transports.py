# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""TCP and PTY transport integration tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import os

import esp32_mock_bootloader.testing as mock
from esp32_mock_bootloader import chips

pytestmark = [pytest.mark.transport, pytest.mark.esptool]


@pytest.mark.parametrize('transport', mock.constants.TRANSPORTS)
@pytest.mark.parametrize('chip', mock.constants.ESPTOOL_CHIPS)
def test_protocol_smoke(transport: str, chip: str):
    with mock.server.running_mock(transport, chip, timeout=30.0) as (proc, port, pty_path):
        assert proc.poll() is None
        sock = mock.server.connect_transport(transport, port=port, pty_path=pty_path)
        try:
            assert mock.protocol.minimal_plain_flash(sock)
        finally:
            sock.close()


@pytest.mark.parametrize('transport', mock.constants.TRANSPORTS)
@pytest.mark.parametrize('chip', mock.constants.ESPTOOL_CHIPS)
def test_explicit_chip_detect_register(transport: str, chip: str):
    profile = chips.PROFILES[chip]
    if not profile.detect_magic:
        pytest.skip(f'{chip} has no detect magic register')
    with mock.server.running_mock(transport, chip, timeout=30.0) as (_proc, port, pty_path):
        sock = mock.server.connect_transport(transport, port=port, pty_path=pty_path)
        try:
            mock.protocol.send_sync(sock)
            value = mock.protocol.read_reg_value(sock, profile.detect_reg)
            assert value == profile.detect_magic
        finally:
            sock.close()


@pytest.mark.parametrize('transport', mock.constants.TRANSPORTS)
@pytest.mark.parametrize('chip', mock.constants.ESPTOOL_CHIPS)
def test_chip_profile_registers(transport: str, chip: str):
    profile = chips.PROFILES[chip]
    with mock.server.running_mock(transport, chip, timeout=30.0) as (_proc, port, pty_path):
        sock = mock.server.connect_transport(transport, port=port, pty_path=pty_path)
        try:
            mock.protocol.send_sync(sock)
            if profile.detect_magic:
                value = mock.protocol.read_reg_value(sock, profile.detect_reg)
                assert value == profile.detect_magic
            efuse_value = mock.protocol.read_reg_value(sock, profile.efuse_base + 0x04)
            assert efuse_value == 0
            assert mock.protocol.minimal_plain_flash(sock)
        finally:
            sock.close()


@pytest.mark.parametrize('transport', mock.constants.TRANSPORTS)
@pytest.mark.parametrize('chip', mock.constants.ESPTOOL_CHIPS)
def test_esptool_write_flash(transport: str, chip: str):
    with mock.server.running_mock(transport, chip, timeout=30.0) as (_proc, port, pty_path):
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = mock.esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
            wrote_ok, detail = mock.esptool.write_flash_no_stub(
                chip, str(bin_file), port=port, pty_path=pty_path,
            )
            assert wrote_ok, detail


@pytest.mark.parametrize('transport', mock.constants.TRANSPORTS)
@pytest.mark.parametrize('chip', mock.constants.ESPTOOL_CHIPS)
def test_auto_mode_esptool_write_flash(transport: str, chip: str):
    with mock.server.running_mock(transport, 'auto', timeout=30.0) as (_proc, port, pty_path):
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = mock.esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
            wrote_ok, detail = mock.esptool.write_flash_no_stub(
                chip, str(bin_file), port=port, pty_path=pty_path,
            )
            assert wrote_ok, detail


def test_pty_path_file_written(tmp_path: Path):
    path_file = tmp_path / 'mock.pty'
    proc = None
    try:
        proc = mock.server.start_pty(path_file, timeout=30.0, chip='auto')
        endpoint = mock.server.read_pty_path(path_file)
        assert endpoint
        if os.environ.get('ESP32_MOCK_COM_PORT'):
            assert endpoint == os.environ.get('ESP32_MOCK_COM_PEER', 'COM19')
        elif os.name == 'nt':
            assert endpoint.startswith('socket://')
        else:
            assert Path(endpoint).exists()
        sock = mock.server.connect_transport('pty', pty_path=endpoint)
        try:
            assert mock.protocol.minimal_plain_flash(sock)
        finally:
            sock.close()
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.skipif(os.name != 'nt', reason='Windows com0com test')
def test_windows_com0com_esptool():
    """Creates a com0com pair via setupc (requires admin + com0com installed)."""
    from esp32_mock_bootloader.com0com import Com0ComError, com0com_pair

    com_server = os.environ.get('ESP32_MOCK_COM_PORT', 'COM18')
    com_peer = os.environ.get('ESP32_MOCK_COM_PEER', 'COM19')
    try:
        with com0com_pair(com_server, com_peer) as pair:
            os.environ['ESP32_MOCK_COM_PORT'] = pair.server
            os.environ['ESP32_MOCK_COM_PEER'] = pair.peer
            with mock.server.running_mock('pty', 'auto', timeout=60.0) as (_proc, _port, endpoint):
                with tempfile.TemporaryDirectory() as tmp:
                    bin_file = mock.esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
                    assert endpoint == pair.peer
                    wrote_ok, detail = mock.esptool.write_flash_no_stub(
                        'esp32', str(bin_file), pty_path=endpoint,
                    )
                    assert wrote_ok, detail
    except Com0ComError as exc:
        pytest.skip(str(exc))


def test_tcp_daemon_start_stop(tmp_path: Path):
    """TCP daemon CLI path (PTY has no background daemon)."""
    from esp32_mock_bootloader import daemon

    port = mock.server.reserve_tcp_port()
    state_dir = tmp_path / 'state'
    try:
        data = daemon.start_daemon(port=port, chip_mode='auto', base=state_dir)
        assert data['url'] == f'socket://127.0.0.1:{port}'
        status = daemon.daemon_status(port, state_dir)
        assert status['running']
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = mock.esptool.create_fake_binary(Path(tmp) / 'test.bin', 512)
            wrote_ok, detail = mock.esptool.write_flash_no_stub(
                'esp32', str(bin_file), port=port,
            )
            assert wrote_ok, detail
    finally:
        daemon.stop_daemon(port, state_dir)
