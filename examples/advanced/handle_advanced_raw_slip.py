# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Send raw SLIP packets through ``mock.advanced``.

Lower level than ``protocol.connect()``: build frames with ``protocol_client`` and
push bytes on the transport yourself.
"""

from __future__ import annotations

from esp32_mock_bootloader import constants, mock_bootloader, protocol
from esp32_mock_bootloader.advanced import protocol_client


def run(chip: str = 'esp32') -> dict[str, object]:
    mock = mock_bootloader(chip=chip)
    sync_packet = protocol_client.make_command(
        protocol.CMD_SYNC,
        constants.SYNC_PAYLOAD,
    )
    raw = mock.advanced.send_raw(sync_packet)
    frames = protocol_client.slip_decode_frames(raw)
    if not frames:
        return {'ok': False, 'frame_count': 0}
    direction, cmd, _status, value, _data = protocol_client.parse_response(frames[0])
    return {
        'ok': direction == 0x01 and cmd == protocol.CMD_SYNC and value != 0,
        'frame_count': len(frames),
    }


def main() -> int:
    info = run()
    print(f'SYNC response: {"ok" if info["ok"] else "failed"} ({info["frame_count"]} frame(s))')
    return 0 if info['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
