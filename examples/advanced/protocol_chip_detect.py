# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Probe chip identity over the ROM protocol (no esptool).

Use this when you build a custom upload client and need to confirm the mock
presents the expected SoC before sending firmware.
"""

from __future__ import annotations

from esp32_mock_bootloader import chips, mock_bootloader
from esp32_mock_bootloader.advanced import protocol


def run(chip: str = 'esp32') -> dict[str, object]:
    profile = chips.PROFILES[chip]
    mock = mock_bootloader(chip=chip)
    client = protocol.connect(mock)
    if not profile.detect_magic:
        return {'chip': chip, 'skipped': True}
    value = client.read_reg(profile.detect_reg)
    return {
        'chip': chip,
        'skipped': False,
        'register': profile.detect_reg,
        'value': value,
        'expected': profile.detect_magic,
        'matches': value == profile.detect_magic,
    }


def main() -> int:
    info = run()
    if info.get('skipped'):
        print(f'{info["chip"]}: no ROM detect register')
        return 0
    print(
        f'{info["chip"]}: READ_REG 0x{info["register"]:08X} '
        f'= 0x{info["value"]:08X}',
    )
    return 0 if info['matches'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
