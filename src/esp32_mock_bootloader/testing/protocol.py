# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""SLIP protocol helpers for talking to the mock bootloader."""

from __future__ import annotations

import socket
import struct

from esp32_mock_bootloader import protocol
from esp32_mock_bootloader.testing import constants
from esp32_mock_bootloader.testing.server import SerialLink


def slip_encode(data: bytes) -> bytes:
    out = bytearray([protocol.SLIP_END])
    for b in data:
        if b == protocol.SLIP_END:
            out.extend([protocol.SLIP_ESC, protocol.SLIP_ESC_END])
        elif b == protocol.SLIP_ESC:
            out.extend([protocol.SLIP_ESC, protocol.SLIP_ESC_DD])
        else:
            out.append(b)
    out.append(protocol.SLIP_END)
    return bytes(out)


def slip_decode_frames(raw: bytes) -> list[bytes]:
    frames = []
    current = bytearray()
    in_frame = False
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == protocol.SLIP_END:
            if in_frame and current:
                frames.append(bytes(current))
                current = bytearray()
            in_frame = True
        elif in_frame:
            if b == protocol.SLIP_ESC:
                i += 1
                if i < len(raw):
                    if raw[i] == protocol.SLIP_ESC_END:
                        current.append(protocol.SLIP_END)
                    elif raw[i] == protocol.SLIP_ESC_DD:
                        current.append(protocol.SLIP_ESC)
                    else:
                        current.append(raw[i])
            else:
                current.append(b)
        i += 1
    return frames


def make_command(cmd: int, data: bytes = b'', checksum: int | None = None) -> bytes:
    if checksum is None:
        if cmd in protocol.DATA_CHECKSUM_CMDS:
            checksum = protocol.data_checksum(protocol.data_command_payload(data))
        else:
            checksum = 0
    header = struct.pack('<BBHI', 0x00, cmd, len(data), checksum)
    return slip_encode(header + data)


def parse_response(frame: bytes) -> tuple[int, int, int, int, bytes]:
    if len(frame) < 8:
        return (0, 0, 0, 0, b'')
    direction = frame[0]
    cmd = frame[1]
    size = struct.unpack_from('<H', frame, 2)[0]
    value = struct.unpack_from('<I', frame, 4)[0]
    data = frame[8:]
    return (direction, cmd, size, value, data)


def send_and_receive(
    sock: socket.socket | SerialLink, data: bytes, recv_size: int = 4096,
) -> bytes:
    sock.sendall(data)
    return _recv_until_idle(sock, recv_size)


def _recv_until_idle(
    sock: socket.socket | SerialLink,
    recv_size: int,
    *,
    idle_sec: float = 0.02,
) -> bytes:
    """Accumulate reads until the link is quiet (handles multi-frame replies)."""
    restore_timeout: float | None = None
    if isinstance(sock, socket.socket):
        restore_timeout = sock.gettimeout()
        sock.settimeout(idle_sec)
    chunks: list[bytes] = []
    try:
        while True:
            try:
                chunk = sock.recv(recv_size)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        if isinstance(sock, socket.socket):
            sock.settimeout(restore_timeout)
    return b''.join(chunks)


def send_sync(sock: socket.socket | SerialLink) -> None:
    send_and_receive(sock, make_command(protocol.CMD_SYNC, constants.SYNC_PAYLOAD), 8192)


def read_reg_value(sock: socket.socket | SerialLink, addr: int) -> int | None:
    raw = send_and_receive(sock, make_command(protocol.CMD_READ_REG, struct.pack('<I', addr)))
    frames = slip_decode_frames(raw)
    if not frames:
        return None
    return parse_response(frames[0])[3]


def minimal_plain_flash(sock: socket.socket | SerialLink) -> bool:
    """Minimal FLASH_BEGIN + one DATA block + END at FLASH_APP_OFFSET."""
    send_sync(sock)
    block_size = 0x100
    fb = make_command(
        protocol.CMD_FLASH_BEGIN,
        struct.pack('<IIII', block_size, 1, block_size, constants.FLASH_APP_OFFSET),
    )
    fd = make_command(
        protocol.CMD_FLASH_DATA,
        struct.pack('<IIII', block_size, 0, 0, 0) + (b'\xAB' * block_size),
    )
    fe = make_command(protocol.CMD_FLASH_END, struct.pack('<I', constants.STAY_IN_LOADER))
    raw = send_and_receive(sock, fb + fd + fe, 4096)
    frames = slip_decode_frames(raw)
    if len(frames) < 3:
        return False
    cmds = [parse_response(frame)[1] for frame in frames]
    return cmds == [protocol.CMD_FLASH_BEGIN, protocol.CMD_FLASH_DATA, protocol.CMD_FLASH_END]


def activate_stub(sock: socket.socket | SerialLink) -> None:
    """Upload a minimal stub session (MEM_BEGIN/DATA/END + OHAI)."""
    send_sync(sock)
    send_and_receive(
        sock,
        make_command(
            protocol.CMD_MEM_BEGIN,
            struct.pack(
                '<IIII',
                constants.STUB_UPLOAD_BLOCK,
                1,
                constants.STUB_UPLOAD_BLOCK,
                constants.STUB_IRAM_ENTRY,
            ),
        ),
    )
    payload = (
        struct.pack('<IIII', constants.STUB_UPLOAD_BLOCK, 0, 0, 0)
        + (b'\x00' * constants.STUB_UPLOAD_BLOCK)
    )
    send_and_receive(sock, make_command(protocol.CMD_MEM_DATA, payload))
    send_and_receive(
        sock,
        make_command(
            protocol.CMD_MEM_END,
            struct.pack('<II', constants.RUN_ENTRYPOINT, constants.STUB_IRAM_ENTRY),
        ),
    )


def stub_read_flash(
    sock: socket.socket | SerialLink,
    offset: int,
    length: int,
    *,
    packet_size: int | None = None,
    max_in_flight: int = 4,
) -> tuple[bytes, bytes]:
    """Client side of stub READ_FLASH (data packets, cumulative acks, MD5)."""
    if packet_size is None:
        packet_size = protocol.FLASH_SECTOR_SIZE
    sock.sendall(
        make_command(
            protocol.CMD_READ_FLASH,
            struct.pack('<IIII', offset, length, packet_size, max_in_flight),
        ),
    )
    buf = bytearray()
    data = b''

    def next_payload() -> bytes:
        nonlocal buf
        while True:
            frames = slip_decode_frames(bytes(buf))
            if frames:
                raw = bytes(buf)
                end = raw.find(bytes([protocol.SLIP_END]), 1)
                if end >= 0:
                    del buf[:end + 1]
                else:
                    buf.clear()
                return frames[0]
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError('connection closed during READ_FLASH')
            buf.extend(chunk)

    while True:
        frame = next_payload()
        if frame[0] == constants.SLIP_RESPONSE_DIRECTION:
            break

    while len(data) < length:
        data += next_payload()
        sock.sendall(slip_encode(struct.pack('<I', len(data))))

    digest = next_payload()
    return data, digest


__all__ = [
    'activate_stub',
    'make_command',
    'minimal_plain_flash',
    'parse_response',
    'read_reg_value',
    'send_and_receive',
    'send_sync',
    'slip_decode_frames',
    'slip_encode',
    'stub_read_flash',
]
