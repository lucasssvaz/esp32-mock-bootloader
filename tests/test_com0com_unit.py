# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""com0com helper unit tests (no setupc required)."""

from __future__ import annotations

from esp32_mock_bootloader.com0com import find_pair_id, parse_pairs


def test_parse_pairs_and_find_pair_id():
    listing = """
CNCA0 PortName=COM18,EmuBR=yes
CNCB0 PortName=COM19,EmuBR=yes
CNCA1 PortName=COM20,EmuBR=yes
CNCB1 PortName=COM21,EmuBR=yes
"""
    pairs = parse_pairs(listing)
    assert (0, 'COM18', 'COM19') in pairs
    assert find_pair_id(listing, 'COM18', 'COM19') == 0
    assert find_pair_id(listing, 'COM20', 'COM21') == 1
    assert find_pair_id(listing, 'COM99', 'COM98') is None
