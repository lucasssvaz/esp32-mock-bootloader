# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Protocol integration, unit, and edge-case tests."""

from __future__ import annotations

import hashlib
import socket
import struct
import tempfile
import time
import zlib
from pathlib import Path

import pytest

from esptool.loader import ESPLoader

from esp32_mock_bootloader import chips, mock_bootloader, protocol, registers, server

from esp32_mock_bootloader import constants, protocol_client
from tests.helpers import esptool
from esp32_mock_bootloader.advanced import protocol as protocol_api


def _client(mock):
    return protocol_api.connect(mock)


def test_server_accepts_connection(mock_server):
    client = _client(mock_server)
    assert mock_server.advanced.transport is not None


def test_sync_response(mock_server):
    client = _client(mock_server)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SYNC, constants.SYNC_PAYLOAD), 8192)
    frames = client.decode_frames(raw)
    assert len(frames) == constants.SYNC_RESPONSE_COUNT
    d, c, _s, v, data = client.parse_response(frames[0])
    assert d == 0x01  # response direction byte
    assert c == protocol.CMD_SYNC
    assert v != 0  # ROM SYNC value field (stub returns 0)
    assert data[0] == 0


def test_read_reg_chip_detect(mock_server, reference_chip):
    profile = chips.PROFILES[reference_chip]
    if not profile.detect_magic:
        pytest.skip(f'{reference_chip} has no detect magic register')
    client = _client(mock_server)
    _client(mock_server).sync()
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_READ_REG, struct.pack('<I', profile.detect_reg)),
    )
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, _c, _s, value, _data = client.parse_response(frames[0])
    assert value == profile.detect_magic



def test_read_reg_efuse(mock_server, reference_chip):
    profile = chips.PROFILES[reference_chip]
    client = _client(mock_server)
    _client(mock_server).sync()
    efuse_addr = profile.efuse_base + 0x04
    raw = client.send_and_receive(protocol_client.make_command(
            protocol.CMD_READ_REG, struct.pack('<I', efuse_addr),
        ),
    )
    expected = registers.rom_profile(reference_chip).get(efuse_addr, 0)
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, _c, _s, value, _data = client.parse_response(frames[0])
    assert value == expected


def test_flash_begin(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    raw = client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x1000, 1, 0x1000, constants.FLASH_APP_OFFSET),
        ),
    )
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = client.parse_response(frames[0])
    assert c == protocol.CMD_FLASH_BEGIN
    assert data[0] == 0


def test_flash_data(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\xAB' * 0x100)  # arbitrary fill
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DATA, payload))
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = client.parse_response(frames[0])
    assert c == protocol.CMD_FLASH_DATA
    assert data[0] == 0


def test_flash_end_second_session(mock_server_persistent, reference_chip):
    client = _client(mock_server_persistent)
    _client(mock_server_persistent).sync()
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_END, struct.pack('<I', constants.STAY_IN_LOADER)),
    )
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = client.parse_response(frames[0])
    assert c == protocol.CMD_FLASH_END
    assert data[0] == 0

    time.sleep(0.2)
    assert mock_server_persistent.returncode is None
    assert _client(mock_server_persistent).minimal_plain_flash()


def test_flash_defl(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    raw = client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_DEFL_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, constants.FLASH_APP_OFFSET),
        ),
    )
    assert len(client.decode_frames(raw)) >= 1
    # Uncompressed placeholder block (not valid zlib; exercises DEFL_DATA path only).
    payload = struct.pack('<IIII', 0x20, 0, 0, 0) + (b'\x00' * 0x20)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DEFL_DATA, payload))
    assert len(client.decode_frames(raw)) >= 1
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DEFL_END, struct.pack('<I', constants.STAY_IN_LOADER)))
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = client.parse_response(frames[0])
    assert c == protocol.CMD_FLASH_DEFL_END
    assert mock_server.returncode is None


def test_mem_sequence_ohai(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_MEM_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, constants.STUB_IRAM_ENTRY),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\x00' * 0x100)
    client.send_and_receive(protocol_client.make_command(protocol.CMD_MEM_DATA, payload))
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_MEM_END, struct.pack('<II', constants.RUN_ENTRYPOINT, constants.STUB_IRAM_ENTRY)),
    )
    assert constants.OHAI_BYTES in raw


def test_write_reg(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    # Generic peripheral address in ESP32 address space (mock stores any WRITE_REG target).
    addr = 0x3FF00000
    value = 0x12345678
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_WRITE_REG, struct.pack('<IIII', addr, value, 0xFFFFFFFF, 0)),
    )
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = client.parse_response(frames[0])
    assert c == protocol.CMD_WRITE_REG
    assert data[0] == 0

    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_READ_REG, struct.pack('<I', addr)))
    _d, c, _s, reg_value, _data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_READ_REG
    assert reg_value == value


def test_change_baudrate(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_CHANGE_BAUDRATE, struct.pack('<II', 921600, 0)),
    )
    # Baud value matches a common esptool default; second word is reserved (0).
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = client.parse_response(frames[0])
    assert c == protocol.CMD_CHANGE_BAUDRATE


def test_spi_attach(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SPI_ATTACH, struct.pack('<I', 0)))
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = client.parse_response(frames[0])
    assert c == protocol.CMD_SPI_ATTACH


def test_spi_set_params(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    # SPI_SET_PARAMS layout from esptool: id, total_size, block_size, sector_size, page_size, status_mask
    params = struct.pack('<IIIIII', 0, 0x400000, constants.FLASH_APP_OFFSET, 0x1000, 0x100, 0xFFFF)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SPI_SET_PARAMS, params))
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = client.parse_response(frames[0])
    assert c == protocol.CMD_SPI_SET_PARAMS


def test_spi_flash_md5(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    md5_req = struct.pack('<IIII', constants.FLASH_APP_OFFSET, 0x1000, 0, 0)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = client.parse_response(frames[0])
    assert c == protocol.CMD_SPI_FLASH_MD5
    # Fresh mock flash is erased (0xFF); ROM MD5 returns 32-byte ASCII hex + 2-byte status.
    expected = hashlib.md5(b'\xff' * 0x1000).hexdigest().encode('ascii')
    assert data[:32] == expected
    assert data[32:34] == b'\x00\x00'


def test_spi_flash_md5_after_stub_upload(mock_server):
    """ROM hex MD5 before MEM_END; binary MD5 after stub entrypoint + OHAI."""
    client = _client(mock_server)
    _client(mock_server).sync()
    md5_req = struct.pack('<IIII', constants.FLASH_APP_OFFSET, 0x1000, 0, 0)
    digest = hashlib.md5(b'\xff' * 0x1000).digest()

    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_SPI_FLASH_MD5
    assert data[:32] == digest.hex().encode('ascii')
    assert data[32:34] == b'\x00\x00'

    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_MEM_END, struct.pack('<II', constants.RUN_ENTRYPOINT, constants.STUB_IRAM_ENTRY)),
    )
    assert constants.OHAI_BYTES in raw

    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_SPI_FLASH_MD5
    assert data[:16] == digest
    assert data[16:18] == b'\x00\x00'


def test_unknown_command(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    raw = client.send_and_receive(protocol_client.make_command(constants.UNKNOWN_COMMAND, b'\x00' * 4))
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, data = client.parse_response(frames[0])
    assert c == constants.UNKNOWN_COMMAND
    assert data[0] == 1
    assert data[1] == protocol.ROM_INVALID_MESSAGE  # 0x05 in ROM mode


def _address_requiring_slip_escape(base: int) -> int | None:
    for offset in range(0, 0x1000):
        addr = base + offset
        if 0xC0 in struct.pack('<I', addr):
            return addr
    return None


def test_slip_escape_decoding(mock_server, reference_chip):
    profile = chips.PROFILES[reference_chip]
    addr = _address_requiring_slip_escape(profile.efuse_base)
    if addr is None:
        pytest.skip(f'no SLIP-escape address in efuse window for {reference_chip}')
    client = _client(mock_server)
    _client(mock_server).sync()
    cmd_packet = protocol_client.make_command(protocol.CMD_READ_REG, struct.pack('<I', addr))
    assert protocol.SLIP_ESC in cmd_packet[1:-1]
    raw = client.send_and_receive(cmd_packet)
    frames = client.decode_frames(raw)
    assert len(frames) >= 1
    _d, c, _s, _v, _data = client.parse_response(frames[0])
    assert c == protocol.CMD_READ_REG


def test_server_timeout(reference_chip):
    with mock_bootloader(
        chip=reference_chip,
        mode='foreground',
        timeout=3.0,
        exit_on_disconnect=False,
        startup_timeout=30.0,
    ) as mock:
        start = time.time()
        mock._session.proc.wait(timeout=10)  # noqa: SLF001
        elapsed = time.time() - start
        assert mock.returncode is not None
        # --timeout is measured from server start; startup wait eats into the window.
        assert 0.5 <= elapsed <= 6.0


def test_multiple_frames_one_chunk(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    fb = protocol_client.make_command(
        protocol.CMD_FLASH_BEGIN,
        struct.pack('<IIII', 0x10, 1, 0x10, constants.FLASH_APP_OFFSET),
    )
    fd = protocol_client.make_command(protocol.CMD_FLASH_DATA, struct.pack('<IIII', 0x10, 0, 0, 0) + b'\xFF' * 0x10)
    fe = protocol_client.make_command(protocol.CMD_FLASH_END, struct.pack('<I', constants.STAY_IN_LOADER))
    mock_server.advanced.transport.sendall(fb + fd + fe)
    time.sleep(0.5)
    raw = b''
    mock_server.advanced.transport.settimeout(2.0)
    try:
        while True:
            chunk = mock_server.advanced.transport.recv(4096)
            if not chunk:
                break
            raw += chunk
    except socket.timeout:
        pass
    frames = client.decode_frames(raw)
    assert len(frames) == 3
    cmds = [client.parse_response(f)[1] for f in frames]
    assert cmds == [protocol.CMD_FLASH_BEGIN, protocol.CMD_FLASH_DATA, protocol.CMD_FLASH_END]
    assert mock_server.returncode is None


@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_explicit_chip_protocol_smoke(chip: str):
    """Minimal SYNC + plain flash for every esptool target."""
    with mock_bootloader(
        chip=chip,
        mode='foreground',
        timeout=None,
        exit_on_disconnect=False,
        startup_timeout=30.0,
    ) as mock:
        assert _client(mock).minimal_plain_flash()


def test_flash_data_checksum_error(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\xAB' * 0x100)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DATA, payload, checksum=0))
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_FLASH_DATA
    assert data[0] == 1
    assert data[1] == protocol.ROM_CHECKSUM_ERROR  # 0x07


def test_stub_erase_flash(mock_server):
    client = _client(mock_server)
    _client(mock_server).activate_stub()
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x100, 1, 0x100, constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\x00' * 0x100)
    client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DATA, payload))
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_ERASE_FLASH))
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_ERASE_FLASH
    assert data[0] == 0
    md5_req = struct.pack('<IIII', constants.FLASH_APP_OFFSET, 0x100, 0, 0)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert data[:16] == hashlib.md5(b'\xff' * 0x100).digest()


def test_stub_erase_region(mock_server):
    client = _client(mock_server)
    _client(mock_server).activate_stub()
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x200, 1, 0x200, constants.FLASH_APP_OFFSET),
        ),
    )
    payload = struct.pack('<IIII', 0x200, 0, 0, 0) + (b'\x11' * 0x200)
    client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DATA, payload))
    # Erase only the first 0x100 bytes of the 0x200-byte block.
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_ERASE_REGION, struct.pack('<II', constants.FLASH_APP_OFFSET, 0x100)),
    )
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_ERASE_REGION
    assert data[0] == 0
    md5_req = struct.pack('<IIII', constants.FLASH_APP_OFFSET, 0x200, 0, 0)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    digest = hashlib.md5(b'\xff' * 0x100 + b'\x11' * 0x100).digest()
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert data[:16] == digest


def test_stub_read_flash(mock_server):
    client = _client(mock_server)
    _client(mock_server).activate_stub()
    offset = constants.FLASH_APP_OFFSET
    length = 0x400  # one sector; arbitrary size for READ_FLASH smoke test
    client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_BEGIN, struct.pack('<IIII', length, 1, length, offset)),
    )
    block = b'\x5A' * length  # arbitrary fill; must round-trip via READ_FLASH + MD5
    payload = struct.pack('<IIII', length, 0, 0, 0) + block
    client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DATA, payload))

    data, digest = client.stub_read_flash( offset, length)
    assert data == block
    assert digest == hashlib.md5(block).digest()


def test_rom_read_flash_slow(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    # READ_FLASH_SLOW uses offset 0x20000 to avoid overlap with constants.FLASH_APP_OFFSET tests.
    slow_offset = 0x20000
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x40, 1, 0x40, slow_offset),
        ),
    )
    payload = struct.pack('<IIII', 0x40, 0, 0, 0) + (b'\x42' * 0x40)
    client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DATA, payload))
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_READ_FLASH_SLOW, struct.pack('<II', slow_offset, 0x20)),
    )
    _d, c, size, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_READ_FLASH_SLOW
    # ROM returns a fixed 64-byte buffer; size field includes 4-byte prefix per esptool.
    assert size == 64 + 4
    assert data[:0x20] == b'\x42' * 0x20
    assert data[0x20:0x40] == b'\xff' * 0x20  # remainder of 64-byte block is erased

def test_cmd_dict_matches_esptool():
    assert protocol.CMD == ESPLoader.ESP_CMDS
    assert protocol.CMD_SYNC == ESPLoader.ESP_CMDS['SYNC']
    assert hasattr(protocol, 'CMD_ERASE_FLASH')


def test_rom_constants_match_esptool():
    assert protocol.ROM_INVALID_MESSAGE == ESPLoader.ROM_INVALID_RECV_MSG
    assert protocol.CHECKSUM_SEED == ESPLoader.ESP_CHECKSUM_MAGIC
    assert protocol.FLASH_SECTOR_SIZE == ESPLoader.FLASH_SECTOR_SIZE


def test_data_checksum_matches_esptool():
    payload = b'\x01\x02\x03\xab'
    assert protocol.data_checksum(payload) == ESPLoader.checksum(payload)


@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        (b'', b''),
        (b'\x00' * 8, b''),
        (struct.pack('<IIII', 4, 0, 0, 0) + b'\xde\xad\xbe\xef', b'\xde\xad\xbe\xef'),
    ],
)
def test_data_command_payload(data: bytes, expected: bytes):
    assert protocol.data_command_payload(data) == expected


def test_slip_roundtrip_with_escapes():
    # Deliberately embed SLIP_END and SLIP_ESC to exercise escape encoding.
    raw = bytes([0x01, protocol.SLIP_END, protocol.SLIP_ESC, 0xFF])
    encoded = server.slip_encode(raw)
    assert encoded[0] == protocol.SLIP_END
    assert encoded[-1] == protocol.SLIP_END
    assert protocol.SLIP_ESC in encoded[1:-1]
    frames = server.slip_decode_frames(encoded)
    assert frames == [raw]


def test_checksum_valid_ignores_non_data_commands():
    # Checksum byte is only validated for FLASH_DATA / MEM_DATA / FLASH_DEFL_DATA.
    assert server.checksum_valid(protocol.CMD_FLASH_BEGIN, 0, b'\xff' * 32)


def test_checksum_valid_detects_bad_flash_data():
    payload = struct.pack('<IIII', 4, 0, 0, 0) + b'\x11\x22\x33\x44'
    data = payload
    good = protocol.data_checksum(b'\x11\x22\x33\x44')
    assert server.checksum_valid(protocol.CMD_FLASH_DATA, good, data)
    assert not server.checksum_valid(protocol.CMD_FLASH_DATA, good ^ 0xFF, data)


def test_make_response_status_byte_count():
    # ROM responses carry a 4-byte status word; stub responses use 2 bytes.
    rom = server.slip_decode_frames(server.make_response(0x02, stub=False))[0]
    stub = server.slip_decode_frames(server.make_response(0x02, stub=True))[0]
    assert struct.unpack_from('<H', rom, 2)[0] == 4
    assert struct.unpack_from('<H', stub, 2)[0] == 2


def test_spi_peripheral_mock_flash_id_and_sfdp():
    spi = server.SpiPeripheralMock(
        constants.ESP32_SPI_REG_BASE, usr2_offs=constants.ESP32_SPI_USR2_OFFS, w0_offs=constants.ESP32_SPI_W0_OFFS,
    )
    base = constants.ESP32_SPI_REG_BASE
    # SPI_USR2 holds the JEDEC command byte; SPI_USR starts the transaction.
    spi.write_reg(base + constants.ESP32_SPI_USR2_OFFS, constants.SPI_CMD_READ_ID, 0xFFFFFFFF)
    spi.write_reg(base + 0x00, protocol.SPI_CMD_USR, 0xFFFFFFFF)
    assert spi.read_reg(base + 0x00) == 0
    assert spi.read_reg(base + constants.ESP32_SPI_W0_OFFS) == protocol.MOCK_FLASH_ID
    spi.write_reg(base + constants.ESP32_SPI_USR2_OFFS, constants.SPI_CMD_RDSFDP, 0xFFFFFFFF)
    spi.write_reg(base + 0x00, protocol.SPI_CMD_USR, 0xFFFFFFFF)
    assert spi.read_reg(base + constants.ESP32_SPI_W0_OFFS) == protocol.SFDP_SIGNATURE


def test_spi_peripheral_mock_configure_from_addr():
    spi = server.SpiPeripheralMock(None)
    from esptool.targets import CHIP_DEFS

    # configure_from_addr infers chip SPI layout from any register in the peripheral block.
    base = CHIP_DEFS['esp32c3'].SPI_REG_BASE
    assert spi.configure_from_addr(base + CHIP_DEFS['esp32c3'].SPI_USR2_OFFS)
    assert spi.spi_base == base


def test_spi_peripheral_mock_write_mask():
    spi = server.SpiPeripheralMock(
        constants.ESP32C3_SPI_REG_BASE, usr2_offs=0x20, w0_offs=constants.ESP32C3_SPI_W0_OFFS,
    )
    addr = constants.ESP32C3_SPI_REG_BASE + constants.ESP32C3_SPI_W0_OFFS
    # WRITE_REG mask 0x0000FFFF clears the upper half before applying new bits.
    spi.write_reg(addr, 0xFFFF0000, 0x0000FFFF)
    assert spi.read_reg(addr) == 0x00000000
    spi.write_reg(addr, 0x12345678, 0xFFFFFFFF)
    assert spi.read_reg(addr) == 0x12345678


def test_flash_image_plain_write_and_erase():
    flash = server.FlashImage()
    test_offset = 0x5000
    block_size = 0x200
    flash.begin_plain(struct.pack('<IIII', block_size, 1, block_size, test_offset))
    flash.write_plain_block(
        struct.pack('<IIII', block_size, 0, 0, 0) + (b'\xCC' * block_size),
    )
    assert bytes(flash.data[test_offset:test_offset + block_size]) == b'\xCC' * block_size
    flash.erase_region(test_offset, 0x100)
    assert bytes(flash.data[test_offset:test_offset + 0x100]) == b'\xff' * 0x100
    assert bytes(flash.data[test_offset + 0x100:test_offset + block_size]) == b'\xCC' * 0x100
    flash.erase_all()
    assert bytes(flash.data[test_offset:test_offset + block_size]) == b'\xff' * block_size


def test_flash_image_read_bounds():
    flash = server.FlashImage()
    flash.data[0] = 0xAB
    # Unwritten bytes read as erased (0xFF).
    assert flash.read(0, 4) == b'\xab\xff\xff\xff'
    assert flash.read(protocol.FLASH_MOCK_SIZE, 16) == b''


def test_ram_image_write_block():
    ram = server.RamImage()
    ram_dest = 0x1000
    block_size = 0x40
    ram.begin(struct.pack('<IIII', block_size, 1, block_size, ram_dest))
    ram.write_block(struct.pack('<IIII', block_size, 0, 0, 0) + (b'\x77' * block_size))
    assert bytes(ram.data[ram_dest:ram_dest + block_size]) == b'\x77' * block_size


def test_chip_session_configures_spi_for_explicit_chip():
    from esptool.targets import CHIP_DEFS

    session = server.ChipSession('esp32c3')
    assert session.spi.spi_base == CHIP_DEFS['esp32c3'].SPI_REG_BASE


def test_flash_image_md5_formats():
    flash = server.FlashImage()
    flash.data[0:16] = bytes(range(16))
    req = struct.pack('<IIII', 0, 16, 0, 0)
    digest = hashlib.md5(bytes(range(16))).digest()
    # ROM SPI_FLASH_MD5 returns a 32-byte ASCII hex digest; stub returns 16 raw bytes.
    rom_frame = protocol_client.slip_decode_frames(flash.md5_stub_response(req))[0]
    stub_frame = protocol_client.slip_decode_frames(flash.md5_stub_response(req, stub=True))[0]
    assert rom_frame[8:40] == digest.hex().encode('ascii')
    assert stub_frame[8:24] == digest

def test_get_security_info_auto_before_detection():
    with mock_bootloader(chip='auto', mode='foreground', timeout=None, exit_on_disconnect=False) as mock:
        client = _client(mock)
        client.sync()
        raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_GET_SECURITY_INFO))
        _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
        assert c == protocol.CMD_GET_SECURITY_INFO
        # Auto mode before chip detection: flags=1, error=ROM_INVALID_MESSAGE (0x05).
        assert data[0] == 1
        assert data[1] == protocol.ROM_INVALID_MESSAGE


def test_read_reg_legacy_deferred_until_chip_evidence():
    with mock_bootloader(chip='auto', mode='foreground', timeout=None, exit_on_disconnect=False) as mock:
        client = _client(mock)
        client.sync()
        raw = client.send_and_receive(
            protocol_client.make_command(protocol.CMD_READ_REG, struct.pack('<I', chips.LEGACY_DETECT_REG)),
        )
        assert client.parse_response(client.decode_frames(raw)[0])[3] == 0


def test_get_security_info_explicit_chip():
    """ESP32 uses magic validation; GET_SECURITY_INFO is unsupported on ROM."""
    with mock_bootloader(chip='esp32', mode='foreground', timeout=None, exit_on_disconnect=False) as mock:
        client = _client(mock)
        client.sync()
        raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_GET_SECURITY_INFO))
        _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
        assert c == protocol.CMD_GET_SECURITY_INFO
        assert data[0] != 0


def test_get_security_info_modern_chip():
    with mock_bootloader(chip='esp32c3', mode='foreground', timeout=None, exit_on_disconnect=False) as mock:
        client = _client(mock)
        client.sync()
        raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_GET_SECURITY_INFO))
        _d, c, size, _v, data = client.parse_response(client.decode_frames(raw)[0])
        assert c == protocol.CMD_GET_SECURITY_INFO
        assert data[0] == 0
        assert size == 22
        chip_id = struct.unpack_from('<I', data, 12)[0]
        assert chip_id == chips.PROFILES['esp32c3'].image_chip_id
        assert data[20:22] == b'\x00\x00'


def test_stub_unknown_command_returns_unimplemented(mock_server):
    client = _client(mock_server)
    _client(mock_server).activate_stub()
    raw = client.send_and_receive(protocol_client.make_command(constants.UNKNOWN_COMMAND, b''))
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == constants.UNKNOWN_COMMAND
    assert data[0] == 1
    assert data[1] == protocol.STUB_UNIMPLEMENTED


def test_mem_data_checksum_error_stub_mode(mock_server):
    client = _client(mock_server)
    _client(mock_server).activate_stub()
    # Small 16-byte MEM_DATA block; 0x55 fill is arbitrary (checksum must still match).
    payload = struct.pack('<IIII', 0x10, 0, 0, 0) + (b'\x55' * 0x10)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_MEM_DATA, payload, checksum=0),
    )
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_MEM_DATA
    assert data[0] == 1
    assert data[1] == protocol.STUB_CHECKSUM_ERROR


def test_flash_defl_data_checksum_error(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_DEFL_BEGIN,
            struct.pack('<IIII', 0x40, 1, 0x40, constants.FLASH_APP_OFFSET),
        ),
    )
    # 0x78 0x9c is the zlib header magic; content is invalid on purpose (checksum=0).
    payload = struct.pack('<IIII', 0x10, 0, 0, 0) + (b'\x78\x9c' + b'\x00' * 12)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DEFL_DATA, payload, checksum=0),
    )
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_FLASH_DEFL_DATA
    assert data[0] == 1
    assert data[1] == protocol.ROM_CHECKSUM_ERROR


def test_write_reg_partial_mask(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    # Arbitrary MMIO address in ESP32 peripheral range (not tied to a real register).
    addr = 0x3FF0ABCD
    client.send_and_receive(protocol_client.make_command(protocol.CMD_WRITE_REG, struct.pack('<IIII', addr, 0xFFFFFFFF, 0xFFFFFFFF, 0)),
    )
    client.send_and_receive(protocol_client.make_command(protocol.CMD_WRITE_REG, struct.pack('<IIII', addr, 0x12340000, 0xFFFF0000, 0)),
    )
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_READ_REG, struct.pack('<I', addr)))
    _d, _c, _s, value, _data = client.parse_response(client.decode_frames(raw)[0])
    assert value == 0x1234FFFF


def test_mem_end_stay_in_loader_no_ohai(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_MEM_BEGIN,
            struct.pack('<IIII', 0x10, 1, 0x10, constants.STUB_IRAM_ENTRY),
        ),
    )
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_MEM_DATA,
            struct.pack('<IIII', 0x10, 0, 0, 0) + (b'\x00' * 0x10),
        ),
    )
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_MEM_END, struct.pack('<II', constants.STAY_IN_LOADER, 0)),
    )
    # OHAI is only sent when MEM_END requests run-at-entrypoint (stay_in_loader=0).
    assert constants.OHAI_BYTES not in raw


def test_rom_stub_only_commands_rejected_before_stub(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    for cmd in (
        protocol.CMD_ERASE_FLASH,
        protocol.CMD_ERASE_REGION,
        protocol.CMD_READ_FLASH,
    ):
        data = (
            struct.pack('<II', constants.FLASH_APP_OFFSET, protocol.FLASH_SECTOR_SIZE)
            if cmd == protocol.CMD_ERASE_REGION
            else b''
        )
        if cmd == protocol.CMD_READ_FLASH:
            # READ_FLASH: offset, length, packet_size, max_in_flight (4 is a small test value).
            data = struct.pack(
                '<IIII', constants.FLASH_APP_OFFSET, 0x100, protocol.FLASH_SECTOR_SIZE, 4,
            )
        raw = client.send_and_receive(protocol_client.make_command(cmd, data))
        _d, c, _s, _v, resp = client.parse_response(client.decode_frames(raw)[0])
        assert c == cmd
        assert resp[0] == 1
        assert resp[1] == protocol.ROM_INVALID_MESSAGE


def test_read_reg_empty_payload(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_READ_REG, b''))
    _d, _c, _s, value, _data = client.parse_response(client.decode_frames(raw)[0])
    assert value == 0


def test_write_reg_short_payload(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_WRITE_REG, struct.pack('<II', 0, 0)),
    )
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_WRITE_REG
    assert data[0] == 1


def test_sync_stub_value_zero_after_activate(mock_server):
    client = _client(mock_server)
    _client(mock_server).activate_stub()
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SYNC, constants.SYNC_PAYLOAD), 8192)
    frames = client.decode_frames(raw)
    _d, c, _s, value, data = client.parse_response(frames[0])
    assert c == protocol.CMD_SYNC
    # Stub SYNC response value field is 0 (ROM returns protocol.ROM_SYNC_VALUE).
    assert value == 0
    assert data[0] == 0



def test_flash_defl_valid_compressed_block(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    plain = b'\x00' * 0x80
    compressed = zlib.compress(plain)
    # 0x30000 is an arbitrary flash offset away from constants.FLASH_APP_OFFSET test traffic.
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_DEFL_BEGIN,
            struct.pack('<IIII', 0x80, 1, len(compressed), 0x30000),
        ),
    )
    payload = struct.pack('<IIII', len(compressed), 0, 0, 0) + compressed
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DEFL_DATA, payload))
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_FLASH_DEFL_DATA
    assert data[0] == 0


def test_stub_read_flash_multiple_packets(mock_server):
    client = _client(mock_server)
    _client(mock_server).activate_stub()
    # Use a non-default offset so READ_FLASH is not confused with prior constants.FLASH_APP_OFFSET tests.
    offset = 0x20000
    length = protocol.FLASH_SECTOR_SIZE * 2
    block = bytes((i & 0xFF for i in range(length)))  # gradient pattern for MD5 uniqueness
    client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_BEGIN, struct.pack('<IIII', length, 1, length, offset)),
    )
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_DATA,
            struct.pack('<IIII', length, 0, 0, 0) + block,
        ),
    )
    data, digest = client.stub_read_flash(
        offset, length, packet_size=protocol.FLASH_SECTOR_SIZE, max_in_flight=2,
    )
    assert data == block
    assert digest == __import__('hashlib').md5(block).digest()


def test_flash_data_sequence_number_preserved(mock_server):
    client = _client(mock_server)
    _client(mock_server).sync()
    client.send_and_receive(protocol_client.make_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', 0x200, 2, 0x100, constants.FLASH_APP_OFFSET),
        ),
    )
    for seq in range(2):
        # Sequence numbers 0 and 1; fill 0x01 / 0x02 makes MD5 predictable.
        chunk = bytes([seq + 1]) * 0x100
        payload = struct.pack('<IIII', 0x100, seq, 0, 0) + chunk
        client.send_and_receive(protocol_client.make_command(protocol.CMD_FLASH_DATA, payload))
    md5_req = struct.pack('<IIII', constants.FLASH_APP_OFFSET, 0x200, 0, 0)
    raw = client.send_and_receive(protocol_client.make_command(protocol.CMD_SPI_FLASH_MD5, md5_req))
    import hashlib
    expected = hashlib.md5(b'\x01' * 0x100 + b'\x02' * 0x100).hexdigest().encode('ascii')
    _d, c, _s, _v, data = client.parse_response(client.decode_frames(raw)[0])
    assert c == protocol.CMD_SPI_FLASH_MD5
    assert data[:32] == expected


# --- TCP transport smoke (mock_bootloader + protocol_client) ---


@pytest.mark.transport
@pytest.mark.esptool
@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_protocol_smoke_tcp(chip: str):
    with mock_bootloader(chip=chip, exit_on_disconnect=True, timeout=30.0, mode='foreground') as boot:
        assert protocol_api.connect(boot).minimal_plain_flash()


@pytest.mark.transport
@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_explicit_chip_detect_register_tcp(chip: str):
    profile = chips.PROFILES[chip]
    if not profile.detect_magic:
        pytest.skip(f'{chip} has no detect magic register')
    with mock_bootloader(chip=chip, timeout=30.0, mode='foreground') as boot:
        client = protocol_api.connect(boot)
        client.sync()
        assert client.read_reg(profile.detect_reg) == profile.detect_magic


@pytest.mark.transport
@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_chip_profile_registers_tcp(chip: str):
    profile = chips.PROFILES[chip]
    with mock_bootloader(chip=chip, timeout=30.0, mode='foreground') as boot:
        client = protocol_api.connect(boot)
        client.sync()
        if profile.detect_magic:
            assert client.read_reg(profile.detect_reg) == profile.detect_magic
        efuse_addr = profile.efuse_base + 0x04
        assert client.read_reg(efuse_addr) == registers.rom_profile(chip).get(efuse_addr, 0)
        assert protocol_api.connect(boot).minimal_plain_flash()


@pytest.mark.transport
@pytest.mark.esptool
@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_esptool_write_flash_tcp(chip: str):
    with mock_bootloader(chip=chip, exit_on_disconnect=True, timeout=30.0, mode='foreground') as boot:
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
            ok, detail = esptool.write_flash_no_stub(chip, str(bin_file), port=boot.port())
            assert ok, detail


@pytest.mark.transport
@pytest.mark.esptool
@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_auto_mode_esptool_write_flash_tcp(chip: str):
    with mock_bootloader(chip='auto', exit_on_disconnect=True, timeout=30.0, mode='foreground') as boot:
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
            ok, detail = esptool.write_flash_no_stub(chip, str(bin_file), port=boot.port())
            assert ok, detail
