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

from esp32_mock_bootloader import protocol
from esp32_mock_bootloader import chips
from esp32_mock_bootloader import server
import esp32_mock_bootloader.testing as mock


def test_chip_session_rejects_unknown_chip():
    with pytest.raises(ValueError, match='unknown chip'):
        server.ChipSession('not-a-chip')


def test_is_serial_port_name():
    assert server.is_serial_port_name('COM19')
    assert server.is_serial_port_name('/dev/ttyUSB0')
    assert not server.is_serial_port_name('9876')
    assert not server.is_serial_port_name('29876')


def test_resolve_serial_pair_from_env(monkeypatch):
    monkeypatch.setenv('ESP32_MOCK_SERIAL_BIND', 'COM10')
    monkeypatch.setenv('ESP32_MOCK_PORT', 'COM11')
    assert server.resolve_serial_pair() == ('COM10', 'COM11')
    assert server.resolve_serial_pair('COM20', 'COM21') == ('COM21', 'COM20')


def test_resolve_serial_pair_legacy_env(monkeypatch):
    monkeypatch.setenv('ESP32_MOCK_COM_PORT', 'COM10')
    monkeypatch.setenv('ESP32_MOCK_COM_PEER', 'COM11')
    assert server.resolve_serial_pair() == ('COM10', 'COM11')


def test_resolve_serial_pair_missing_peer_raises():
    with pytest.raises(ValueError, match='null-modem pair'):
        server.resolve_serial_pair('COM99', None)


def test_bootloader_connection_requires_exactly_one_mode():
    with pytest.raises(ValueError, match='exactly one'):
        server.BootloaderConnection()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ValueError, match='exactly one'):
            server.BootloaderConnection(sock=sock, pty=server.PtyMasterTransport(0, 1))
    finally:
        sock.close()


def test_get_security_info_esp8266_returns_error():
    session = server.ChipSession('esp8266')
    raw = server.handle_get_security_info(session)
    frames = server.slip_decode_frames(raw)
    _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_GET_SECURITY_INFO
    assert data[0] != 0


def test_get_security_info_esp32_returns_error():
    session = server.ChipSession('esp32')
    raw = server.handle_get_security_info(session)
    frames = server.slip_decode_frames(raw)
    _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_GET_SECURITY_INFO
    assert data[0] != 0


def test_get_security_info_modern_chip():
    session = server.ChipSession('esp32c3')
    raw = server.handle_get_security_info(session)
    frames = server.slip_decode_frames(raw)
    _d, c, size, _v, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_GET_SECURITY_INFO
    assert size == 22
    assert data[0] == 0
    chip_id = struct.unpack_from('<I', data, 12)[0]
    assert chip_id == chips.PROFILES['esp32c3'].image_chip_id
    assert data[20:22] == b'\x00\x00'


def test_handle_read_reg_legacy_deferred_in_auto():
    session = server.ChipSession('auto')
    raw = server.handle_read_reg(struct.pack('<I', chips.LEGACY_DETECT_REG), session)
    frames = server.slip_decode_frames(raw)
    assert mock.protocol.parse_response(frames[0])[3] == 0


def test_handle_read_reg_detects_unique_magic():
    chip = next(
        c for c, p in chips.PROFILES.items()
        if p.detect_magic and p.detect_reg != chips.LEGACY_DETECT_REG
    )
    profile = chips.PROFILES[chip]
    session = server.ChipSession('auto')
    raw = server.handle_read_reg(struct.pack('<I', profile.detect_reg), session)
    frames = server.slip_decode_frames(raw)
    assert mock.protocol.parse_response(frames[0])[3] == profile.detect_magic
    assert session.detected_chip == chip


def test_flash_image_md5_and_defl_error_paths():
    flash = server.FlashImage()
    flash.begin_defl(struct.pack('<IIII', 0x100, 1, 0x100, 0x10000))
    flash.write_defl_block(b'\x00' * 8)  # too short
    flash.write_defl_block(
        struct.pack('<IIII', 4, 0, 0, 0) + b'\xff\xff\xff\xff',
    )  # invalid zlib
    flash.end_defl()
    rom = flash.md5_stub_response(struct.pack('<IIII', 0, 16, 0, 0))
    frames = mock.protocol.slip_decode_frames(rom)
    _d, cmd, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert cmd == 0x13
    assert data[:32] == hashlib.md5(bytes(flash.data[0:16])).hexdigest().encode('ascii')
    assert data[32:34] == b'\x00\x00'

    stub = flash.md5_stub_response(struct.pack('<IIII', 0, 16, 0, 0), stub=True)
    _d, cmd, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(stub)[0])
    assert data[:16] == hashlib.md5(bytes(flash.data[0:16])).digest()
    assert data[16:18] == b'\x00\x00'


def test_make_on_detected_updates_state_file(tmp_path: Path):
    state_file = tmp_path / 'state.json'
    state_file.write_text(
        json.dumps({'chip': 'auto', 'detected_chip': None}) + '\n',
        encoding='utf-8',
    )
    callback = server._make_on_detected(str(state_file))
    assert callback is not None
    callback('esp32')
    state = json.loads(state_file.read_text(encoding='utf-8'))
    assert state['detected_chip'] == 'esp32'
    callback('esp32')  # idempotent


@pytest.fixture
def bootloader_client():
    """In-process TCP connection to server.handle_client (same thread pool as production)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop_event = threading.Event()

    def serve() -> None:
        conn, _addr = srv.accept()
        server.handle_client(server.BootloaderConnection(sock=conn), stop_event, 'esp32')

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
    mock.protocol.send_sync(bootloader_client)
    raw = mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(protocol.CMD_WRITE_REG, struct.pack('<IIII', 0x3FF00000, 1, 0xFFFFFFFF, 0)),
    )
    frames = server.slip_decode_frames(raw)
    assert frames
    assert mock.protocol.parse_response(frames[0])[1] == protocol.CMD_WRITE_REG


def test_handle_client_flash_end_idle_disconnect(bootloader_client):
    mock.protocol.send_sync(bootloader_client)
    mock.protocol.send_and_receive(bootloader_client, mock.protocol.make_command(protocol.CMD_FLASH_END, struct.pack('<I', 1)))
    # server.handle_client exits after _FLASH_END_IDLE_SEC with no further traffic.
    time.sleep(server._FLASH_END_IDLE_SEC + 0.5)
    bootloader_client.settimeout(1.0)
    try:
        bootloader_client.sendall(mock.protocol.make_command(protocol.CMD_SYNC, b'\x55' * 36))
        assert bootloader_client.recv(64) == b''
    except (ConnectionResetError, BrokenPipeError, OSError, socket.timeout):
        pass


def test_chip_session_image_chip_id_undetected():
    session = server.ChipSession('auto')
    assert session.image_chip_id() == 0


def test_get_security_info_auto_before_detection():
    session = server.ChipSession('auto')
    raw = server.handle_get_security_info(session)
    frames = server.slip_decode_frames(raw)
    _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_GET_SECURITY_INFO
    assert data[0] != 0


def test_handle_write_reg_short_payload():
    session = server.ChipSession('esp32')
    raw = server.handle_write_reg(b'\x00' * 8, session)
    frames = server.slip_decode_frames(raw)
    _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_WRITE_REG
    assert data[0] == 1


def test_make_on_detected_corrupt_state(tmp_path: Path):
    state_file = tmp_path / 'state.json'
    state_file.write_text('{bad', encoding='utf-8')
    callback = server._make_on_detected(str(state_file))
    assert callback is not None
    callback('esp32')  # should not raise


def test_handle_read_reg_shared_efuse_returns_zero():
    session = server.ChipSession('auto')
    # C3 family shares efuse window early in connect — respond 0 without guessing.
    addr = chips.PROFILES['esp32c3'].efuse_base + 0x04
    raw = server.handle_read_reg(struct.pack('<I', addr), session)
    frames = server.slip_decode_frames(raw)
    assert mock.protocol.parse_response(frames[0])[3] == 0


def test_handle_client_stub_read_flash(bootloader_client):
    mock.protocol.send_sync(bootloader_client)
    mock.protocol.activate_stub(bootloader_client)
    offset = mock.constants.FLASH_APP_OFFSET
    length = 0x200
    mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', length, 1, length, offset),
        ),
    )
    block = b'\x5A' * length
    payload = struct.pack('<IIII', length, 0, 0, 0) + block
    mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload),
    )
    data, digest = mock.protocol.stub_read_flash(bootloader_client, offset, length)
    assert data == block
    assert digest == hashlib.md5(block).digest()


def test_handle_client_rom_read_flash_slow(bootloader_client):
    mock.protocol.send_sync(bootloader_client)
    offset = 0x20000
    length = 0x20
    mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', length, 1, length, offset),
        ),
    )
    block = b'\xAB' * length
    payload = struct.pack('<IIII', length, 0, 0, 0) + block
    mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload),
    )
    raw = mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(protocol.CMD_READ_FLASH_SLOW, struct.pack('<II', offset, length)),
    )
    _d, c, _s, _v, data = mock.protocol.parse_response(server.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_READ_FLASH_SLOW
    assert data[:length] == block


def test_handle_client_flash_data_checksum_error(bootloader_client):
    mock.protocol.send_sync(bootloader_client)
    mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, mock.constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\xAB' * 0x100)
    raw = mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload, checksum=0),
    )
    _d, c, _s, _v, data = mock.protocol.parse_response(server.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_FLASH_DATA
    assert data[0] == 1
    assert data[1] == protocol.ROM_CHECKSUM_ERROR


def test_handle_client_mem_end_ohai(bootloader_client):
    mock.protocol.send_sync(bootloader_client)
    mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(
            protocol.CMD_MEM_BEGIN,
            struct.pack('<IIII', 0x10, 1, 0x10, 0x40370000),
        ),
    )
    payload = struct.pack('<IIII', 0x10, 0, 0, 0) + (b'\x00' * 0x10)
    mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(protocol.CMD_MEM_DATA, payload),
    )
    raw = mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(protocol.CMD_MEM_END, struct.pack('<II', 0, 0x40370000)),
    )
    assert mock.constants.OHAI_BYTES in raw


def test_handle_client_unknown_command_rom_vs_stub(bootloader_client):
    mock.protocol.send_sync(bootloader_client)
    raw = mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(0xFF, b''),
    )
    _d, c, _s, _v, data = mock.protocol.parse_response(server.slip_decode_frames(raw)[0])
    assert c == 0xFF
    assert data[1] == protocol.ROM_INVALID_MESSAGE

    mock.protocol.activate_stub(bootloader_client)
    raw = mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(0xFE, b''),
    )
    _d, c, _s, _v, data = mock.protocol.parse_response(server.slip_decode_frames(raw)[0])
    assert c == 0xFE
    assert data[1] == protocol.STUB_UNIMPLEMENTED


def test_cli_run_exits_on_client_disconnect():
    port = mock.server.reserve_tcp_port()
    proc = None
    try:
        proc, listen_port = mock.server.start_server(
            port=port, timeout=30.0, chip='esp32', exit_on_disconnect=True,
        )
        sock = mock.server.connect(listen_port)
        mock.protocol.send_sync(sock)
        sock.close()
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc is not None and proc.poll() is None:
            mock.server.stop_subprocess(proc)


def test_spi_peripheral_out_of_range_and_flash_probes():
    spi = server.SpiPeripheralMock(0x3FF00000, usr2_offs=0x24, w0_offs=0x80)
    spi.write_reg(0x20000000, 0xFF, 0xFF)
    assert spi.read_reg(0x20000000) == 0
    spi.write_reg(spi.spi_base + 0x24, 0x9F, 0xFF)
    assert spi.read_reg(spi.spi_base + 0x80) == protocol.MOCK_FLASH_ID
    spi.write_reg(spi.spi_base + 0x24, 0x5A, 0xFF)
    assert spi.read_reg(spi.spi_base + 0x80) == protocol.SFDP_SIGNATURE
    spi.write_reg(spi.spi_base + 0x00, protocol.SPI_CMD_USR, protocol.SPI_CMD_USR)
    assert spi.read_reg(spi.spi_base + 0x00) == 0


def test_chip_session_note_detection_ignored_in_fixed_mode(capsys):
    session = server.ChipSession('esp32')
    session.note_detection('esp32c3', 'test')
    assert session.detected_chip == 'esp32'
    assert 'Detected chip' not in capsys.readouterr().out


def test_infer_chip_from_unique_detect_reg(monkeypatch):
    addr = 0xDEADBEEF
    fake_profiles = {
        'unique': chips.ChipProfile(detect_reg=addr, detect_magic=1, efuse_base=0, image_chip_id=1, uses_magic_value=False),
        'other': chips.ChipProfile(detect_reg=0xCAFEBABE, detect_magic=2, efuse_base=0, image_chip_id=2, uses_magic_value=False),
    }
    monkeypatch.setattr(chips, 'PROFILES', fake_profiles)
    assert server._infer_chip_from_addr(addr) == 'unique'
    assert server._infer_chip_from_addr(chips.LEGACY_DETECT_REG) is None


def test_handle_read_reg_inferred_when_already_detected():
    session = server.ChipSession('esp32c3')
    session.detected_chip = 'esp32c3'
    reg = chips.PROFILES['esp32c3'].detect_reg
    raw = server.handle_read_reg(struct.pack('<I', reg), session)
    assert mock.protocol.parse_response(server.slip_decode_frames(raw)[0])[3] != 0


def test_handle_read_reg_unique_efuse_detection():
    unique = chips.chips_with_unique_efuse()
    if not unique:
        pytest.skip('no unique efuse chips')
    chip = unique[0]
    addr = chips.PROFILES[chip].efuse_base + 0x04
    session = server.ChipSession('auto')
    server.handle_read_reg(struct.pack('<I', addr), session)
    assert session.detected_chip == chip


def test_flash_and_ram_short_write_blocks():
    server.FlashImage().write_plain_block(b'\x00' * 8)
    server.RamImage().write_block(b'\x00' * 8)


def test_slip_decode_unknown_escape_byte():
    raw = bytes([protocol.SLIP_END, protocol.SLIP_ESC, 0xAB, protocol.SLIP_END])
    frames = server.slip_decode_frames(raw)
    assert frames and frames[0] == bytes([0xAB])


def test_make_on_detected_write_oserror(tmp_path, monkeypatch):
    state_file = tmp_path / 'state.json'
    state_file.write_text('{"detected_chip": null}', encoding='utf-8')
    callback = server._make_on_detected(str(state_file))
    assert callback is not None
    real_open = open

    def guarded_open(path, mode='r', *args, **kwargs):
        if 'w' in mode:
            raise OSError('read only')
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr('builtins.open', guarded_open)
    callback('esp32')


def test_write_serial_endpoint_stdout(capsys):
    server._write_serial_endpoint(None, 'socket://127.0.0.1:1')
    assert 'socket://127.0.0.1:1' in capsys.readouterr().out


def test_recv_slip_payload_returns_none_on_close():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def accept_and_close() -> None:
        client, _addr = srv.accept()
        client.close()

    threading.Thread(target=accept_and_close, daemon=True).start()
    peer = socket.create_connection(('127.0.0.1', port))
    conn = server.BootloaderConnection(sock=peer)
    conn.set_timeout(0.1)
    assert server._recv_slip_payload(conn, threading.Event(), bytearray()) is None
    peer.close()
    srv.close()


def test_perform_read_flash_stream_invalid_params():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.perform_read_flash_stream(
            server.BootloaderConnection(sock=sock),
            threading.Event(),
            server.FlashImage(),
            0,
            0,
            0,
            1,
            bytearray(),
        )
    finally:
        sock.close()


def test_tcp_listen_loop_timeout():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    server._tcp_listen_loop(srv, timeout=0.05, chip_mode='esp32', on_detected=None, label='test')


def test_handle_client_run_user_code_stub(bootloader_client):
    mock.protocol.send_sync(bootloader_client)
    mock.protocol.activate_stub(bootloader_client)
    raw = mock.protocol.send_and_receive(
        bootloader_client,
        mock.protocol.make_command(protocol.CMD_RUN_USER_CODE, b''),
    )
    assert raw == b'' or server.slip_decode_frames(raw) == []

