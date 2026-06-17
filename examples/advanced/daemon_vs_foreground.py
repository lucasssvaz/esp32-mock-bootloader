# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Foreground (default) vs daemon mode.

* **foreground** (default for ``mock_bootloader()``) — subprocess in your test
  job; stops when the handle is destroyed or the ``with`` block ends.
* **daemon** — background process, like ``esp32-mock-bootloader start``; call
  ``mock.stop()`` or ``instances.stop()`` when done.

Both expose ``mock.url()`` for uploads.
"""

from __future__ import annotations

from esp32_mock_bootloader import mock_bootloader


def run(chip: str = 'esp32') -> dict[str, object]:
    foreground = mock_bootloader(chip=chip)
    foreground_info = {
        'mode': 'foreground',
        'url': foreground.url(),
        'port': foreground.port(),
    }

    daemon = mock_bootloader(chip=chip, mode='daemon')
    try:
        daemon_info = {
            'mode': 'daemon',
            'url': daemon.url(),
            'port': daemon.port(),
        }
    finally:
        daemon.stop()

    return {
        'foreground': foreground_info,
        'daemon': daemon_info,
        'both_socket_urls': (
            str(foreground_info['url']).startswith('socket://')
            and str(daemon_info['url']).startswith('socket://')
        ),
    }


def main() -> int:
    info = run()
    for label in ('foreground', 'daemon'):
        row = info[label]
        print(f"{row['mode']}: port={row['port']} url={row['url']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
