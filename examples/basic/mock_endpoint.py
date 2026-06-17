# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Start a mock and read its upload endpoint.

The mock is passive: it listens on a TCP port and speaks the ROM bootloader
protocol. Your job is to take ``mock.url()`` and pass it to an upload client
(esptool, arduino-cli, etc.).

Foreground mode (the default) stops automatically when ``mock`` is destroyed.
"""

from __future__ import annotations

from esp32_mock_bootloader import mock_bootloader


def run(chip: str = 'esp32') -> dict[str, object]:
    mock = mock_bootloader(chip=chip)
    return {
        'chip': mock.chip,
        'port': mock.port(),
        'url': mock.url(),
        'running': mock.status()['running'],
    }


def main() -> int:
    info = run()
    print(f"chip={info['chip']}  port={info['port']}  url={info['url']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
