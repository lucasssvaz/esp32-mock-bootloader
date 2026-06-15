# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Documented protocol and layout reference values for tests and CI."""

from __future__ import annotations

from esp32_mock_bootloader import chips

# All esptool targets — single source via chips / esptool.targets.CHIP_LIST.
ESPTOOL_CHIPS = list(chips.SUPPORTED)

TRANSPORTS = ['tcp', 'pty']

# SYNC payload: 4-byte ROM marker 0x07071220 + 32 × 0x55 for UART baud detect.
# esptool loader.sync() sends the same body; ROM returns 8 duplicate responses.
# Ref: Espressif serial-protocol "Initialization" section.
SYNC_PAYLOAD = bytes([0x07, 0x07, 0x12, 0x20]) + (b'\x55' * 32)
SYNC_RESPONSE_COUNT = 8  # ROM bootloader echoes SYNC this many times before idle.

# Response packet direction byte (requests use 0x00).
SLIP_RESPONSE_DIRECTION = 0x01

# Typical application image offset in ESP-IDF / arduino-esp32 (after 0x1000 bootloader
# and 0x8000 partition table). Used by write_flash() and many smoke tests.
FLASH_APP_OFFSET = 0x10000

# IRAM address esptool passes to MEM_END when uploading the flasher stub.
STUB_IRAM_ENTRY = 0x40080000

# Minimal stub upload size in activate_stub() — one MEM_DATA block (256 bytes).
STUB_UPLOAD_BLOCK = 0x100

# Unsolicited SLIP frame sent after MEM_END entrypoint; ASCII "OHAI".
OHAI_BYTES = bytes([0x4F, 0x48, 0x41, 0x49])

# FLASH_END / MEM_END flag: 1 = stay in loader, 0 = run entrypoint.
STAY_IN_LOADER = 1
RUN_ENTRYPOINT = 0

# SPI flash erase value and esptool sector size for --diff-with tests.
FLASH_ERASE_BYTE = 0xFF
DIFF_FLASH_SECTOR_COUNT = 4  # 4 × 4 KiB sectors (enough for multi-sector diff tests)

# JEDEC flash ID command and SFDP command bytes (SPI peripheral bit-bang path).
SPI_CMD_READ_ID = 0x9F
SPI_CMD_RDSFDP = 0x5A

# ESP32 SPI peripheral register map (esptool.targets.esp32.ESP32ROM).
ESP32_SPI_REG_BASE = 0x3FF42000
ESP32_SPI_USR2_OFFS = 0x24
ESP32_SPI_W0_OFFS = 0x80

# ESP32-C3 SPI peripheral register map (esptool.targets.esp32c3.ESP32C3ROM).
ESP32C3_SPI_REG_BASE = 0x60002000
ESP32C3_SPI_W0_OFFS = 0x58

# Unknown opcode for "unimplemented command" negative tests (not in ESP_CMDS).
UNKNOWN_COMMAND = 0xFE

__all__ = [
    'DIFF_FLASH_SECTOR_COUNT',
    'ESPTOOL_CHIPS',
    'ESP32C3_SPI_REG_BASE',
    'ESP32C3_SPI_W0_OFFS',
    'ESP32_SPI_REG_BASE',
    'ESP32_SPI_USR2_OFFS',
    'ESP32_SPI_W0_OFFS',
    'FLASH_APP_OFFSET',
    'FLASH_ERASE_BYTE',
    'OHAI_BYTES',
    'RUN_ENTRYPOINT',
    'SLIP_RESPONSE_DIRECTION',
    'SPI_CMD_RDSFDP',
    'SPI_CMD_READ_ID',
    'STAY_IN_LOADER',
    'STUB_IRAM_ENTRY',
    'STUB_UPLOAD_BLOCK',
    'SYNC_PAYLOAD',
    'SYNC_RESPONSE_COUNT',
    'TRANSPORTS',
    'UNKNOWN_COMMAND',
]
