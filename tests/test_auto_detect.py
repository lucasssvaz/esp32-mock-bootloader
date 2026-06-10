# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Chip auto-detection tests."""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

import pytest

from esp32_mock_bootloader import daemon
from esp32_mock_bootloader.chip_profiles import CHIP_PROFILES, LEGACY_DETECT_REG
from conftest import (
    CMD_GET_SECURITY_INFO,
    ESPTOOL_CHIPS,
    reserve_tcp_port,
    chips_with_unique_efuse,
    connect_to_server,
    create_fake_binary,
    esptool_write_flash_no_stub,
    make_command,
    parse_response,
    read_reg_value,
    send_and_receive,
    send_sync,
    slip_decode_frames,
    start_mock_server,
)

pytestmark = pytest.mark.esptool


def test_auto_legacy_detect_reg_deferred_until_known():
    """Legacy detect register must not return magic before chip-specific evidence."""
    proc, port = start_mock_server(chip='auto')
    try:
        sock = connect_to_server(port)
        send_sync(sock)
        raw = send_and_receive(
            sock, make_command(0x0A, struct.pack('<I', LEGACY_DETECT_REG)),
        )
        frames = slip_decode_frames(raw)
        assert len(frames) >= 1
        _d, _c, _s, value, _data = parse_response(frames[0])
        assert value == 0
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.parametrize('chip', chips_with_unique_efuse())
def test_auto_read_reg_detects_via_unique_efuse(chip: str):
    profile = CHIP_PROFILES[chip]
    proc, port = start_mock_server(chip='auto')
    try:
        sock = connect_to_server(port)
        send_sync(sock)
        efuse_addr = profile.efuse_base + 0x04
        raw = send_and_receive(sock, make_command(0x0A, struct.pack('<I', efuse_addr)))
        frames = slip_decode_frames(raw)
        assert len(frames) >= 1
        _d, _c, _s, value, _data = parse_response(frames[0])
        assert value == 0

        if profile.detect_magic:
            raw = send_and_receive(
                sock, make_command(0x0A, struct.pack('<I', profile.detect_reg)),
            )
            frames = slip_decode_frames(raw)
            _d, _c, _s, value, _data = parse_response(frames[0])
            assert value == profile.detect_magic
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_get_security_info_after_auto_detection():
    chip = next(
        c for c, p in CHIP_PROFILES.items()
        if p.detect_magic and p.detect_reg != LEGACY_DETECT_REG
    )
    profile = CHIP_PROFILES[chip]
    proc, port = start_mock_server(chip='auto')
    try:
        sock = connect_to_server(port)
        send_sync(sock)
        send_and_receive(
            sock, make_command(0x0A, struct.pack('<I', profile.detect_reg)),
        )
        raw = send_and_receive(sock, make_command(CMD_GET_SECURITY_INFO))
        frames = slip_decode_frames(raw)
        _d, c, _s, _v, data = parse_response(frames[0])
        assert c == CMD_GET_SECURITY_INFO
        assert data[0] == 0
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_get_security_info_unknown_auto_returns_error():
    proc, port = start_mock_server(chip='auto')
    try:
        sock = connect_to_server(port)
        send_sync(sock)
        raw = send_and_receive(sock, make_command(CMD_GET_SECURITY_INFO))
        frames = slip_decode_frames(raw)
        assert len(frames) >= 1
        _d, c, _s, _v, data = parse_response(frames[0])
        assert c == CMD_GET_SECURITY_INFO
        assert data and data[0] != 0  # error status before detection
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.parametrize('chip', ESPTOOL_CHIPS)
def test_auto_mode_esptool_per_soc(chip: str):
    proc, port = start_mock_server(timeout=30.0, chip='auto')
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = create_fake_binary(Path(tmp) / 'test.bin', 1024)
            wrote_ok, detail = esptool_write_flash_no_stub(
                chip, str(bin_file), port=port,
            )
            assert wrote_ok, detail
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.parametrize('chip', ESPTOOL_CHIPS)
def test_explicit_chip_detect_register(chip: str):
    profile = CHIP_PROFILES[chip]
    proc, port = start_mock_server(chip=chip)
    try:
        sock = connect_to_server(port)
        send_sync(sock)
        if profile.detect_magic:
            value = read_reg_value(sock, profile.detect_reg)
            assert value == profile.detect_magic
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_daemon_auto_start_detected_chip(tmp_path):
    port = reserve_tcp_port()
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
            bin_file = create_fake_binary(Path(tmp) / 'test.bin', 512)
            wrote_ok, detail = esptool_write_flash_no_stub(
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
