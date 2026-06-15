# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Protocol integration tests (socket-level, no esptool required).

See ``esp32_mock_bootloader.testing.constants`` for documented shared constants
(SYNC_PAYLOAD, FLASH_APP_OFFSET, STUB_IRAM_ENTRY, OHAI_BYTES, etc.). Block sizes
like 0x100 / 0x1000 are small test payloads unless noted; they follow esptool FLASH_BEGIN
by the serial protocol spec.
"""

from __future__ import annotations

import hashlib
import socket
import struct
import time

import pytest

from esp32_mock_bootloader import protocol
from esp32_mock_bootloader import chips

import esp32_mock_bootloader.testing as mock


def test_server_accepts_connection(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    assert sock is not None
    sock.close()


def test_sync_response(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SYNC, mock.constants.SYNC_PAYLOAD), 8192)
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) == mock.constants.SYNC_RESPONSE_COUNT
    d, c, _s, v, data = mock.protocol.parse_response(frames[0])
    assert d == 0x01  # response direction byte
    assert c == protocol.CMD_SYNC
    assert v != 0  # ROM SYNC value field (stub returns 0)
    assert data[0] == 0
    sock.close()


def test_read_reg_chip_detect(mock_server, reference_chip):
    port, _proc = mock_server
    profile = chips.PROFILES[reference_chip]
    if not profile.detect_magic:
        pytest.skip(f'{reference_chip} has no detect magic register')
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    raw = mock.protocol.send_and_receive(
        sock, mock.protocol.make_command(protocol.CMD_READ_REG, struct.pack('<I', profile.detect_reg)),
    )
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, _c, _s, value, _data = mock.protocol.parse_response(frames[0])
    assert value == profile.detect_magic
    sock.close()


def test_read_reg_efuse(mock_server, reference_chip):
    port, _proc = mock_server
    profile = chips.PROFILES[reference_chip]
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    raw = mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_READ_REG, struct.pack('<I', profile.efuse_base + 0x04),
        ),
    )
    # Unprogrammed eFuse words read as 0.
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, _c, _s, value, _data = mock.protocol.parse_response(frames[0])
    assert value == 0
    sock.close()


def test_flash_begin(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    raw = mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x1000, 1, 0x1000, mock.constants.FLASH_APP_OFFSET),
        ),
    )
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_FLASH_BEGIN
    assert data[0] == 0
    sock.close()


def test_flash_data(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, mock.constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\xAB' * 0x100)  # arbitrary fill
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload))
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_FLASH_DATA
    assert data[0] == 0
    sock.close()


def test_flash_end_second_session(reference_chip):
    proc, port = mock.server.start_server(timeout=30.0, chip=reference_chip)
    try:
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        raw = mock.protocol.send_and_receive(
            sock, mock.protocol.make_command(protocol.CMD_FLASH_END, struct.pack('<I', mock.constants.STAY_IN_LOADER)),
        )
        frames = mock.protocol.slip_decode_frames(raw)
        assert len(frames) >= 1
        _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
        assert c == protocol.CMD_FLASH_END
        assert data[0] == 0
        sock.close()

        time.sleep(0.2)
        assert proc.poll() is None
        sock2 = mock.server.connect(port)
        assert mock.protocol.minimal_plain_flash(sock2)
        sock2.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_flash_defl(mock_server):
    port, proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    raw = mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_DEFL_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, mock.constants.FLASH_APP_OFFSET),
        ),
    )
    assert len(mock.protocol.slip_decode_frames(raw)) >= 1
    # Uncompressed placeholder block (not valid zlib; exercises DEFL_DATA path only).
    payload = struct.pack('<IIII', 0x20, 0, 0, 0) + (b'\x00' * 0x20)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DEFL_DATA, payload))
    assert len(mock.protocol.slip_decode_frames(raw)) >= 1
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DEFL_END, struct.pack('<I', mock.constants.STAY_IN_LOADER)))
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_FLASH_DEFL_END
    assert proc.poll() is None
    sock.close()


def test_mem_sequence_ohai(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_MEM_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, mock.constants.STUB_IRAM_ENTRY),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\x00' * 0x100)
    mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_MEM_DATA, payload))
    raw = mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(protocol.CMD_MEM_END, struct.pack('<II', mock.constants.RUN_ENTRYPOINT, mock.constants.STUB_IRAM_ENTRY)),
    )
    assert mock.constants.OHAI_BYTES in raw
    sock.close()


def test_write_reg(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    # Generic peripheral address in ESP32 address space (mock stores any WRITE_REG target).
    addr = 0x3FF00000
    value = 0x12345678
    raw = mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(protocol.CMD_WRITE_REG, struct.pack('<IIII', addr, value, 0xFFFFFFFF, 0)),
    )
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_WRITE_REG
    assert data[0] == 0

    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_READ_REG, struct.pack('<I', addr)))
    _d, c, _s, reg_value, _data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_READ_REG
    assert reg_value == value
    sock.close()


def test_change_baudrate(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    raw = mock.protocol.send_and_receive(
        sock, mock.protocol.make_command(protocol.CMD_CHANGE_BAUDRATE, struct.pack('<II', 921600, 0)),
    )
    # Baud value matches a common esptool default; second word is reserved (0).
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_CHANGE_BAUDRATE
    sock.close()


def test_spi_attach(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SPI_ATTACH, struct.pack('<I', 0)))
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_SPI_ATTACH
    sock.close()


def test_spi_set_params(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    # SPI_SET_PARAMS layout from esptool: id, total_size, block_size, sector_size, page_size, status_mask
    params = struct.pack('<IIIIII', 0, 0x400000, mock.constants.FLASH_APP_OFFSET, 0x1000, 0x100, 0xFFFF)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SPI_SET_PARAMS, params))
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_SPI_SET_PARAMS
    sock.close()


def test_spi_flash_md5(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    md5_req = struct.pack('<IIII', mock.constants.FLASH_APP_OFFSET, 0x1000, 0, 0)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_SPI_FLASH_MD5
    # Fresh mock flash is erased (0xFF); ROM MD5 returns 32-byte ASCII hex + 2-byte status.
    expected = hashlib.md5(b'\xff' * 0x1000).hexdigest().encode('ascii')
    assert data[:32] == expected
    assert data[32:34] == b'\x00\x00'
    sock.close()


def test_spi_flash_md5_after_stub_upload(mock_server):
    """ROM hex MD5 before MEM_END; binary MD5 after stub entrypoint + OHAI."""
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    md5_req = struct.pack('<IIII', mock.constants.FLASH_APP_OFFSET, 0x1000, 0, 0)
    digest = hashlib.md5(b'\xff' * 0x1000).digest()

    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_SPI_FLASH_MD5
    assert data[:32] == digest.hex().encode('ascii')
    assert data[32:34] == b'\x00\x00'

    raw = mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(protocol.CMD_MEM_END, struct.pack('<II', mock.constants.RUN_ENTRYPOINT, mock.constants.STUB_IRAM_ENTRY)),
    )
    assert mock.constants.OHAI_BYTES in raw

    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_SPI_FLASH_MD5
    assert data[:16] == digest
    assert data[16:18] == b'\x00\x00'
    sock.close()


def test_unknown_command(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(mock.constants.UNKNOWN_COMMAND, b'\x00' * 4))
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = mock.protocol.parse_response(frames[0])
    assert c == mock.constants.UNKNOWN_COMMAND
    assert data[0] == 1
    assert data[1] == protocol.ROM_INVALID_MESSAGE  # 0x05 in ROM mode
    sock.close()


def _address_requiring_slip_escape(base: int) -> int | None:
    for offset in range(0, 0x1000):
        addr = base + offset
        if 0xC0 in struct.pack('<I', addr):
            return addr
    return None


def test_slip_escape_decoding(mock_server, reference_chip):
    port, _proc = mock_server
    profile = chips.PROFILES[reference_chip]
    addr = _address_requiring_slip_escape(profile.efuse_base)
    if addr is None:
        pytest.skip(f'no SLIP-escape address in efuse window for {reference_chip}')
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    cmd_packet = mock.protocol.make_command(protocol.CMD_READ_REG, struct.pack('<I', addr))
    assert protocol.SLIP_ESC in cmd_packet[1:-1]
    raw = mock.protocol.send_and_receive(sock, cmd_packet)
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = mock.protocol.parse_response(frames[0])
    assert c == protocol.CMD_READ_REG
    sock.close()


def test_server_timeout(reference_chip):
    proc, port = mock.server.start_server(timeout=3.0, chip=reference_chip)
    start = time.time()
    try:
        proc.wait(timeout=10)
        elapsed = time.time() - start
        assert proc.returncode is not None
        assert 2.5 <= elapsed <= 7.0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_multiple_frames_one_chunk(mock_server):
    port, proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    fb = mock.protocol.make_command(
        protocol.CMD_FLASH_BEGIN,
        struct.pack('<IIII', 0x10, 1, 0x10, mock.constants.FLASH_APP_OFFSET),
    )
    fd = mock.protocol.make_command(protocol.CMD_FLASH_DATA, struct.pack('<IIII', 0x10, 0, 0, 0) + b'\xFF' * 0x10)
    fe = mock.protocol.make_command(protocol.CMD_FLASH_END, struct.pack('<I', mock.constants.STAY_IN_LOADER))
    sock.sendall(fb + fd + fe)
    time.sleep(0.5)
    raw = b''
    sock.settimeout(2.0)
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
    except socket.timeout:
        pass
    frames = mock.protocol.slip_decode_frames(raw)
    assert len(frames) == 3
    cmds = [mock.protocol.parse_response(f)[1] for f in frames]
    assert cmds == [protocol.CMD_FLASH_BEGIN, protocol.CMD_FLASH_DATA, protocol.CMD_FLASH_END]
    assert proc.poll() is None
    sock.close()


@pytest.mark.parametrize('chip', mock.constants.ESPTOOL_CHIPS)
def test_explicit_chip_protocol_smoke(chip: str):
    """Minimal SYNC + plain flash for every esptool target."""
    proc, port = mock.server.start_server(chip=chip)
    try:
        sock = mock.server.connect(port)
        assert mock.protocol.minimal_plain_flash(sock)
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_flash_data_checksum_error(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, mock.constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\xAB' * 0x100)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload, checksum=0))
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_FLASH_DATA
    assert data[0] == 1
    assert data[1] == protocol.ROM_CHECKSUM_ERROR  # 0x07
    sock.close()


def test_stub_erase_flash(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.activate_stub(sock)
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, mock.constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\x00' * 0x100)
    mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload))
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_ERASE_FLASH))
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_ERASE_FLASH
    assert data[0] == 0
    md5_req = struct.pack('<IIII', mock.constants.FLASH_APP_OFFSET, 0x100, 0, 0)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert data[:16] == hashlib.md5(b'\xff' * 0x100).digest()
    sock.close()


def test_stub_erase_region(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.activate_stub(sock)
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x200, 1, 0x200, mock.constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x200, 0, 0, 0) + (b'\x11' * 0x200)
    mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload))
    # Erase only the first 0x100 bytes of the 0x200-byte block.
    raw = mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(protocol.CMD_ERASE_REGION, struct.pack('<II', mock.constants.FLASH_APP_OFFSET, 0x100)),
    )
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_ERASE_REGION
    assert data[0] == 0
    md5_req = struct.pack('<IIII', mock.constants.FLASH_APP_OFFSET, 0x200, 0, 0)
    raw = mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    digest = hashlib.md5(b'\xff' * 0x100 + b'\x11' * 0x100).digest()
    _d, c, _s, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert data[:16] == digest
    sock.close()


def test_stub_read_flash(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.activate_stub(sock)
    offset = mock.constants.FLASH_APP_OFFSET
    length = 0x400  # one sector; arbitrary size for READ_FLASH smoke test
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(protocol.CMD_FLASH_BEGIN, struct.pack('<IIII', length, 1, length, offset)),
    )
    block = b'\x5A' * length  # arbitrary fill; must round-trip via READ_FLASH + MD5
    payload = struct.pack('<IIII', length, 0, 0, 0) + block
    mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload))

    data, digest = mock.protocol.stub_read_flash(sock, offset, length)
    assert data == block
    assert digest == hashlib.md5(block).digest()
    sock.close()


def test_rom_read_flash_slow(mock_server):
    port, _proc = mock_server
    sock = mock.server.connect(port)
    mock.protocol.send_sync(sock)
    # READ_FLASH_SLOW uses offset 0x20000 to avoid overlap with mock.constants.FLASH_APP_OFFSET tests.
    slow_offset = 0x20000
    mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x40, 1, 0x40, slow_offset),
        ),
    )
    payload = struct.pack('<IIII', 0x40, 0, 0, 0) + (b'\x42' * 0x40)
    mock.protocol.send_and_receive(sock, mock.protocol.make_command(protocol.CMD_FLASH_DATA, payload))
    raw = mock.protocol.send_and_receive(
        sock,
        mock.protocol.make_command(protocol.CMD_READ_FLASH_SLOW, struct.pack('<II', slow_offset, 0x20)),
    )
    _d, c, size, _v, data = mock.protocol.parse_response(mock.protocol.slip_decode_frames(raw)[0])
    assert c == protocol.CMD_READ_FLASH_SLOW
    # ROM returns a fixed 64-byte buffer; size field includes 4-byte prefix per esptool.
    assert size == 64 + 4
    assert data[:0x20] == b'\x42' * 0x20
    assert data[0x20:0x40] == b'\xff' * 0x20  # remainder of 64-byte block is erased
    sock.close()
