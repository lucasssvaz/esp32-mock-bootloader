# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""In-process server unit tests (no subprocess)."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from esp32_mock_bootloader.chip_profiles import CHIP_PROFILES, LEGACY_DETECT_REG
from esp32_mock_bootloader.server import (
    BootloaderConnection,
    ChipSession,
    FlashImage,
    _make_on_detected,
    handle_client,
    handle_get_security_info,
    handle_read_reg,
    resolve_com_ports,
    slip_decode_frames,
)
from conftest import (
    CMD_FLASH_END,
    CMD_GET_SECURITY_INFO,
    CMD_SYNC,
    CMD_WRITE_REG,
    make_command,
    parse_response,
    send_and_receive,
    send_sync,
    slip_decode_frames as conftest_slip_decode,
)


def test_chip_session_rejects_unknown_chip():
    with pytest.raises(ValueError, match='unknown chip'):
        ChipSession('not-a-chip')


def test_resolve_com_ports_from_env(monkeypatch):
    monkeypatch.setenv('ESP32_MOCK_COM_PORT', 'COM10')
    monkeypatch.setenv('ESP32_MOCK_COM_PEER', 'COM11')
    assert resolve_com_ports() == ('COM10', 'COM11')
    assert resolve_com_ports('COM20', 'COM21') == ('COM20', 'COM21')


def test_resolve_com_ports_partial_raises():
    with pytest.raises(ValueError, match='both com port'):
        resolve_com_ports('COM10', None)


def test_bootloader_connection_requires_exactly_one_mode():
    with pytest.raises(ValueError, match='exactly one'):
        BootloaderConnection()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ValueError, match='exactly one'):
            BootloaderConnection(sock=sock, master_fd=1)
    finally:
        sock.close()


def test_get_security_info_esp8266_returns_error():
    session = ChipSession('esp8266')
    raw = handle_get_security_info(session)
    frames = conftest_slip_decode(raw)
    _d, c, _s, _v, data = parse_response(frames[0])
    assert c == CMD_GET_SECURITY_INFO
    assert data[0] != 0


def test_get_security_info_after_explicit_chip():
    session = ChipSession('esp32')
    raw = handle_get_security_info(session)
    frames = conftest_slip_decode(raw)
    _d, c, _s, _v, data = parse_response(frames[0])
    assert c == CMD_GET_SECURITY_INFO
    assert data[0] == 0
    chip_id = struct.unpack_from('<I', data, 12)[0]
    assert chip_id == CHIP_PROFILES['esp32'].image_chip_id


def test_handle_read_reg_legacy_deferred_in_auto():
    session = ChipSession('auto')
    raw = handle_read_reg(struct.pack('<I', LEGACY_DETECT_REG), session)
    frames = conftest_slip_decode(raw)
    assert parse_response(frames[0])[3] == 0


def test_handle_read_reg_detects_unique_magic():
    chip = next(
        c for c, p in CHIP_PROFILES.items()
        if p.detect_magic and p.detect_reg != LEGACY_DETECT_REG
    )
    profile = CHIP_PROFILES[chip]
    session = ChipSession('auto')
    raw = handle_read_reg(struct.pack('<I', profile.detect_reg), session)
    frames = conftest_slip_decode(raw)
    assert parse_response(frames[0])[3] == profile.detect_magic
    assert session.detected_chip == chip


def test_flash_image_md5_and_defl_error_paths():
    flash = FlashImage()
    flash.begin_defl(struct.pack('<IIII', 0x100, 1, 0x100, 0x10000))
    flash.write_defl_block(b'\x00' * 8)  # too short
    flash.write_defl_block(
        struct.pack('<IIII', 4, 0, 0, 0) + b'\xff\xff\xff\xff',
    )  # invalid zlib
    flash.end_defl()
    rom = flash.md5_stub_response(struct.pack('<IIII', 0, 16, 0, 0))
    frames = slip_decode_frames(rom)
    _d, cmd, _s, _v, data = parse_response(frames[0])
    assert cmd == 0x13
    assert data[:32] == hashlib.md5(bytes(flash.data[0:16])).hexdigest().encode('ascii')
    assert data[32:34] == b'\x00\x00'

    stub = flash.md5_stub_response(struct.pack('<IIII', 0, 16, 0, 0), stub=True)
    _d, cmd, _s, _v, data = parse_response(slip_decode_frames(stub)[0])
    assert data[:16] == hashlib.md5(bytes(flash.data[0:16])).digest()
    assert data[16:18] == b'\x00\x00'


def test_make_on_detected_updates_state_file(tmp_path: Path):
    state_file = tmp_path / 'state.json'
    state_file.write_text(
        json.dumps({'chip': 'auto', 'detected_chip': None}) + '\n',
        encoding='utf-8',
    )
    callback = _make_on_detected(str(state_file))
    assert callback is not None
    callback('esp32')
    state = json.loads(state_file.read_text(encoding='utf-8'))
    assert state['detected_chip'] == 'esp32'
    callback('esp32')  # idempotent


@pytest.fixture
def bootloader_client():
    """In-process TCP connection to handle_client (same thread pool as production)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop_event = threading.Event()

    def serve() -> None:
        conn, _addr = srv.accept()
        handle_client(BootloaderConnection(sock=conn), stop_event, 'esp32')

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(5.0)
    client.connect(('127.0.0.1', port))
    try:
        yield client
    finally:
        stop_event.set()
        client.close()
        srv.close()
        thread.join(timeout=2)


def test_handle_client_write_reg(bootloader_client):
    send_sync(bootloader_client)
    raw = send_and_receive(
        bootloader_client,
        make_command(CMD_WRITE_REG, struct.pack('<II', 0x3FF00000, 1)),
    )
    frames = conftest_slip_decode(raw)
    assert frames
    assert parse_response(frames[0])[1] == CMD_WRITE_REG


def test_handle_client_flash_end_idle_disconnect(bootloader_client):
    send_sync(bootloader_client)
    send_and_receive(bootloader_client, make_command(CMD_FLASH_END, struct.pack('<I', 1)))
    # handle_client exits after >3s idle once flash_ended (1s recv timeouts).
    time.sleep(5.0)
    bootloader_client.settimeout(1.0)
    try:
        bootloader_client.sendall(make_command(CMD_SYNC, b'\x55' * 36))
        assert bootloader_client.recv(64) == b''
    except (ConnectionResetError, BrokenPipeError, OSError, socket.timeout):
        pass
