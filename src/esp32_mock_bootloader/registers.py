# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Sparse ROM register profiles for esptool connect-time fidelity."""

from __future__ import annotations

import hashlib
import struct
from functools import lru_cache

from esptool.loader import ESPLoader
from esptool.targets import CHIP_DEFS

from esp32_mock_bootloader import chips

# Espressif OUI for synthetic mock MACs. These addresses are not burned on real
# silicon and must not be treated as globally unique device identifiers.
_MOCK_MAC_OUI = (0x24, 0x0A, 0xC4)

# Crystal defaults aligned with common dev boards.
_CRYSTAL_MHZ_LEGACY = 26
_CRYSTAL_MHZ_MODERN = 40

_LEGACY_CRYSTAL_CHIPS = frozenset({'esp32', 'esp8266'})


def mac_bytes_for_chip(chip: str) -> tuple[int, int, int, int, int, int]:
    """Return the synthetic BASE_MAC bytes encoded into the ROM profile for *chip*.

    esptool only requires a non-zero address that decodes correctly from each
    chip family's MAC/efuse register layout (see ``read_mac()`` in esptool ROM
    classes). The mock does not emulate factory MAC allocation.

    Format: ``24:0A:C4:XX:YY:ZZ`` where ``XX:YY:ZZ`` are the first three bytes
    of ``SHA256(chip.encode())``. That yields a stable, chip-specific suffix
    without hand-maintaining a table when esptool adds SoCs. A single fixed MAC
    for every chip would also satisfy esptool; per-chip suffixes make ``flash-id``
    output easier to tell apart in multi-SoC CI logs.

    Examples: ``esp32`` → ``24:0a:c4:e2:95:26``,
    ``esp32c3`` → ``24:0a:c4:1f:67:7d``.
    """
    suffix = hashlib.sha256(chip.encode()).digest()[:3]
    return _MOCK_MAC_OUI + (suffix[0], suffix[1], suffix[2])


def default_crystal_mhz(chip: str) -> int:
    if chip in _LEGACY_CRYSTAL_CHIPS:
        return _CRYSTAL_MHZ_LEGACY
    return _CRYSTAL_MHZ_MODERN


def uart_clkdiv_for_chip(
    chip: str,
    baud: int = ESPLoader.ESP_ROM_BAUD,
    crystal_mhz: int | None = None,
) -> int | None:
    """UART_CLKDIV value for chips using ESPLoader.get_crystal_freq()."""
    rom = CHIP_DEFS.get(chip)
    if rom is None or not hasattr(rom, 'UART_CLKDIV_REG'):
        return None
    if crystal_mhz is None:
        crystal_mhz = default_crystal_mhz(chip)
    divider = int(getattr(rom, 'XTAL_CLK_DIVIDER', 1))
    return int(crystal_mhz * 1_000_000 * divider / baud)


def _encode_mac_c3_family(mac: tuple[int, ...], mac_reg: int) -> dict[int, int]:
    mac1, mac0 = struct.unpack('>II', b'\x00\x00' + bytes(mac))
    return {mac_reg: mac0, mac_reg + 4: mac1}


def _encode_mac_esp32(mac: tuple[int, ...]) -> dict[int, int]:
    rom = CHIP_DEFS['esp32']
    base = int(rom.EFUSE_RD_REG_BASE)
    efuse2, efuse1 = struct.unpack('>II', b'\x00\x00' + bytes(mac))
    return {base + 4: efuse1, base + 8: efuse2}


def _encode_mac_esp8266(mac: tuple[int, ...]) -> dict[int, int]:
    rom = CHIP_DEFS['esp8266']
    mac3 = (mac[0] << 16) | (mac[1] << 8) | mac[2]
    mac1 = (mac[3] << 8) | mac[4]
    mac0 = mac[5] << 24
    return {
        int(rom.ESP_OTP_MAC0): mac0,
        int(rom.ESP_OTP_MAC1): mac1,
        int(rom.ESP_OTP_MAC3): mac3,
    }


def _encode_mac_s2(mac: tuple[int, ...]) -> dict[int, int]:
    rom = CHIP_DEFS['esp32s2']
    return _encode_mac_c3_family(mac, int(rom.MAC_EFUSE_REG))


def _mac_registers(chip: str, mac: tuple[int, ...]) -> dict[int, int]:
    if chip == 'esp8266':
        return _encode_mac_esp8266(mac)
    if chip == 'esp32':
        return _encode_mac_esp32(mac)
    if chip == 'esp32s2':
        return _encode_mac_s2(mac)
    rom = CHIP_DEFS.get(chip)
    if rom is not None and hasattr(rom, 'MAC_EFUSE_REG'):
        return _encode_mac_c3_family(mac, int(rom.MAC_EFUSE_REG))
    return {}


def _esp32_crystal_registers(crystal_mhz: int) -> dict[int, int]:
    rom = CHIP_DEFS['esp32']
    clk_8m = 128
    target_hz = 40_000_000 if crystal_mhz >= 40 else 26_000_000
    cali_val = max(1, int(target_hz * 40 / (15625 * clk_8m)))
    rtccalicfg1 = cali_val << int(rom.TIMERS_RTC_CALI_VALUE_S)
    efuse_base = int(rom.EFUSE_RD_REG_BASE)
    return {
        int(rom.RTCCALICFG1): rtccalicfg1,
        efuse_base + 16: clk_8m,
    }


def _crystal_registers(chip: str) -> dict[int, int]:
    crystal_mhz = default_crystal_mhz(chip)
    regs: dict[int, int] = {}
    rom = CHIP_DEFS.get(chip)
    if rom is None:
        return regs
    div = uart_clkdiv_for_chip(chip, crystal_mhz=crystal_mhz)
    if div is not None and hasattr(rom, 'UART_CLKDIV_REG'):
        regs[int(rom.UART_CLKDIV_REG)] = div
    if chip == 'esp32':
        regs.update(_esp32_crystal_registers(crystal_mhz))
    return regs


@lru_cache
def rom_profile(chip: str) -> dict[int, int]:
    """Sparse READ_REG defaults for esptool ROM-class connect behavior."""
    if chip not in CHIP_DEFS:
        return {}
    mac = mac_bytes_for_chip(chip)
    profile: dict[int, int] = {}
    profile.update(_mac_registers(chip, mac))
    profile.update(_crystal_registers(chip))
    return profile


def read_reg_value(chip: str, addr: int) -> int:
    """ROM profile lookup for explicit-chip READ_REG emulation."""
    profile = chips.PROFILES.get(chip)
    if profile and profile.detect_magic and addr == profile.detect_reg:
        return profile.detect_magic
    return rom_profile(chip).get(addr, 0)
