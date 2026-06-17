# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Connect with a raw TCP socket via ``advanced.transport``.

``protocol.connect(mock)`` is the usual path. Use ``transport.connect`` when you
already have a port number and want the same socket layer without a Client wrapper.
"""

from __future__ import annotations

from esp32_mock_bootloader import chips, mock_bootloader
from esp32_mock_bootloader.advanced import protocol_client, transport


def run(chip: str = 'esp32') -> dict[str, object]:
    profile = chips.PROFILES[chip]
    mock = mock_bootloader(chip=chip)
    sock = transport.connect(mock.port())
    try:
        protocol_client.send_sync(sock)
        if not profile.detect_magic:
            return {'chip': chip, 'skipped': True}
        value = protocol_client.read_reg_value(sock, profile.detect_reg)
        return {
            'chip': chip,
            'port': mock.port(),
            'skipped': False,
            'matches': value == profile.detect_magic,
        }
    finally:
        sock.close()


def main() -> int:
    info = run()
    if info.get('skipped'):
        print(f'{info["chip"]}: no detect register')
        return 0
    print(f'{info["chip"]} port {info["port"]}: detect register {"ok" if info["matches"] else "fail"}')
    return 0 if info['matches'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
