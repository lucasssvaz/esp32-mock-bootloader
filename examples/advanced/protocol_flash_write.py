# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Program flash with ROM protocol commands (custom upload client).

If you are not using esptool, talk SLIP directly via
``esp32_mock_bootloader.advanced.protocol.connect()``.
"""

from __future__ import annotations

import struct

from esp32_mock_bootloader import constants, mock_bootloader, protocol
from esp32_mock_bootloader.advanced import protocol as protocol_api


def run(chip: str = 'esp32', length: int = 0x100) -> dict[str, object]:
    offset = constants.FLASH_APP_OFFSET
    payload = b'\x5A' * length
    mock = mock_bootloader(chip=chip)
    client = protocol_api.connect(mock)
    client.sync()
    client.send_command(
        protocol.CMD_FLASH_BEGIN,
        struct.pack('<IIII', length, 1, length, offset),
    )
    client.send_command(
        protocol.CMD_FLASH_DATA,
        struct.pack('<IIII', length, 0, 0, 0) + payload,
    )
    client.send_command(protocol.CMD_FLASH_END, struct.pack('<I', 0))
    client.activate_stub()
    data, _digest = client.stub_read_flash(offset, length)
    return {
        'chip': chip,
        'offset': offset,
        'length': length,
        'bytes_match': data == payload,
    }


def main() -> int:
    info = run()
    print(
        f'{info["chip"]}: wrote {info["length"]} B @ 0x{info["offset"]:X} '
        f"-> readback {'ok' if info['bytes_match'] else 'mismatch'}",
    )
    return 0 if info['bytes_match'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
