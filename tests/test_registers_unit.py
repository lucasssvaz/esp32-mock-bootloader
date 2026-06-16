# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""ROM register profile unit tests."""

from __future__ import annotations

import struct

from esptool.loader import ESPLoader
from esptool.targets import CHIP_DEFS

from esp32_mock_bootloader import registers
from esp32_mock_bootloader import chips


def test_mac_bytes_use_espressif_oui():
    mac = registers.mac_bytes_for_chip('esp32c3')
    assert mac[:3] == (0x24, 0x0A, 0xC4)


def test_mac_bytes_documented_examples():
    """Golden values for the SHA256(chip) suffix documented in README."""
    assert registers.mac_bytes_for_chip('esp32') == (
        0x24, 0x0A, 0xC4, 0xE2, 0x95, 0x26,
    )
    assert registers.mac_bytes_for_chip('esp32c3') == (
        0x24, 0x0A, 0xC4, 0x1F, 0x67, 0x7D,
    )
    assert registers.mac_bytes_for_chip('esp8266') == (
        0x24, 0x0A, 0xC4, 0xCE, 0x2B, 0xC1,
    )


def test_mac_bytes_stable_per_chip():
    a = registers.mac_bytes_for_chip('esp32')
    b = registers.mac_bytes_for_chip('esp32')
    c = registers.mac_bytes_for_chip('esp32c3')
    assert a == b
    assert a != c


def test_uart_clkdiv_default_crystal_mhz():
    div = registers.uart_clkdiv_for_chip('esp32c3', crystal_mhz=None)
    assert div is not None


def test_mac_registers_empty_for_unknown_chip():
    assert registers._mac_registers('not-a-real-chip', (1, 2, 3, 4, 5, 6)) == {}


def test_crystal_registers_empty_for_unknown_chip():
    assert registers._crystal_registers('not-a-real-chip') == {}


def test_uart_clkdiv_esp32_26mhz():
    div = registers.uart_clkdiv_for_chip('esp32', crystal_mhz=26)
    assert div is not None
    est = 115200 * div / 1e6
    assert 24 < est < 28


def test_uart_clkdiv_esp8266_uses_double_xtal():
    div = registers.uart_clkdiv_for_chip('esp8266', crystal_mhz=26)
    assert div is not None
    est = 115200 * div / 1e6 / 2
    assert 24 < est < 28


def test_esp32_rom_profile_includes_rtccalicfg1():
    profile = registers.rom_profile('esp32')
    rom = CHIP_DEFS['esp32']
    assert int(rom.RTCCALICFG1) in profile
    assert profile[int(rom.RTCCALICFG1)] != 0


def test_c3_mac_registers_round_trip():
    chip = 'esp32c3'
    mac = registers.mac_bytes_for_chip(chip)
    profile = registers.rom_profile(chip)
    rom = CHIP_DEFS[chip]
    mac0 = profile[int(rom.MAC_EFUSE_REG)]
    mac1 = profile[int(rom.MAC_EFUSE_REG) + 4]
    decoded = struct.pack('>II', mac1, mac0)[2:]
    assert tuple(decoded) == mac


def test_esp32_mac_registers_round_trip():
    chip = 'esp32'
    mac = registers.mac_bytes_for_chip(chip)
    profile = registers.rom_profile(chip)
    rom = CHIP_DEFS[chip]
    base = int(rom.EFUSE_RD_REG_BASE)
    efuse1 = profile[base + 4]
    efuse2 = profile[base + 8]
    decoded = struct.pack('>II', efuse2, efuse1)[2:]
    assert tuple(decoded) == mac


def test_uart_clkdiv_unknown_chip():
    assert registers.uart_clkdiv_for_chip('not-a-real-chip') is None


def test_rom_profile_unknown_chip():
    assert registers.rom_profile('not-a-real-chip') == {}


def test_read_reg_value_detect_magic_short_circuit():
    chip = 'esp32c3'
    reg = chips.PROFILES[chip].detect_reg
    magic = chips.PROFILES[chip].detect_magic
    assert registers.read_reg_value(chip, reg) == magic

