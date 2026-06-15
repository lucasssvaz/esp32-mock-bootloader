# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Pure unit tests for protocol helpers and server internals.

Hardcoded values in this module:
- SPI register addresses (0x3FF42000 etc.) come from esptool CHIP_DEFS per target.
- JEDEC opcodes 0x9F (RDID) and 0x5A (RDSFDP) are standard SPI flash commands.
- FLASH_BEGIN/MEM_BEGIN struct fields: size, num_blocks, block_size, offset (esptool layout).
- 0x5000 / 0x30000 flash offsets are arbitrary test addresses inside the mock flash image.
- 0xFF erase byte and protocol.FLASH_MOCK_SIZE bounds match esptool flash semantics.
"""

from __future__ import annotations

import hashlib
import struct

import pytest

import esp32_mock_bootloader.testing as mock
from esp32_mock_bootloader import protocol
from esp32_mock_bootloader import server
from esptool.loader import ESPLoader


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
        mock.constants.ESP32_SPI_REG_BASE, usr2_offs=mock.constants.ESP32_SPI_USR2_OFFS, w0_offs=mock.constants.ESP32_SPI_W0_OFFS,
    )
    base = mock.constants.ESP32_SPI_REG_BASE
    # SPI_USR2 holds the JEDEC command byte; SPI_USR starts the transaction.
    spi.write_reg(base + mock.constants.ESP32_SPI_USR2_OFFS, mock.constants.SPI_CMD_READ_ID, 0xFFFFFFFF)
    spi.write_reg(base + 0x00, protocol.SPI_CMD_USR, 0xFFFFFFFF)
    assert spi.read_reg(base + 0x00) == 0
    assert spi.read_reg(base + mock.constants.ESP32_SPI_W0_OFFS) == protocol.MOCK_FLASH_ID
    spi.write_reg(base + mock.constants.ESP32_SPI_USR2_OFFS, mock.constants.SPI_CMD_RDSFDP, 0xFFFFFFFF)
    spi.write_reg(base + 0x00, protocol.SPI_CMD_USR, 0xFFFFFFFF)
    assert spi.read_reg(base + mock.constants.ESP32_SPI_W0_OFFS) == protocol.SFDP_SIGNATURE


def test_spi_peripheral_mock_configure_from_addr():
    spi = server.SpiPeripheralMock(None)
    from esptool.targets import CHIP_DEFS

    # configure_from_addr infers chip SPI layout from any register in the peripheral block.
    base = CHIP_DEFS['esp32c3'].SPI_REG_BASE
    assert spi.configure_from_addr(base + CHIP_DEFS['esp32c3'].SPI_USR2_OFFS)
    assert spi.spi_base == base


def test_spi_peripheral_mock_write_mask():
    spi = server.SpiPeripheralMock(
        mock.constants.ESP32C3_SPI_REG_BASE, usr2_offs=0x20, w0_offs=mock.constants.ESP32C3_SPI_W0_OFFS,
    )
    addr = mock.constants.ESP32C3_SPI_REG_BASE + mock.constants.ESP32C3_SPI_W0_OFFS
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
    rom_frame = mock.protocol.slip_decode_frames(flash.md5_stub_response(req))[0]
    stub_frame = mock.protocol.slip_decode_frames(flash.md5_stub_response(req, stub=True))[0]
    assert rom_frame[8:40] == digest.hex().encode('ascii')
    assert stub_frame[8:24] == digest
