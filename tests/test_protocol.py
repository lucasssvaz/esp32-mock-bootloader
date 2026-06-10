# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Protocol unit tests (no esptool required)."""

from __future__ import annotations

import hashlib
import socket
import struct
import time

import pytest

from esp32_mock_bootloader.chip_profiles import CHIP_PROFILES

from conftest import (
    CMD_CHANGE_BAUDRATE,
    CMD_FLASH_BEGIN,
    CMD_FLASH_DATA,
    CMD_FLASH_DEFL_BEGIN,
    CMD_FLASH_DEFL_END,
    CMD_FLASH_END,
    CMD_MEM_END,
    CMD_READ_REG,
    CMD_SPI_ATTACH,
    CMD_SPI_FLASH_MD5,
    CMD_SPI_SET_PARAMS,
    CMD_SYNC,
    CMD_WRITE_REG,
    ESPTOOL_CHIPS,
    SLIP_END,
    SLIP_ESC,
    connect_to_server,
    make_command,
    minimal_plain_flash,
    parse_response,
    send_and_receive,
    send_sync,
    slip_decode_frames,
    start_mock_server,
)


@pytest.fixture
def mock_server(reference_chip):
    """Yield (port, proc) for a mock server on a unique port."""
    proc, port = start_mock_server(chip=reference_chip)
    try:
        yield port, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_server_accepts_connection(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    assert sock is not None
    sock.close()


def test_sync_response(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    sync_data = bytes([0x07, 0x07, 0x12, 0x20]) + (b'\x55' * 32)
    raw = send_and_receive(sock, make_command(CMD_SYNC, sync_data), 8192)
    frames = slip_decode_frames(raw)
    assert len(frames) == 8
    d, c, _s, v, data = parse_response(frames[0])
    assert d == 0x01
    assert c == CMD_SYNC
    assert v != 0
    assert data[0] == 0
    sock.close()


def test_read_reg_chip_detect(mock_server, reference_chip):
    port, _proc = mock_server
    profile = CHIP_PROFILES[reference_chip]
    if not profile.detect_magic:
        pytest.skip(f'{reference_chip} has no detect magic register')
    sock = connect_to_server(port)
    send_sync(sock)
    raw = send_and_receive(
        sock, make_command(CMD_READ_REG, struct.pack('<I', profile.detect_reg)),
    )
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, _c, _s, value, _data = parse_response(frames[0])
    assert value == profile.detect_magic
    sock.close()


def test_read_reg_efuse(mock_server, reference_chip):
    port, _proc = mock_server
    profile = CHIP_PROFILES[reference_chip]
    sock = connect_to_server(port)
    send_sync(sock)
    raw = send_and_receive(
        sock,
        make_command(CMD_READ_REG, struct.pack('<I', profile.efuse_base + 0x04)),
    )
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, _c, _s, value, _data = parse_response(frames[0])
    assert value == 0
    sock.close()


def test_flash_begin(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    raw = send_and_receive(
        sock,
        make_command(CMD_FLASH_BEGIN, struct.pack('<IIII', 0x1000, 1, 0x1000, 0x10000)),
    )
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = parse_response(frames[0])
    assert c == CMD_FLASH_BEGIN
    assert data[0] == 0
    sock.close()


def test_flash_data(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    send_and_receive(
        sock,
        make_command(CMD_FLASH_BEGIN, struct.pack('<IIII', 0x100, 1, 0x100, 0x10000)),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\xAB' * 0x100)
    raw = send_and_receive(sock, make_command(CMD_FLASH_DATA, payload))
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = parse_response(frames[0])
    assert c == CMD_FLASH_DATA
    assert data[0] == 0
    sock.close()


def test_flash_end_second_session(reference_chip):
    proc, port = start_mock_server(timeout=30.0, chip=reference_chip)
    try:
        sock = connect_to_server(port)
        send_sync(sock)
        raw = send_and_receive(
            sock, make_command(CMD_FLASH_END, struct.pack('<I', 1)),
        )
        frames = slip_decode_frames(raw)
        assert len(frames) >= 1
        _d, c, _s, _v, data = parse_response(frames[0])
        assert c == CMD_FLASH_END
        assert data[0] == 0
        sock.close()

        time.sleep(0.2)
        assert proc.poll() is None
        sock2 = connect_to_server(port)
        assert minimal_plain_flash(sock2)
        sock2.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_flash_defl(mock_server):
    port, proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    raw = send_and_receive(
        sock,
        make_command(CMD_FLASH_DEFL_BEGIN, struct.pack('<IIII', 0x100, 1, 0x100, 0x10000)),
    )
    assert len(slip_decode_frames(raw)) >= 1
    payload = struct.pack('<IIII', 0x20, 0, 0, 0) + (b'\x00' * 0x20)
    raw = send_and_receive(sock, make_command(0x11, payload))
    assert len(slip_decode_frames(raw)) >= 1
    raw = send_and_receive(sock, make_command(CMD_FLASH_DEFL_END, struct.pack('<I', 1)))
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = parse_response(frames[0])
    assert c == CMD_FLASH_DEFL_END
    assert proc.poll() is None
    sock.close()


def test_mem_sequence_ohai(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    send_and_receive(
        sock,
        make_command(0x05, struct.pack('<IIII', 0x100, 1, 0x100, 0x40080000)),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\x00' * 0x100)
    send_and_receive(sock, make_command(0x07, payload))
    raw = send_and_receive(
        sock, make_command(CMD_MEM_END, struct.pack('<II', 0, 0x40080000)),
    )
    assert bytes([0x4F, 0x48, 0x41, 0x49]) in raw
    sock.close()


def test_write_reg(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    raw = send_and_receive(
        sock, make_command(CMD_WRITE_REG, struct.pack('<II', 0x3FF00000, 1)),
    )
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = parse_response(frames[0])
    assert c == CMD_WRITE_REG
    assert data[0] == 0
    sock.close()


def test_change_baudrate(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    raw = send_and_receive(
        sock, make_command(CMD_CHANGE_BAUDRATE, struct.pack('<II', 921600, 0)),
    )
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = parse_response(frames[0])
    assert c == CMD_CHANGE_BAUDRATE
    sock.close()


def test_spi_attach(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    raw = send_and_receive(sock, make_command(CMD_SPI_ATTACH, struct.pack('<I', 0)))
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = parse_response(frames[0])
    assert c == CMD_SPI_ATTACH
    sock.close()


def test_spi_set_params(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    params = struct.pack('<IIIIII', 0, 0x400000, 0x10000, 0x1000, 0x100, 0xFFFF)
    raw = send_and_receive(sock, make_command(CMD_SPI_SET_PARAMS, params))
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = parse_response(frames[0])
    assert c == CMD_SPI_SET_PARAMS
    sock.close()


def test_spi_flash_md5(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    md5_req = struct.pack('<IIII', 0x10000, 0x1000, 0, 0)
    raw = send_and_receive(sock, make_command(CMD_SPI_FLASH_MD5, md5_req))
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = parse_response(frames[0])
    assert c == CMD_SPI_FLASH_MD5
    expected = hashlib.md5(b'\xff' * 0x1000).hexdigest().encode('ascii')
    assert data[:32] == expected
    assert data[32:34] == b'\x00\x00'
    sock.close()


def test_spi_flash_md5_after_stub_upload(mock_server):
    """ROM hex MD5 before MEM_END; binary MD5 after stub entrypoint + OHAI."""
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    md5_req = struct.pack('<IIII', 0x10000, 0x1000, 0, 0)
    digest = hashlib.md5(b'\xff' * 0x1000).digest()

    raw = send_and_receive(sock, make_command(CMD_SPI_FLASH_MD5, md5_req))
    _d, c, _s, _v, data = parse_response(slip_decode_frames(raw)[0])
    assert c == CMD_SPI_FLASH_MD5
    assert data[:32] == digest.hex().encode('ascii')
    assert data[32:34] == b'\x00\x00'

    raw = send_and_receive(
        sock, make_command(CMD_MEM_END, struct.pack('<II', 0, 0x40080000)),
    )
    assert bytes([0x4F, 0x48, 0x41, 0x49]) in raw

    raw = send_and_receive(sock, make_command(CMD_SPI_FLASH_MD5, md5_req))
    _d, c, _s, _v, data = parse_response(slip_decode_frames(raw)[0])
    assert c == CMD_SPI_FLASH_MD5
    assert data[:16] == digest
    assert data[16:18] == b'\x00\x00'
    sock.close()


def test_unknown_command(mock_server):
    port, _proc = mock_server
    sock = connect_to_server(port)
    send_sync(sock)
    raw = send_and_receive(sock, make_command(0xFE, b'\x00' * 4))
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = parse_response(frames[0])
    assert c == 0xFE
    assert data[0] == 0
    sock.close()


def _address_requiring_slip_escape(base: int) -> int | None:
    for offset in range(0, 0x1000):
        addr = base + offset
        if 0xC0 in struct.pack('<I', addr):
            return addr
    return None


def test_slip_escape_decoding(mock_server, reference_chip):
    port, _proc = mock_server
    profile = CHIP_PROFILES[reference_chip]
    addr = _address_requiring_slip_escape(profile.efuse_base)
    if addr is None:
        pytest.skip(f'no SLIP-escape address in efuse window for {reference_chip}')
    sock = connect_to_server(port)
    send_sync(sock)
    cmd_packet = make_command(CMD_READ_REG, struct.pack('<I', addr))
    assert SLIP_ESC in cmd_packet[1:-1]
    raw = send_and_receive(sock, cmd_packet)
    frames = slip_decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = parse_response(frames[0])
    assert c == CMD_READ_REG
    sock.close()


def test_server_timeout(reference_chip):
    proc, port = start_mock_server(timeout=3.0, chip=reference_chip)
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
    sock = connect_to_server(port)
    send_sync(sock)
    fb = make_command(CMD_FLASH_BEGIN, struct.pack('<IIII', 0x10, 1, 0x10, 0x10000))
    fd = make_command(CMD_FLASH_DATA, struct.pack('<IIII', 0x10, 0, 0, 0) + b'\xFF' * 0x10)
    fe = make_command(CMD_FLASH_END, struct.pack('<I', 1))
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
    frames = slip_decode_frames(raw)
    assert len(frames) == 3
    cmds = [parse_response(f)[1] for f in frames]
    assert cmds == [CMD_FLASH_BEGIN, CMD_FLASH_DATA, CMD_FLASH_END]
    assert proc.poll() is None
    sock.close()


@pytest.mark.parametrize('chip', ESPTOOL_CHIPS)
def test_explicit_chip_protocol_smoke(chip: str):
    """Minimal SYNC + plain flash for every esptool target."""
    proc, port = start_mock_server(chip=chip)
    try:
        sock = connect_to_server(port)
        assert minimal_plain_flash(sock)
        sock.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
