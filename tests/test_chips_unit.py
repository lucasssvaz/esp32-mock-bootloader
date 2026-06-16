# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Chip profile helper unit tests."""

from __future__ import annotations

from esp32_mock_bootloader import chips


def test_reference_chip_returns_supported():
    name = chips.reference_chip()
    assert name in chips.SUPPORTED
    profile = chips.PROFILES[name]
    assert profile.detect_magic or name in chips.SUPPORTED


def test_reference_chip_fallback_to_magic_only(monkeypatch):
    fake_profiles = {
        'plain': chips.ChipProfile(1, 0, 0, None, True),
        'magic': chips.ChipProfile(1, 0x12345678, 0, 1, True),
    }
    monkeypatch.setattr(chips, 'get_chip_profiles', lambda: fake_profiles)
    monkeypatch.setattr(chips, 'supported_chips', lambda: ('plain', 'magic'))
    chips.reference_chip.cache_clear()
    assert chips.reference_chip() == 'magic'


def test_reference_chip_fallback_to_first_supported(monkeypatch):
    fake_profiles = {
        'only': chips.ChipProfile(1, 0, 0, None, False),
    }
    monkeypatch.setattr(chips, 'get_chip_profiles', lambda: fake_profiles)
    monkeypatch.setattr(chips, 'supported_chips', lambda: ('only',))
    chips.reference_chip.cache_clear()
    assert chips.reference_chip() == 'only'


def test_efuse_base_esp_otp_path():
    class FakeRom:
        ESP_OTP_MAC0 = 0x3FF00050

    assert chips._efuse_base(FakeRom) == 0x3FF00050
