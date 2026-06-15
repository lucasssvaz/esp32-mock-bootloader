# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Chip auto-detection tests."""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

import pytest

from esp32_mock_bootloader import protocol
from esp32_mock_bootloader import daemon
from esp32_mock_bootloader import chips
import esp32_mock_bootloader.testing as mock

pytestmark = pytest.mark.esptool


def test_auto_legacy_detect_reg_deferred_until_known():
    """Legacy detect register must not return magic before chip-specific evidence."""
    proc, port = mock.server.start_server(chip='auto')
    try:
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        raw = mock.protocol.send_and_receive(
            sock, mock.protocol.make_command(0x0A, struct.pack('<I', chips.LEGACY_DETECT_REG)),
        )
        frames = mock.protocol.slip_decode_frames(raw)
        assert len(frames) >= 1
        _d, _c, _s, value, _data = mock.protocol.parse_response(frames[0])
        assert value == 0
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.parametrize('chip', chips.chips_with_unique_efuse())
def test_auto_read_reg_detects_via_unique_efuse(chip: str):
    profile = chips.PROFILES[chip]
    proc, port = mock.server.start_server(chip='auto')
    try:
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        efuse_addr = profile.efuse_base + 0x04
        raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(0x0A, struct.pack('<I', efuse_addr)))
        frames = mock.protocol.slip_decode_frames(raw)
        assert len(frames) >= 1
        _d, _c, _s, value, _data = mock.protocol.parse_response(frames[0])
        assert value == 0

        if profile.detect_magic:
            raw = mock.protocol.send_and_receive(
                sock, mock.protocol.make_command(0x0A, struct.pack('<I', profile.detect_reg)),
            )
            frames = mock.protocol.slip_decode_frames(raw)
            _d, _c, _s, value, _data = mock.protocol.parse_response(frames[0])
            assert value == profile.detect_magic
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_get_security_info_after_auto_detection():
    chip = next(
        c for c, p in chips.PROFILES.items()
        if p.detect_magic and p.detect_reg != chips.LEGACY_DETECT_REG
    )
    profile = chips.PROFILES[chip]
    proc, port = mock.server.start_server(chip='auto')
    try:
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        mock.protocol.send_and_receive(
            sock, mock.protocol.make_command(0x0A, struct.pack('<I', profile.detect_reg)),
        )
        raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_GET_SECURITY_INFO))
        frames = mock.protocol.slip_decode_frames(raw)
        _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
        assert c == protocol.CMD_GET_SECURITY_INFO
        assert data[0] == 0
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_get_security_info_unknown_auto_returns_error():
    proc, port = mock.server.start_server(chip='auto')
    try:
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_GET_SECURITY_INFO))
        frames = mock.protocol.slip_decode_frames(raw)
        assert len(frames) >= 1
        _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
        assert c == protocol.CMD_GET_SECURITY_INFO
        assert data and data[0] != 0  # error status before detection
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.parametrize('chip', mock.constants.ESPTOOL_CHIPS)
def test_auto_mode_esptool_per_soc(chip: str):
    proc, port = mock.server.start_server(timeout=30.0, chip='auto')
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = mock.esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
            wrote_ok, detail = mock.esptool.write_flash_no_stub(
                chip, str(bin_file), port=port,
            )
            assert wrote_ok, detail
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.parametrize('chip', mock.constants.ESPTOOL_CHIPS)
def test_explicit_chip_detect_register(chip: str):
    profile = chips.PROFILES[chip]
    proc, port = mock.server.start_server(chip=chip)
    try:
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        if profile.detect_magic:
            value = mock.protocol.read_reg_value(sock, profile.detect_reg)
            assert value == profile.detect_magic
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_daemon_auto_start_detected_chip(tmp_path):
    port = mock.server.reserve_tcp_port()
    state_dir = tmp_path / 'state'
    try:
        data = daemon.start_daemon(
            port=port, chip_mode='auto', base=state_dir,
        )
        assert data['chip'] == 'auto'
        status = daemon.daemon_status(port, state_dir)
        assert status['running']
        assert status['url'] == f'socket://127.0.0.1:{port}'

        with tempfile.TemporaryDirectory() as tmp:
            bin_file = mock.esptool.create_fake_binary(Path(tmp) / 'test.bin', 512)
            wrote_ok, detail = mock.esptool.write_flash_no_stub(
                'esp32', str(bin_file), port=port,
            )
            assert wrote_ok, detail

        status = daemon.daemon_status(port, state_dir)
        assert status['detected_chip'] == 'esp32'
        state = daemon.read_state(port, state_dir)
        assert state is not None
        assert state.get('detected_chip') == 'esp32'
    finally:
        daemon.stop_daemon(port, state_dir)
