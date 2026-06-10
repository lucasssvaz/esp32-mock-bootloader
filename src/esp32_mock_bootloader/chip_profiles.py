# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Chip detection metadata derived from esptool ROM target classes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from esptool.loader import ESPLoader
from esptool.targets import CHIP_DEFS, CHIP_LIST

# esptool validation probes this register for many --chip values during connect.
LEGACY_DETECT_REG = int(ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR)

# Shared by RISC-V ROM targets without per-chip magic registers.
_RISCV_DETECT_REG = 0x60000000


@dataclass(frozen=True)
class ChipProfile:
    detect_reg: int
    detect_magic: int
    efuse_base: int
    image_chip_id: int | None
    uses_magic_value: bool

    @property
    def supports_security_info(self) -> bool:
        return self.image_chip_id is not None


def _efuse_base(rom_cls: type) -> int:
    if hasattr(rom_cls, 'EFUSE_BASE'):
        return int(rom_cls.EFUSE_BASE)
    if hasattr(rom_cls, 'EFUSE_RD_REG_BASE'):
        return int(rom_cls.EFUSE_RD_REG_BASE)
    if hasattr(rom_cls, 'ESP_OTP_MAC0'):
        return int(rom_cls.ESP_OTP_MAC0)
    return 0


def _detect_reg_and_magic(rom_cls: type) -> tuple[int, int]:
    uses_magic = bool(getattr(rom_cls, 'USES_MAGIC_VALUE', True))
    if uses_magic:
        reg = int(getattr(
            rom_cls,
            'CHIP_DETECT_MAGIC_REG_ADDR',
            ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR,
        ))
        magic = int(getattr(rom_cls, 'MAGIC_VALUE', 0) or 0)
        return reg, magic

    chip_id = getattr(rom_cls, 'IMAGE_CHIP_ID', None)
    rtccntl = getattr(rom_cls, 'RTCCNTL_BASE_REG', None)
    if rtccntl is not None and chip_id is not None:
        # e.g. ESP32-S3: esptool tests read this region during chip-specific flows.
        return int(rtccntl), int(chip_id)
    return _RISCV_DETECT_REG, 0


def profile_from_rom(name: str, rom_cls: type) -> ChipProfile:
    detect_reg, detect_magic = _detect_reg_and_magic(rom_cls)
    image_chip_id = getattr(rom_cls, 'IMAGE_CHIP_ID', None)
    return ChipProfile(
        detect_reg=detect_reg,
        detect_magic=detect_magic,
        efuse_base=_efuse_base(rom_cls),
        image_chip_id=image_chip_id,
        uses_magic_value=bool(getattr(rom_cls, 'USES_MAGIC_VALUE', True)),
    )


@lru_cache
def get_chip_profiles() -> dict[str, ChipProfile]:
    return {
        name: profile_from_rom(name, rom_cls)
        for name, rom_cls in CHIP_DEFS.items()
    }


def supported_chips() -> tuple[str, ...]:
    return tuple(CHIP_LIST)


@lru_cache
def chips_with_unique_efuse() -> tuple[str, ...]:
    """SoCs whose efuse block address uniquely identifies them in auto mode."""
    profiles = get_chip_profiles()
    counts = Counter(
        profile.efuse_base for profile in profiles.values() if profile.efuse_base
    )
    return tuple(
        name for name, profile in profiles.items()
        if profile.efuse_base and counts[profile.efuse_base] == 1
    )


@lru_cache
def reference_chip() -> str:
    """Representative esptool target for single-chip smoke tests."""
    profiles = get_chip_profiles()
    for name in supported_chips():
        profile = profiles[name]
        if profile.detect_magic and profile.supports_security_info:
            return name
    for name in supported_chips():
        if profiles[name].detect_magic:
            return name
    return supported_chips()[0]


# Eager alias used across the package (rebuilt when esptool is upgraded).
CHIP_PROFILES: dict[str, ChipProfile] = get_chip_profiles()
SUPPORTED_CHIPS: tuple[str, ...] = supported_chips()
