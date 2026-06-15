# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Bootloader protocol constants — derived from esptool where possible."""

from __future__ import annotations

import struct

from esptool.loader import ESPLoader

# Command opcodes (single source: esptool ESPLoader.ESP_CMDS).
CMD: dict[str, int] = dict(ESPLoader.ESP_CMDS)

for _name, _value in CMD.items():
    globals()[f'CMD_{_name}'] = _value

DATA_CHECKSUM_CMDS = frozenset({
    CMD['FLASH_DATA'],
    CMD['MEM_DATA'],
    CMD['FLASH_DEFL_DATA'],
})


def data_command_payload(data: bytes) -> bytes:
    if len(data) < 16:
        return b''
    data_len = struct.unpack_from('<I', data, 0)[0]
    return data[16:16 + data_len]


# ROM bootloader parameters exposed by esptool.
ROM_INVALID_MESSAGE = ESPLoader.ROM_INVALID_RECV_MSG
CHECKSUM_SEED = ESPLoader.ESP_CHECKSUM_MAGIC
FLASH_SECTOR_SIZE = ESPLoader.FLASH_SECTOR_SIZE
data_checksum = ESPLoader.checksum

# ROM error codes (second byte of esptool.util.FatalError composite keys, e.g. 0x107).
ROM_CHECKSUM_ERROR = 0x107 & 0xFF

# SLIP framing (serial protocol spec — not defined in esptool).
SLIP_END = 0xC0
SLIP_ESC = 0xDB
SLIP_ESC_END = 0xDC
SLIP_ESC_DD = 0xDD

# Mock-only / not exported as named constants by esptool.
ROM_SYNC_VALUE = 0x20120707  # SYNC response value field (ROM only).
FLASH_MOCK_SIZE = 4 * 1024 * 1024
READ_FLASH_SLOW_BLOCK_LEN = 64  # esp32.read_flash_slow() local BLOCK_LEN.
SPI_CMD_USR = 1 << 18  # run_spiflash_command() local flag.
MOCK_FLASH_ID = 0x00164020  # plausible JEDEC ID for flash verification.
SFDP_SIGNATURE = 0x50444653  # cmds._verify_flash_connection SFDP signature.
EFUSE_WINDOW = 0x200  # auto-detect heuristic window size.

# Stub loader errors (serial protocol doc; high byte of esptool err_defs keys).
STUB_CHECKSUM_ERROR = 0xC100 >> 8
STUB_UNIMPLEMENTED = 0xFF00 >> 8
