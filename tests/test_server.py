# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Server, transport, connection, and chip auto-detection tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path

import pytest

from esp32_mock_bootloader import chips, daemon, mock_bootloader, protocol, server

from esp32_mock_bootloader import constants, process, protocol_client
from tests.helpers import esptool


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
    _d, c, _s, _v, data = protocol_client.parse_response(frames[0])
    assert c == protocol.CMD_GET_SECURITY_INFO
    assert data[0] != 0


def test_get_security_info_esp32_returns_error():
    session = server.ChipSession('esp32')
    raw = server.handle_get_security_info(session)
    frames = server.slip_decode_frames(raw)
    _d, c, _s, _v, data = protocol_client.parse_response(frames[0])
    assert c == protocol.CMD_GET_SECURITY_INFO
    assert data[0] != 0


def test_get_security_info_modern_chip():
    session = server.ChipSession('esp32c3')
    raw = server.handle_get_security_info(session)
    frames = server.slip_decode_frames(raw)
    _d, c, size, _v, data = protocol_client.parse_response(frames[0])
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
    assert protocol_client.parse_response(frames[0])[3] == 0


def test_handle_read_reg_detects_unique_magic():
    chip = next(
        c for c, p in chips.PROFILES.items()
        if p.detect_magic and p.detect_reg != chips.LEGACY_DETECT_REG
    )
    profile = chips.PROFILES[chip]
    session = server.ChipSession('auto')
    raw = server.handle_read_reg(struct.pack('<I', profile.detect_reg), session)
    frames = server.slip_decode_frames(raw)
    assert protocol_client.parse_response(frames[0])[3] == profile.detect_magic
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
    frames = protocol_client.slip_decode_frames(rom)
    _d, cmd, _s, _v, data = protocol_client.parse_response(frames[0])
    assert cmd == 0x13
    assert data[:32] == hashlib.md5(bytes(flash.data[0:16])).hexdigest().encode('ascii')
    assert data[32:34] == b'\x00\x00'

    stub = flash.md5_stub_response(struct.pack('<IIII', 0, 16, 0, 0), stub=True)
    _d, cmd, _s, _v, data = protocol_client.parse_response(protocol_client.slip_decode_frames(stub)[0])
    assert data[:16] == hashlib.md5(bytes(flash.data[0:16])).digest()
    assert data[16:18] == b'\x00\x00'


def test_make_on_detected_updates_registry():
    import os

    from esp32_mock_bootloader import daemon

    port = 39999
    daemon.register_instance(port, {
        'pid': os.getpid(),
        'port': port,
        'chip': 'auto',
        'detected_chip': None,
    })
    try:
        callback = server._make_on_detected(port)
        assert callback is not None
        callback('esp32')
        state = daemon.read_state(port)
        assert state is not None
        assert state['detected_chip'] == 'esp32'
        callback('esp32')  # idempotent
    finally:
        daemon.unregister_instance(port)


@pytest.fixture
def bootloader_client(request):
    """In-process TCP connection to server.handle_client (same thread pool as production)."""
    request.node.add_marker(pytest.mark.advanced)
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
    protocol_client.send_sync(bootloader_client)
    raw = protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(protocol.CMD_WRITE_REG, struct.pack('<IIII', 0x3FF00000, 1, 0xFFFFFFFF, 0)),
    )
    frames = server.slip_decode_frames(raw)
    assert frames
    assert protocol_client.parse_response(frames[0])[1] == protocol.CMD_WRITE_REG


def test_handle_client_flash_end_idle_disconnect(bootloader_client):
    protocol_client.send_sync(bootloader_client)
    protocol_client.send_and_receive(bootloader_client, protocol_client.make_command(protocol.CMD_FLASH_END, struct.pack('<I', 1)))
    # server.handle_client exits after _FLASH_END_IDLE_SEC with no further traffic.
    time.sleep(server._FLASH_END_IDLE_SEC + 0.5)
    bootloader_client.settimeout(1.0)
    try:
        bootloader_client.sendall(protocol_client.make_command(protocol.CMD_SYNC, b'\x55' * 36))
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
    _d, c, _s, _v, data = protocol_client.parse_response(frames[0])
    assert c == protocol.CMD_GET_SECURITY_INFO
    assert data[0] != 0


def test_handle_write_reg_short_payload():
    session = server.ChipSession('esp32')
    raw = server.handle_write_reg(b'\x00' * 8, session)
    frames = server.slip_decode_frames(raw)
    _d, c, _s, _v, data = protocol_client.parse_response(frames[0])
    assert c == protocol.CMD_WRITE_REG
    assert data[0] == 1


def test_make_on_detected_missing_registry_entry():
    callback = server._make_on_detected(39997)
    assert callback is not None
    callback('esp32')  # no registry row — should not raise


def test_handle_read_reg_shared_efuse_returns_zero():
    session = server.ChipSession('auto')
    # C3 family shares efuse window early in connect — respond 0 without guessing.
    addr = chips.PROFILES['esp32c3'].efuse_base + 0x04
    raw = server.handle_read_reg(struct.pack('<I', addr), session)
    frames = server.slip_decode_frames(raw)
    assert protocol_client.parse_response(frames[0])[3] == 0


def test_handle_client_stub_read_flash(bootloader_client):
    protocol_client.send_sync(bootloader_client)
    protocol_client.activate_stub(bootloader_client)
    offset = constants.FLASH_APP_OFFSET
    length = 0x200
    protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', length, 1, length, offset),
        ),
    )
    block = b'\x5A' * length
    payload = struct.pack('<IIII', length, 0, 0, 0) + block
    protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(protocol.CMD_FLASH_DATA, payload),
    )
    data, digest = protocol_client.stub_read_flash(bootloader_client, offset, length)
    assert data == block
    assert digest == hashlib.md5(block).digest()


def test_handle_client_rom_read_flash_slow(bootloader_client):
    protocol_client.send_sync(bootloader_client)
    offset = 0x20000
    length = 0x20
    protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', length, 1, length, offset),
        ),
    )
    block = b'\xAB' * length
    payload = struct.pack('<IIII', length, 0, 0, 0) + block
    protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(protocol.CMD_FLASH_DATA, payload),
    )
    raw = protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(protocol.CMD_READ_FLASH_SLOW, struct.pack('<II', offset, length)),
    )
    _d, c, _s, _v, data = protocol_client.parse_response(server.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_READ_FLASH_SLOW
    assert data[:length] == block


def test_handle_client_flash_data_checksum_error(bootloader_client):
    protocol_client.send_sync(bootloader_client)
    protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\xAB' * 0x100)
    raw = protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(protocol.CMD_FLASH_DATA, payload, checksum=0),
    )
    _d, c, _s, _v, data = protocol_client.parse_response(server.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_FLASH_DATA
    assert data[0] == 1
    assert data[1] == protocol.ROM_CHECKSUM_ERROR


def test_handle_client_mem_end_ohai(bootloader_client):
    protocol_client.send_sync(bootloader_client)
    protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(
            protocol.CMD_MEM_BEGIN,
            struct.pack('<IIII', 0x10, 1, 0x10, 0x40370000),
        ),
    )
    payload = struct.pack('<IIII', 0x10, 0, 0, 0) + (b'\x00' * 0x10)
    protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(protocol.CMD_MEM_DATA, payload),
    )
    raw = protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(protocol.CMD_MEM_END, struct.pack('<II', 0, 0x40370000)),
    )
    assert constants.OHAI_BYTES in raw


def test_handle_client_unknown_command_rom_vs_stub(bootloader_client):
    protocol_client.send_sync(bootloader_client)
    raw = protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(0xFF, b''),
    )
    _d, c, _s, _v, data = protocol_client.parse_response(server.slip_decode_frames(raw)[0])
    assert c == 0xFF
    assert data[1] == protocol.ROM_INVALID_MESSAGE

    protocol_client.activate_stub(bootloader_client)
    raw = protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(0xFE, b''),
    )
    _d, c, _s, _v, data = protocol_client.parse_response(server.slip_decode_frames(raw)[0])
    assert c == 0xFE
    assert data[1] == protocol.STUB_UNIMPLEMENTED


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
    assert protocol_client.parse_response(server.slip_decode_frames(raw)[0])[3] != 0


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


def test_make_on_detected_write_oserror(monkeypatch):
    from esp32_mock_bootloader import daemon

    port = 39998
    daemon.register_instance(port, {'pid': 1, 'port': port, 'detected_chip': None})

    def boom(_port: int, _detected: str | None, base=None) -> None:
        raise OSError('read only')

    monkeypatch.setattr(daemon, 'set_detected_chip', boom)
    callback = server._make_on_detected(port)
    assert callback is not None
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
    protocol_client.send_sync(bootloader_client)
    protocol_client.activate_stub(bootloader_client)
    raw = protocol_client.send_and_receive(
        bootloader_client,
        protocol_client.make_command(protocol.CMD_RUN_USER_CODE, b''),
    )
    assert raw == b'' or server.slip_decode_frames(raw) == []

class _MockSerial:
    """Minimal pyserial stand-in backed by in-memory buffers."""

    def __init__(self) -> None:
        self._rx = bytearray()
        self._tx = bytearray()
        self.timeout = 1.0

    def feed(self, data: bytes) -> None:
        self._rx.extend(data)

    def read(self, size: int) -> bytes:
        if not self._rx:
            return b''
        chunk = bytes(self._rx[:size])
        del self._rx[:size]
        return chunk

    def write(self, data: bytes) -> int:
        self._tx.extend(data)
        return len(data)

    @property
    def written(self) -> bytes:
        return bytes(self._tx)

    def close(self) -> None:
        pass


def test_bootloader_connection_mock_serial_round_trip():
    ser = _MockSerial()
    conn = server.BootloaderConnection(serial_port=ser)
    conn.sendall(b'\xc0hello\xc0')
    assert ser.written == b'\xc0hello\xc0'
    ser.feed(b'\xc0ok\xc0')
    assert conn.recv(64) == b'\xc0ok\xc0'
    conn.close()


def test_bootloader_connection_serial_timeout_on_empty():
    ser = _MockSerial()
    conn = server.BootloaderConnection(serial_port=ser)
    with pytest.raises(socket.timeout):
        conn.recv(8)


@pytest.mark.skipif(os.name == 'nt', reason='Unix PTY fd transport')
def test_handle_client_over_pty_master_fd():
    import pty

    master, slave = pty.openpty()
    pty_transport = server.PtyMasterTransport(master, slave)
    stop_event = threading.Event()

    def serve() -> None:
        server.handle_client(
            server.BootloaderConnection(pty=pty_transport),
            stop_event,
            'esp32',
        )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    try:
        sync = protocol_client.make_command(protocol.CMD_SYNC, b'\x55' * 36)
        os.write(slave, sync)
        buf = bytearray()
        deadline = 5.0
        import time

        start = time.monotonic()
        while time.monotonic() - start < deadline:
            try:
                chunk = os.read(slave, 4096)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            if not chunk:
                break
            buf.extend(chunk)
            frames = server.slip_decode_frames(bytes(buf))
            if frames:
                _d, cmd, _s, _v, _data = protocol_client.parse_response(frames[0])
                assert cmd == protocol.CMD_SYNC
                break
        else:
            pytest.fail('no SYNC response on PTY')
    finally:
        stop_event.set()
        os.close(slave)
        pty_transport.close()
        thread.join(timeout=2)


def test_handle_client_over_mock_serial():
    ser = _MockSerial()
    stop_event = threading.Event()

    def serve() -> None:
        server.handle_client(
            server.BootloaderConnection(serial_port=ser),
            stop_event,
            'esp32',
        )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    try:
        sync = protocol_client.make_command(protocol.CMD_SYNC, b'\x55' * 36)
        ser.feed(sync)
        deadline = 5.0
        import time

        start = time.monotonic()
        while time.monotonic() - start < deadline:
            if ser.written:
                frames = server.slip_decode_frames(ser.written)
                if frames:
                    _d, cmd, _s, _v, _data = protocol_client.parse_response(frames[0])
                    assert cmd == protocol.CMD_SYNC
                    break
            time.sleep(0.05)
        else:
            pytest.fail('no SYNC response on mock serial')
    finally:
        stop_event.set()
        thread.join(timeout=2)


@pytest.mark.skipif(os.name == 'nt', reason='Unix PTY fd transport')
def test_pty_master_transport_releases_slave_on_first_byte():
    import pty

    master, slave = pty.openpty()
    path = os.ttyname(slave)
    transport = server.PtyMasterTransport(master, slave, track_disconnect=True)
    try:
        client = os.open(path, os.O_RDWR)
        try:
            os.write(client, b'\x01')
            assert transport.recv(64) == b'\x01'
            assert not transport.ignore_eof_for_disconnect()
        finally:
            os.close(client)
        assert transport.recv(64) == b''
    finally:
        transport.close()


def test_handle_client_exit_on_disconnect_waits_for_eof():
    """exit_on_disconnect defers session end until the transport signals disconnect."""
    from serial.serialutil import SerialException

    class _DisconnectAfterSyncSerial:
        def __init__(self) -> None:
            self.timeout = 1.0
            self._reads = 0

        def read(self, size: int) -> bytes:
            self._reads += 1
            if self._reads == 1:
                return protocol_client.make_command(protocol.CMD_SYNC, b'\x55' * 36)
            raise SerialException('port closed')

        def write(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None:
            pass

    ser = _DisconnectAfterSyncSerial()
    stop_event = threading.Event()
    disconnected: list[bool] = []

    def serve() -> None:
        disconnected.append(
            server.handle_client(
                server.BootloaderConnection(serial_port=ser),
                stop_event,
                'esp32',
                exit_on_disconnect=True,
            ),
        )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert disconnected == [True]


def test_bootloader_connection_serial_exception():
    from serial.serialutil import SerialException

    class _BrokenSerial:
        timeout = 0.05

        def read(self, size: int) -> bytes:
            raise SerialException('gone')

    conn = server.BootloaderConnection(serial_port=_BrokenSerial())
    assert conn.recv(8) == b''


def test_run_com_server_writes_client_port(tmp_path, monkeypatch):
    import sys

    class _FakeSerial:
        def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.05) -> None:
            self.port = port
            self.timeout = timeout

        def read(self, size: int) -> bytes:
            return b''

        def write(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None:
            pass

    fake_serial_mod = type(sys)('serial')
    fake_serial_mod.Serial = _FakeSerial
    monkeypatch.setitem(sys.modules, 'serial', fake_serial_mod)
    monkeypatch.setattr(server, 'handle_client', lambda *args, **kwargs: False)

    path_file = str(tmp_path / 'client.port')
    server._run_com_server(
        'COM_BIND',
        'COM_CLIENT',
        timeout=0.05,
        port_file=path_file,
        chip_mode='esp32',
        on_detected=None,
        exit_on_disconnect=False,
    )
    assert (tmp_path / 'client.port').read_text(encoding='ascii') == 'COM_CLIENT'


def test_run_pty_server_dispatch(monkeypatch, tmp_path):
    com_calls: list[tuple[str, str]] = []
    win_calls: list[str] = []

    monkeypatch.setattr(
        server,
        'resolve_serial_pair',
        lambda client_port=None, serial_bind=None: ('COM1', 'COM2')
        if client_port == 'COM2'
        else None,
    )
    monkeypatch.setattr(
        server,
        '_run_com_server',
        lambda bind, client, *rest, **kwargs: com_calls.append((bind, client)),
    )
    monkeypatch.setattr(
        server,
        '_run_windows_pty_server',
        lambda *rest, **kwargs: win_calls.append('win'),
    )
    monkeypatch.setattr(server, '_run_unix_pty_server', lambda *rest, **kwargs: None)

    server.run_pty_server(0.05, str(tmp_path / 'a'), 'esp32', client_port='COM2')
    assert com_calls == [('COM1', 'COM2')]

    monkeypatch.setattr(server, 'resolve_serial_pair', lambda *a, **k: None)
    monkeypatch.setattr(os, 'name', 'nt')
    server.run_pty_server(0.05, str(tmp_path / 'b'), 'esp32')
    assert win_calls == ['win']


@pytest.mark.esptool
def test_auto_legacy_detect_reg_deferred_until_known():
    """Legacy detect register must not return magic before chip-specific evidence."""
    from esp32_mock_bootloader.advanced import protocol as protocol_api

    with mock_bootloader(chip='auto', timeout=None, exit_on_disconnect=False, mode='foreground') as mock:
        client = protocol_api.connect(mock)
        client.sync()
        raw = client.send_and_receive(
            protocol_client.make_command(0x0A, struct.pack('<I', chips.LEGACY_DETECT_REG)),
        )
        frames = client.decode_frames(raw)
        assert len(frames) >= 1
        _d, _c, _s, value, _data = client.parse_response(frames[0])
        assert value == 0


@pytest.mark.esptool
@pytest.mark.parametrize('chip', chips.chips_with_unique_efuse())
def test_auto_read_reg_detects_via_unique_efuse(chip: str):
    from esp32_mock_bootloader.advanced import protocol as protocol_api

    profile = chips.PROFILES[chip]
    with mock_bootloader(chip='auto', timeout=None, exit_on_disconnect=False, mode='foreground') as mock:
        client = protocol_api.connect(mock)
        client.sync()
        efuse_addr = profile.efuse_base + 0x04
        raw = client.send_and_receive(
            protocol_client.make_command(0x0A, struct.pack('<I', efuse_addr)),
        )
        frames = client.decode_frames(raw)
        assert len(frames) >= 1
        _d, _c, _s, value, _data = client.parse_response(frames[0])
        assert value == 0

        if profile.detect_magic:
            raw = client.send_and_receive(
                protocol_client.make_command(0x0A, struct.pack('<I', profile.detect_reg)),
            )
            frames = client.decode_frames(raw)
            _d, _c, _s, value, _data = client.parse_response(frames[0])
            assert value == profile.detect_magic


@pytest.mark.esptool
def test_get_security_info_after_auto_detection():
    from esp32_mock_bootloader.advanced import protocol as protocol_api

    chip = next(
        c for c, p in chips.PROFILES.items()
        if p.detect_magic and p.detect_reg != chips.LEGACY_DETECT_REG
    )
    profile = chips.PROFILES[chip]
    with mock_bootloader(chip='auto', timeout=None, exit_on_disconnect=False, mode='foreground') as mock:
        client = protocol_api.connect(mock)
        client.sync()
        client.send_and_receive(
            protocol_client.make_command(0x0A, struct.pack('<I', profile.detect_reg)),
        )
        raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_GET_SECURITY_INFO))
        frames = client.decode_frames(raw)
        _d, c, _s, _v, data = client.parse_response(frames[0])
        assert c == protocol.CMD_GET_SECURITY_INFO
        assert data[0] == 0


@pytest.mark.esptool
def test_get_security_info_unknown_auto_returns_error():
    from esp32_mock_bootloader.advanced import protocol as protocol_api

    with mock_bootloader(chip='auto', timeout=None, exit_on_disconnect=False, mode='foreground') as mock:
        client = protocol_api.connect(mock)
        client.sync()
        raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_GET_SECURITY_INFO))
        frames = client.decode_frames(raw)
        assert len(frames) >= 1
        _d, c, _s, _v, data = client.parse_response(frames[0])
        assert c == protocol.CMD_GET_SECURITY_INFO
        assert data and data[0] != 0  # error status before detection


@pytest.mark.esptool
@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_auto_mode_esptool_per_soc(chip: str):
    with mock_bootloader(chip='auto', timeout=30.0, exit_on_disconnect=True, mode='foreground') as mock:
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
            wrote_ok, detail = esptool.write_flash_no_stub(
                chip, str(bin_file), port=mock.port(),
            )
            assert wrote_ok, detail


@pytest.mark.esptool
@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_explicit_chip_detect_register(chip: str):
    from esp32_mock_bootloader.advanced import protocol as protocol_api

    profile = chips.PROFILES[chip]
    with mock_bootloader(chip=chip, timeout=None, exit_on_disconnect=False, mode='foreground') as mock:
        client = protocol_api.connect(mock)
        client.sync()
        if profile.detect_magic:
            value = client.read_reg(profile.detect_reg)
            assert value == profile.detect_magic


@pytest.mark.esptool
def test_daemon_auto_start_detected_chip():
    port = process.reserve_tcp_port()
    try:
        data = daemon.start_daemon(port=port, chip_mode='auto')
        assert data['chip'] == 'auto'
        status = daemon.daemon_status(port)
        assert status['running']
        assert status['url'] == f'socket://127.0.0.1:{port}'

        with tempfile.TemporaryDirectory() as tmp:
            bin_file = esptool.create_fake_binary(Path(tmp) / 'test.bin', 512)
            wrote_ok, detail = esptool.write_flash_no_stub(
                'esp32', str(bin_file), port=port,
            )
            assert wrote_ok, detail

        status = daemon.daemon_status(port)
        assert status['detected_chip'] == 'esp32'
        state = daemon.read_state(port)
        assert state is not None
        assert state.get('detected_chip') == 'esp32'
    finally:
        daemon.stop_daemon(port)
