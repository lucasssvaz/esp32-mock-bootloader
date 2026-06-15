# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Protocol edge-case tests (socket-level, no esptool required).

Shared constants (offsets, SYNC payload, OHAI, stub entry) are documented in
``esp32_mock_bootloader.testing.constants``.
Pattern fill bytes (0x55, 0x78 0x9c, etc.) are arbitrary payloads chosen to exercise
checksum or deflate paths — they are not protocol-mandated values.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from esp32_mock_bootloader import protocol
from esp32_mock_bootloader import chips

import esp32_mock_bootloader.testing as mock


def test_get_security_info_auto_before_detection():
    proc, port = mock.server.start_server(chip='auto')
    try:
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_GET_SECURITY_INFO))
        _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
        assert c == protocol.CMD_GET_SECURITY_INFO
        # Auto mode before chip detection: flags=1, error=ROM_INVALID_MESSAGE (0x05).
        assert data[0] == 1
        assert data[1] == protocol.ROM_INVALID_MESSAGE
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_read_reg_legacy_deferred_until_chip_evidence():
    proc, port = mock.server.start_server(chip='auto')
    try:
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        raw = mock.protocol.send_and_receive(
            sock, mock.protocol.make_command(protocol.CMD_READ_REG, struct.pack('<I', chips.LEGACY_DETECT_REG)),
        )
        assert mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])[3] == 0
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_get_security_info_explicit_chip():
    proc, port = mock.server.start_server(chip='esp32')
    try:
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_GET_SECURITY_INFO))
        _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
        assert c == protocol.CMD_GET_SECURITY_INFO
        assert data[0] == 0
        chip_id = struct.unpack_from('<I', data, 12)[0]
        assert chip_id == chips.PROFILES['esp32'].image_chip_id
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_stub_unknown_command_returns_unimplemented(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.activate_stub(sock)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(mock.constants.UNKNOWN_COMMAND, b''))
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == mock.constants.UNKNOWN_COMMAND
    assert data[0] == 1
    assert data[1] == protocol.STUB_UNIMPLEMENTED
    sock.close()


def test_mem_data_checksum_error_stub_mode(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.activate_stub(sock)
    # Small 16-byte MEM_DATA block; 0x55 fill is arbitrary (checksum must still match).
    payload = struct.pack('<IIII', 0x10, 0, 0, 0) + (b'\x55' * 0x10)
    raw = mock.protocol.send_and_receive(
        sock, mock.protocol.make_command(protocol.CMD_MEM_DATA, payload, checksum=0),
    )
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_MEM_DATA
    assert data[0] == 1
    assert data[1] == protocol.STUB_CHECKSUM_ERROR
    sock.close()


def test_flash_defl_data_checksum_error(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_DEFL_BEGIN,
            struct.pack('<IIII', 0x40, 1, 0x40, mock.constants.FLASH_APP_OFFSET),
        ),
    )
    # 0x78 0x9c is the zlib header magic; content is invalid on purpose (checksum=0).
    payload = struct.pack('<IIII', 0x10, 0, 0, 0) + (b'\x78\x9c' + b'\x00' * 12)
    raw = mock.protocol.send_and_receive(
        sock, mock.protocol.make_command(protocol.CMD_FLASH_DEFL_DATA, payload, checksum=0),
    )
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_FLASH_DEFL_DATA
    assert data[0] == 1
    assert data[1] == protocol.ROM_CHECKSUM_ERROR
    sock.close()


def test_write_reg_partial_mask(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    # Arbitrary MMIO address in ESP32 peripheral range (not tied to a real register).
    addr = 0x3FF0ABCD
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(protocol.CMD_WRITE_REG, struct.pack('<IIII', addr, 0xFFFFFFFF, 0xFFFFFFFF, 0)),
    )
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(protocol.CMD_WRITE_REG, struct.pack('<IIII', addr, 0x12340000, 0xFFFF0000, 0)),
    )
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_READ_REG, struct.pack('<I', addr)))
    _d, _c, _s, value, _data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert value == 0x1234FFFF
    sock.close()


def test_mem_end_stay_in_loader_no_ohai(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_MEM_BEGIN,
            struct.pack('<IIII', 0x10, 1, 0x10, mock.constants.STUB_IRAM_ENTRY),
        ),
    )
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_MEM_DATA,
            struct.pack('<IIII', 0x10, 0, 0, 0) + (b'\x00' * 0x10),
        ),
    )
    raw = mock.protocol.send_and_receive(
        sock, mock.protocol.make_command(protocol.CMD_MEM_END, struct.pack('<II', mock.constants.STAY_IN_LOADER, 0)),
    )
    # OHAI is only sent when MEM_END requests run-at-entrypoint (stay_in_loader=0).
    assert mock.constants.OHAI_BYTES not in raw
    sock.close()


def test_rom_stub_only_commands_rejected_before_stub(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    for cmd in (
        protocol.CMD_ERASE_FLASH,
        protocol.CMD_ERASE_REGION,
        protocol.CMD_READ_FLASH,
    ):
        data = (
            struct.pack('<II', mock.constants.FLASH_APP_OFFSET, protocol.FLASH_SECTOR_SIZE)
            if cmd == protocol.CMD_ERASE_REGION
            else b''
        )
        if cmd == protocol.CMD_READ_FLASH:
            # READ_FLASH: offset, length, packet_size, max_in_flight (4 is a small test value).
            data = struct.pack(
                '<IIII', mock.constants.FLASH_APP_OFFSET, 0x100, protocol.FLASH_SECTOR_SIZE, 4,
            )
        raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(cmd, data))
        _d, c, _s, _v, resp = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
        assert c == cmd
        assert resp[0] == 1
        assert resp[1] == protocol.ROM_INVALID_MESSAGE
    sock.close()


def test_read_reg_empty_payload(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_READ_REG, b''))
    _d, _c, _s, value, _data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert value == 0
    sock.close()


def test_write_reg_short_payload(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    raw = mock.protocol.send_and_receive(
        sock, mock.protocol.make_command(protocol.CMD_WRITE_REG, struct.pack('<II', 0, 0)),
    )
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_WRITE_REG
    assert data[0] == 1
    sock.close()


def test_sync_stub_value_zero_after_activate(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.activate_stub(sock)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SYNC, mock.constants.SYNC_PAYLOAD), 8192)
    frames = mock.protocol.slip_decode_frames(raw)
    _d, c, _s, value, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_SYNC
    # Stub SYNC response value field is 0 (ROM returns protocol.ROM_SYNC_VALUE).
    assert value == 0
    assert data[0] == 0
    sock.close()



def test_flash_defl_valid_compressed_block(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    plain = b'\x00' * 0x80
    compressed = zlib.compress(plain)
    # 0x30000 is an arbitrary flash offset away from mock.constants.FLASH_APP_OFFSET test traffic.
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_DEFL_BEGIN,
            struct.pack('<IIII', 0x80, 1, len(compressed), 0x30000),
        ),
    )
    payload = struct.pack('<IIII', len(compressed), 0, 0, 0) + compressed
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DEFL_DATA, payload))
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_FLASH_DEFL_DATA
    assert data[0] == 0
    sock.close()


def test_stub_read_flash_multiple_packets(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.activate_stub(sock)
    # Use a non-default offset so READ_FLASH is not confused with prior mock.constants.FLASH_APP_OFFSET tests.
    offset = 0x20000
    length = protocol.FLASH_SECTOR_SIZE * 2
    block = bytes((i & 0xFF for i in range(length)))  # gradient pattern for MD5 uniqueness
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(protocol.CMD_FLASH_BEGIN, struct.pack('<IIII', length, 1, length, offset)),
    )
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_DATA,
            struct.pack('<IIII', length, 0, 0, 0) + block,
        ),
    )
    data, digest = mock.protocol.stub_read_flash(
        sock, offset, length, packet_size=protocol.FLASH_SECTOR_SIZE, max_in_flight=2,
    )
    assert data == block
    assert digest == __import__('hashlib').md5(block).digest()
    sock.close()


def test_flash_data_sequence_number_preserved(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x200, 2, 0x100, mock.constants.FLASH_APP_OFFSET),
        ),
    )
    for seq in range(2):
        # Sequence numbers 0 and 1; fill 0x01 / 0x02 makes MD5 predictable.
        chunk = bytes([seq + 1]) * 0x100
        payload = struct.pack('<IIII', 0x100, seq, 0, 0) + chunk
        mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload))
    md5_req = struct.pack('<IIII', mock.constants.FLASH_APP_OFFSET, 0x200, 0, 0)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    import hashlib
    expected = hashlib.md5(b'\x01' * 0x100 + b'\x02' * 0x100).hexdigest().encode('ascii')
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_SPI_FLASH_MD5
    assert data[:32] == expected
    sock.close()
