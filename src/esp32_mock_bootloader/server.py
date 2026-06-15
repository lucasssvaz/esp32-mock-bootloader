# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Mock ESP32 ROM bootloader SLIP protocol server."""

from __future__ import annotations

import hashlib
import json
import os
import select
import socket
import struct
import threading
import time
import zlib
from typing import Callable

from esp32_mock_bootloader import chips
from esp32_mock_bootloader import protocol
from esptool.targets import CHIP_DEFS


class SpiPeripheralMock:
    """Minimal SPI peripheral simulation for esptool flash_id / SFDP probes."""

    SPI_WINDOW = 0x100

    def __init__(
        self,
        spi_base: int | None,
        *,
        usr2_offs: int = 0x24,
        w0_offs: int = 0x80,
    ) -> None:
        self.spi_base = spi_base
        self.usr2_offs = usr2_offs
        self.w0_offs = w0_offs
        self._regs: dict[int, int] = {}
        self._last_command = 0

    def configure_for_chip(self, chip: str) -> None:
        rom = CHIP_DEFS[chip]
        self.spi_base = getattr(rom, 'SPI_REG_BASE', None)
        self.usr2_offs = int(getattr(rom, 'SPI_USR2_OFFS', 0x24))
        self.w0_offs = int(getattr(rom, 'SPI_W0_OFFS', 0x80))
        self._regs.clear()
        self._last_command = 0

    def configure_from_addr(self, addr: int) -> bool:
        """Pick SPI peripheral layout from a register address (auto mode)."""
        if self.spi_base is not None:
            return self.contains(addr)
        for chip, rom in CHIP_DEFS.items():
            base = getattr(rom, 'SPI_REG_BASE', None)
            if base is not None and base <= addr < base + self.SPI_WINDOW:
                self.configure_for_chip(chip)
                return True
        return False

    def contains(self, addr: int) -> bool:
        return (
            self.spi_base is not None
            and self.spi_base <= addr < self.spi_base + self.SPI_WINDOW
        )

    def write_reg(self, addr: int, value: int, mask: int) -> None:
        if not self.contains(addr):
            return
        old = self._regs.get(addr, 0)
        new = (old & ~mask) | (value & mask)
        self._regs[addr] = new
        if self.spi_base is not None:
            rel = addr - self.spi_base
            if rel == self.usr2_offs:
                self._last_command = new & 0xFF
            if rel == 0x00 and (new & protocol.SPI_CMD_USR):
                self._regs[addr] = 0

    def read_reg(self, addr: int) -> int:
        if not self.contains(addr):
            return 0
        rel = addr - self.spi_base
        if rel == 0x00:
            return 0
        if rel == self.w0_offs:
            if self._last_command == 0x9F:
                return protocol.MOCK_FLASH_ID
            if self._last_command == 0x5A:
                return protocol.SFDP_SIGNATURE
        return self._regs.get(addr, 0)


class ChipSession:
    """Per-connection chip detection state."""

    def __init__(self, chip_mode: str) -> None:
        if chip_mode != 'auto' and chip_mode not in chips.PROFILES:
            raise ValueError(f'unknown chip: {chip_mode}')
        self.chip_mode = chip_mode
        self.detected_chip: str | None = chip_mode if chip_mode != 'auto' else None
        self._logged_detection = False
        self.seen_addrs: set[int] = set()
        self.get_security_info_seen = False
        self.stub_active = False
        self.registers: dict[int, int] = {}
        self.spi = SpiPeripheralMock(None)
        if chip_mode != 'auto':
            self.spi.configure_for_chip(chip_mode)

    def note_detection(self, chip: str, source: str) -> None:
        if self.chip_mode != 'auto':
            return
        if self.detected_chip is None:
            self.detected_chip = chip
            self.spi.configure_for_chip(chip)
        if not self._logged_detection:
            print(f'Detected chip: {chip} ({source})', flush=True)
            self._logged_detection = True

    def image_chip_id(self) -> int:
        if self.detected_chip is not None:
            chip_id = chips.PROFILES[self.detected_chip].image_chip_id
            return 0 if chip_id is None else chip_id
        return 0


def _chips_matching_efuse(addr: int) -> list[str]:
    return [
        chip for chip, profile in chips.PROFILES.items()
        if profile.efuse_base <= addr < profile.efuse_base + protocol.EFUSE_WINDOW
    ]


def _chips_matching_detect_reg(addr: int) -> list[str]:
    return [
        chip for chip, profile in chips.PROFILES.items()
        if profile.detect_magic and addr == profile.detect_reg
    ]


def _infer_chip_from_addr(addr: int) -> str | None:
    """Infer SoC from a register address (unique matches only)."""
    if addr == chips.LEGACY_DETECT_REG:
        # esptool probes this for many --chip values during validation.
        return None
    detect_matches = _chips_matching_detect_reg(addr)
    if len(detect_matches) == 1:
        return detect_matches[0]
    efuse_matches = _chips_matching_efuse(addr)
    if len(efuse_matches) == 1:
        return efuse_matches[0]
    return None


def _read_reg_value_for_chip(addr: int, chip: str) -> int:
    profile = chips.PROFILES[chip]
    if profile.detect_magic and addr == profile.detect_reg:
        return profile.detect_magic
    if profile.efuse_base <= addr < profile.efuse_base + protocol.EFUSE_WINDOW:
        return 0
    return 0


class FlashImage:
    """Minimal flash backing store for compressed writes and MD5 verification."""

    def __init__(self) -> None:
        self.data = bytearray(b'\xff' * protocol.FLASH_MOCK_SIZE)
        self._defl_offset = 0
        self._decompressor: zlib.decompressobj | None = None
        self._plain_offset = 0

    def begin_defl(self, payload: bytes) -> None:
        _write_size, _num_blocks, _packet_size, offset = struct.unpack_from('<IIII', payload, 0)
        self._defl_offset = offset
        self._decompressor = zlib.decompressobj()

    def write_defl_block(self, payload: bytes) -> None:
        if len(payload) < 16 or self._decompressor is None:
            return
        compressed_len, _seq, _, _ = struct.unpack_from('<IIII', payload, 0)
        compressed = payload[16:16 + compressed_len]
        try:
            decompressed = self._decompressor.decompress(compressed)
        except zlib.error:
            return
        off = self._defl_offset
        for i, b in enumerate(decompressed):
            pos = off + i
            if pos < len(self.data):
                self.data[pos] = b
        self._defl_offset += len(decompressed)

    def end_defl(self) -> None:
        self._decompressor = None

    def begin_plain(self, payload: bytes) -> None:
        if len(payload) >= 16:
            _size, _blocks, _block_size, offset = struct.unpack_from('<IIII', payload, 0)
            self._plain_offset = offset

    def write_plain_block(self, payload: bytes) -> None:
        if len(payload) < 16:
            return
        data_len, _seq, _, _ = struct.unpack_from('<IIII', payload, 0)
        block = payload[16:16 + data_len]
        off = self._plain_offset
        for i, b in enumerate(block):
            pos = off + i
            if pos < len(self.data):
                self.data[pos] = b
        self._plain_offset += len(block)

    def erase_all(self) -> None:
        self.data[:] = b'\xff' * len(self.data)

    def erase_region(self, offset: int, size: int) -> None:
        end = min(offset + size, len(self.data))
        if offset < end:
            self.data[offset:end] = b'\xff' * (end - offset)

    def read(self, offset: int, length: int) -> bytes:
        end = min(offset + length, len(self.data))
        if offset >= end:
            return b''
        return bytes(self.data[offset:end])

    def md5_stub_response(self, payload: bytes, *, stub: bool = False) -> bytes:
        addr, size, _, _ = struct.unpack_from('<IIII', payload, 0)
        digest = hashlib.md5(bytes(self.data[addr:addr + size])).digest()
        if stub:
            # Flasher stub: 16-byte binary digest plus 2 status bytes.
            resp_data = digest + struct.pack('<BB', 0, 0)
        else:
            # ROM bootloader: 32-byte ASCII hex digest plus 2 status bytes.
            resp_data = digest.hex().encode('ascii') + struct.pack('<BB', 0, 0)
        header = struct.pack('<BBHI', 0x01, protocol.CMD_SPI_FLASH_MD5, len(resp_data), 0)
        return slip_encode(header + resp_data)


class RamImage:
    """In-memory RAM backing store for MEM_BEGIN / MEM_DATA."""

    def __init__(self) -> None:
        self.data = bytearray(protocol.FLASH_MOCK_SIZE)
        self._offset = 0

    def begin(self, payload: bytes) -> None:
        if len(payload) >= 16:
            _size, _blocks, _block_size, offset = struct.unpack_from('<IIII', payload, 0)
            self._offset = offset

    def write_block(self, payload: bytes) -> None:
        if len(payload) < 16:
            return
        data_len, _seq, _, _ = struct.unpack_from('<IIII', payload, 0)
        block = payload[16:16 + data_len]
        off = self._offset
        for i, b in enumerate(block):
            pos = off + i
            if 0 <= pos < len(self.data):
                self.data[pos] = b
        self._offset += len(block)


def checksum_valid(cmd: int, checksum: int, data: bytes) -> bool:
    if cmd not in (protocol.CMD_FLASH_DATA, protocol.CMD_MEM_DATA, protocol.CMD_FLASH_DEFL_DATA):
        return True
    payload = protocol.data_command_payload(data)
    return (checksum & 0xFF) == protocol.data_checksum(payload)


def _checksum_error(session: ChipSession) -> int:
    return protocol.STUB_CHECKSUM_ERROR if session.stub_active else protocol.ROM_CHECKSUM_ERROR


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


def make_response(
    cmd: int,
    value: int = 0,
    status: int = 0,
    error: int = 0,
    *,
    stub: bool = False,
) -> bytes:
    if stub:
        status_bytes = struct.pack('<BB', status, error)
    else:
        status_bytes = struct.pack('<BBBB', status, error, 0, 0)
    size = len(status_bytes)
    header = struct.pack('<BBHI', 0x01, cmd, size, value)
    return slip_encode(header + status_bytes)


def make_sync_response(*, stub: bool = False) -> bytes:
    value = 0 if stub else protocol.ROM_SYNC_VALUE
    return make_response(protocol.CMD_SYNC, value=value, stub=stub)


def handle_read_reg(data: bytes, session: ChipSession) -> bytes:
    if len(data) < 4:
        return make_response(protocol.CMD_READ_REG, value=0, stub=session.stub_active)
    addr = struct.unpack('<I', data[:4])[0]
    session.seen_addrs.add(addr)

    if addr in session.registers:
        return make_response(
            protocol.CMD_READ_REG,
            value=session.registers[addr],
            stub=session.stub_active,
        )

    if session.spi.contains(addr) or session.spi.configure_from_addr(addr):
        return make_response(
            protocol.CMD_READ_REG,
            value=session.spi.read_reg(addr),
            stub=session.stub_active,
        )

    if session.chip_mode != 'auto':
        chip = session.chip_mode
        return make_response(
            protocol.CMD_READ_REG,
            value=_read_reg_value_for_chip(addr, chip),
            stub=session.stub_active,
        )

    if session.detected_chip is not None:
        return make_response(
            protocol.CMD_READ_REG,
            value=_read_reg_value_for_chip(addr, session.detected_chip),
            stub=session.stub_active,
        )

    # esptool --chip <soc> validation probes the legacy ESP32 magic register
    # (0x40001000) for many SoC classes. Never return ESP32 magic until the
    # session has chip-specific evidence (unique detect/efuse addresses).
    if addr == chips.LEGACY_DETECT_REG:
        return make_response(protocol.CMD_READ_REG, value=0, stub=session.stub_active)

    inferred = _infer_chip_from_addr(addr)
    if inferred is not None:
        session.note_detection(inferred, f'READ_REG 0x{addr:08x}')

    if session.detected_chip is not None:
        return make_response(
            protocol.CMD_READ_REG,
            value=_read_reg_value_for_chip(addr, session.detected_chip),
            stub=session.stub_active,
        )

    for chip, profile in chips.PROFILES.items():
        if profile.detect_magic and addr == profile.detect_reg:
            session.note_detection(chip, f'READ_REG 0x{addr:08x}')
            return make_response(
                protocol.CMD_READ_REG,
                value=profile.detect_magic,
                stub=session.stub_active,
            )

    efuse_matches = _chips_matching_efuse(addr)
    if len(efuse_matches) == 1:
        session.note_detection(efuse_matches[0], f'READ_REG efuse 0x{addr:08x}')
    elif len(efuse_matches) > 1 and session.detected_chip is None:
        # Shared efuse window (e.g. early C3 reads) — respond without guessing.
        pass

    if efuse_matches:
        return make_response(protocol.CMD_READ_REG, value=0, stub=session.stub_active)

    return make_response(protocol.CMD_READ_REG, value=0, stub=session.stub_active)


def handle_write_reg(data: bytes, session: ChipSession) -> bytes:
    if len(data) < 16:
        return make_response(
            protocol.CMD_WRITE_REG,
            status=1,
            error=protocol.ROM_INVALID_MESSAGE,
            stub=session.stub_active,
        )
    addr, value, mask, _delay = struct.unpack_from('<IIII', data, 0)
    if session.spi.contains(addr) or session.spi.configure_from_addr(addr):
        session.spi.write_reg(addr, value, mask)
    else:
        old = session.registers.get(addr, 0)
        session.registers[addr] = (old & ~mask) | (value & mask)
    return make_response(protocol.CMD_WRITE_REG, stub=session.stub_active)


def handle_read_flash_slow(data: bytes, flash: FlashImage, session: ChipSession) -> bytes:
    if session.stub_active or len(data) < 8:
        return make_response(
            protocol.CMD_READ_FLASH_SLOW,
            status=1,
            error=protocol.ROM_INVALID_MESSAGE,
            stub=session.stub_active,
        )
    offset, block_len = struct.unpack_from('<II', data, 0)
    block = flash.read(offset, block_len)
    if len(block) < protocol.READ_FLASH_SLOW_BLOCK_LEN:
        block = block + b'\xff' * (protocol.READ_FLASH_SLOW_BLOCK_LEN - len(block))
    resp_data = block[:protocol.READ_FLASH_SLOW_BLOCK_LEN] + struct.pack('<BBBB', 0, 0, 0, 0)
    header = struct.pack('<BBHI', 0x01, protocol.CMD_READ_FLASH_SLOW, len(resp_data), 0)
    return slip_encode(header + resp_data)


def _consume_slip_frame(buf: bytearray) -> bytes | None:
    """Return the first decoded SLIP payload and remove it from buf."""
    raw = bytes(buf)
    end = raw.find(bytes([protocol.SLIP_END]), 1)
    if end < 0:
        return None
    frames = slip_decode_frames(raw[:end + 1])
    if not frames:
        return None
    del buf[:end + 1]
    return frames[0]


def _recv_slip_payload(
    conn: BootloaderConnection,
    stop_event: threading.Event,
    buf: bytearray,
) -> bytes | None:
    while not stop_event.is_set():
        payload = _consume_slip_frame(buf)
        if payload is not None:
            return payload
        try:
            chunk = conn.recv(4096)
        except (socket.timeout, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return None


def perform_read_flash_stream(
    conn: BootloaderConnection,
    stop_event: threading.Event,
    flash: FlashImage,
    offset: int,
    length: int,
    packet_size: int,
    max_in_flight: int,
    buf: bytearray,
) -> None:
    """Stub READ_FLASH: stream SLIP data packets, read cumulative acks, send MD5."""
    if packet_size <= 0 or max_in_flight <= 0:
        return
    sent = 0
    pos = offset
    in_flight = 0

    while sent < length and not stop_event.is_set():
        while in_flight < max_in_flight and sent < length:
            chunk_len = min(packet_size, length - sent)
            chunk = flash.read(pos, chunk_len)
            conn.sendall(slip_encode(chunk))
            pos += chunk_len
            sent += chunk_len
            in_flight += 1

        while not stop_event.is_set():
            ack_payload = _recv_slip_payload(conn, stop_event, buf)
            if ack_payload is None or len(ack_payload) < 4:
                return
            ack_total = struct.unpack('<I', ack_payload[:4])[0]
            if ack_total >= sent:
                break
        else:
            return
        in_flight = 0

    digest = hashlib.md5(flash.read(offset, length)).digest()
    conn.sendall(slip_encode(digest))


def _active_chip(session: ChipSession) -> str | None:
    if session.chip_mode != 'auto':
        return session.chip_mode
    return session.detected_chip


def handle_get_security_info(session: ChipSession) -> bytes:
    session.get_security_info_seen = True
    active = _active_chip(session)
    if active is not None and not chips.PROFILES[active].supports_security_info:
        return make_response(
            protocol.CMD_GET_SECURITY_INFO,
            status=1,
            error=protocol.ROM_INVALID_MESSAGE,
            stub=session.stub_active,
        )
    if session.chip_mode != 'auto' or session.detected_chip is not None:
        chip_id = session.image_chip_id()
        payload = struct.pack('<IBBBBBBBBII', 0, 0, 0, 0, 0, 0, 0, 0, 0, chip_id, 0)
        header = struct.pack('<BBHI', 0x01, protocol.CMD_GET_SECURITY_INFO, len(payload), 0)
        return slip_encode(header + payload)

    # Before chip-specific READ_REG traffic, return ROM error so esptool does not
    # treat the session as ESP32 (chip_id 0) during connect validation.
    return make_response(
        protocol.CMD_GET_SECURITY_INFO,
        status=1,
        error=protocol.ROM_INVALID_MESSAGE,
        stub=session.stub_active,
    )


class BootloaderConnection:
    """Socket, PTY master fd, or pyserial COM port transport."""

    def __init__(
        self,
        sock: socket.socket | None = None,
        master_fd: int | None = None,
        serial_port: object | None = None,
    ) -> None:
        modes = sum(mode is not None for mode in (sock, master_fd, serial_port))
        if modes != 1:
            raise ValueError('exactly one of sock, master_fd, or serial_port is required')
        self._sock = sock
        self._fd = master_fd
        self._serial = serial_port
        self._timeout = 1.0

    def set_timeout(self, timeout: float) -> None:
        self._timeout = timeout
        if self._sock is not None:
            self._sock.settimeout(timeout)
        if self._serial is not None:
            self._serial.timeout = timeout

    def recv(self, size: int) -> bytes:
        if self._sock is not None:
            return self._sock.recv(size)
        if self._serial is not None:
            data = self._serial.read(size)
            if not data:
                raise socket.timeout()
            return data
        ready, _, _ = select.select([self._fd], [], [], self._timeout)
        if not ready:
            raise socket.timeout()
        return os.read(self._fd, size)

    def sendall(self, data: bytes) -> None:
        if self._sock is not None:
            self._sock.sendall(data)
        elif self._serial is not None:
            self._serial.write(data)
        else:
            os.write(self._fd, data)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
        if self._serial is not None:
            self._serial.close()


def handle_client(
    conn: BootloaderConnection,
    stop_event: threading.Event,
    chip_mode: str = 'auto',
    on_detected: Callable[[str | None], None] | None = None,
) -> None:
    session = ChipSession(chip_mode)
    if on_detected and session.detected_chip:
        on_detected(session.detected_chip)

    buf = bytearray()
    flash = FlashImage()
    ram = RamImage()
    flash_ended = False
    idle_since_flash_end = 0.0
    try:
        conn.set_timeout(1.0)
        while not stop_event.is_set():
            if flash_ended:
                if idle_since_flash_end > 3.0:
                    break
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                if flash_ended:
                    idle_since_flash_end += 1.0
                continue
            except OSError:
                break
            if not chunk:
                break
            idle_since_flash_end = 0.0
            buf.extend(chunk)

            frames = slip_decode_frames(bytes(buf))
            if not frames:
                continue
            last_end = buf.rfind(protocol.SLIP_END)
            if last_end >= 0 and last_end < len(buf) - 1:
                buf = buf[last_end + 1:]
            else:
                buf.clear()

            for frame in frames:
                if len(frame) < 8:
                    continue

                direction = frame[0]
                if direction != 0x00:
                    continue

                cmd = frame[1]
                size = struct.unpack_from('<H', frame, 2)[0]
                checksum = struct.unpack_from('<I', frame, 4)[0]
                data = frame[8:8 + size] if len(frame) >= 8 + size else frame[8:]

                response = b''
                stub = session.stub_active
                if cmd == protocol.CMD_SYNC:
                    for _ in range(8):
                        response += make_sync_response(stub=stub)
                elif cmd == protocol.CMD_READ_REG:
                    response = handle_read_reg(data, session)
                    if on_detected:
                        on_detected(session.detected_chip)
                elif cmd == protocol.CMD_GET_SECURITY_INFO:
                    response = handle_get_security_info(session)
                elif cmd == protocol.CMD_FLASH_BEGIN:
                    flash.begin_plain(data)
                    response = make_response(cmd, stub=stub)
                elif cmd == protocol.CMD_FLASH_DATA:
                    if not checksum_valid(cmd, checksum, data):
                        response = make_response(
                            cmd, status=1, error=_checksum_error(session), stub=stub,
                        )
                    else:
                        flash.write_plain_block(data)
                        response = make_response(cmd, stub=stub)
                elif cmd == protocol.CMD_FLASH_END:
                    response = make_response(cmd, stub=stub)
                    flash_ended = True
                elif cmd == protocol.CMD_FLASH_DEFL_BEGIN:
                    flash.begin_defl(data)
                    response = make_response(cmd, stub=stub)
                elif cmd == protocol.CMD_FLASH_DEFL_DATA:
                    if not checksum_valid(cmd, checksum, data):
                        response = make_response(
                            cmd, status=1, error=_checksum_error(session), stub=stub,
                        )
                    else:
                        flash.write_defl_block(data)
                        response = make_response(cmd, stub=stub)
                elif cmd == protocol.CMD_FLASH_DEFL_END:
                    flash.end_defl()
                    response = make_response(cmd, stub=stub)
                    flash_ended = True
                elif cmd == protocol.CMD_MEM_BEGIN:
                    ram.begin(data)
                    response = make_response(cmd, stub=stub)
                elif cmd == protocol.CMD_MEM_DATA:
                    if not checksum_valid(cmd, checksum, data):
                        response = make_response(
                            cmd, status=1, error=_checksum_error(session), stub=stub,
                        )
                    else:
                        ram.write_block(data)
                        response = make_response(cmd, stub=stub)
                elif cmd == protocol.CMD_MEM_END:
                    activate_stub = False
                    if len(data) >= 8:
                        stay_in_loader, entrypoint = struct.unpack_from('<II', data, 0)
                        if stay_in_loader == 0 and entrypoint != 0:
                            session.stub_active = True
                            activate_stub = True
                    response = make_response(cmd, stub=session.stub_active)
                    if activate_stub:
                        ohai = bytes([protocol.SLIP_END, 0x4F, 0x48, 0x41, 0x49, protocol.SLIP_END])
                        response += ohai
                elif cmd == protocol.CMD_WRITE_REG:
                    response = handle_write_reg(data, session)
                elif cmd == protocol.CMD_SPI_SET_PARAMS:
                    response = make_response(cmd, stub=stub)
                elif cmd == protocol.CMD_SPI_ATTACH:
                    response = make_response(cmd, stub=stub)
                elif cmd == protocol.CMD_CHANGE_BAUDRATE:
                    response = make_response(cmd, stub=stub)
                elif cmd == protocol.CMD_SPI_FLASH_MD5:
                    response = flash.md5_stub_response(data, stub=session.stub_active)
                elif cmd == protocol.CMD_READ_FLASH_SLOW:
                    response = handle_read_flash_slow(data, flash, session)
                elif cmd == protocol.CMD_ERASE_FLASH:
                    if session.stub_active:
                        flash.erase_all()
                        response = make_response(cmd, stub=True)
                    else:
                        response = make_response(
                            cmd, status=1, error=protocol.ROM_INVALID_MESSAGE, stub=False,
                        )
                elif cmd == protocol.CMD_ERASE_REGION:
                    if session.stub_active and len(data) >= 8:
                        offset, erase_size = struct.unpack_from('<II', data, 0)
                        flash.erase_region(offset, erase_size)
                        response = make_response(cmd, stub=True)
                    else:
                        response = make_response(
                            cmd,
                            status=1,
                            error=protocol.ROM_INVALID_MESSAGE if not session.stub_active else protocol.STUB_UNIMPLEMENTED,
                            stub=session.stub_active,
                        )
                elif cmd == protocol.CMD_READ_FLASH:
                    if session.stub_active and len(data) >= 16:
                        offset, length, packet_size, max_in_flight = struct.unpack_from(
                            '<IIII', data, 0,
                        )
                        try:
                            conn.sendall(make_response(cmd, stub=True))
                            perform_read_flash_stream(
                                conn,
                                stop_event,
                                flash,
                                offset,
                                length,
                                packet_size,
                                max_in_flight,
                                buf,
                            )
                        except OSError:
                            return
                        response = b''
                    else:
                        response = make_response(
                            cmd,
                            status=1,
                            error=protocol.ROM_INVALID_MESSAGE if not session.stub_active else protocol.STUB_UNIMPLEMENTED,
                            stub=session.stub_active,
                        )
                elif cmd == protocol.CMD_RUN_USER_CODE:
                    if session.stub_active:
                        flash_ended = True
                        response = b''
                    else:
                        response = make_response(
                            cmd, status=1, error=protocol.ROM_INVALID_MESSAGE, stub=False,
                        )
                else:
                    response = make_response(
                        cmd,
                        status=1,
                        error=protocol.STUB_UNIMPLEMENTED if session.stub_active else protocol.ROM_INVALID_MESSAGE,
                        stub=session.stub_active,
                    )

                if response:
                    try:
                        conn.sendall(response)
                    except OSError:
                        return
    finally:
        if on_detected:
            on_detected(session.detected_chip)
        conn.close()


def _make_on_detected(state_file: str | None) -> Callable[[str | None], None] | None:
    if not state_file:
        return None

    lock = threading.Lock()

    def _update(detected: str | None) -> None:
        if detected is None:
            return
        with lock:
            try:
                with open(state_file, encoding='utf-8') as f:
                    state = json.load(f)
            except (OSError, json.JSONDecodeError):
                return
            if state.get('detected_chip') == detected:
                return
            state['detected_chip'] = detected
            try:
                with open(state_file, 'w', encoding='utf-8') as f:
                    json.dump(state, f, indent=2)
                    f.write('\n')
            except OSError:
                pass

    return _update


def _tcp_listen_loop(
    srv: socket.socket,
    timeout: float | None,
    chip_mode: str,
    on_detected: Callable[[str | None], None] | None,
    label: str,
) -> None:
    stop_event = threading.Event()
    srv.settimeout(2.0)
    print(label, flush=True)
    start = time.time()
    try:
        while not stop_event.is_set():
            if timeout is not None and time.time() - start > timeout:
                print('Timeout reached, shutting down', flush=True)
                break
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            print(f'Connection from {addr}', flush=True)
            handle_client(BootloaderConnection(sock=conn), stop_event, chip_mode, on_detected)
            print('Client disconnected', flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
    print('Mock bootloader stopped', flush=True)


def _write_serial_endpoint(path_file: str | None, endpoint: str) -> None:
    if path_file:
        with open(path_file, 'w', encoding='ascii') as f:
            f.write(endpoint)
    else:
        print(endpoint, flush=True)


def run_server(
    port: int,
    timeout: float | None = None,
    chip_mode: str = 'auto',
    bind: str = '127.0.0.1',
    state_file: str | None = None,
) -> None:
    on_detected = _make_on_detected(state_file)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(1)
    _tcp_listen_loop(
        srv, timeout, chip_mode, on_detected,
        f'Mock bootloader (chip={chip_mode}) listening on {bind}:{port}',
    )


def _run_unix_pty_server(
    timeout: float | None,
    pty_path_file: str | None,
    chip_mode: str,
    on_detected: Callable[[str | None], None] | None,
) -> None:
    import pty

    master, slave = pty.openpty()
    slave_path = os.ttyname(slave)
    _write_serial_endpoint(pty_path_file, slave_path)
    print(f'Mock bootloader (chip={chip_mode}) on PTY {slave_path}', flush=True)
    start = time.time()
    try:
        while timeout is None or time.time() - start < timeout:
            stop_event = threading.Event()
            handle_client(
                BootloaderConnection(master_fd=master),
                stop_event,
                chip_mode,
                on_detected,
            )
    finally:
        os.close(master)
        os.close(slave)
        if timeout is not None and time.time() - start >= timeout:
            print('Timeout reached, shutting down', flush=True)
    print('Mock bootloader stopped', flush=True)


def resolve_com_ports(
    com_port: str | None = None,
    com_peer: str | None = None,
) -> tuple[str, str] | None:
    """Return (server, client) COM names from args or ESP32_MOCK_COM_* env vars."""
    server = com_port or os.environ.get('ESP32_MOCK_COM_PORT')
    client = com_peer or os.environ.get('ESP32_MOCK_COM_PEER')
    if server and client:
        return server, client
    if server or client:
        raise ValueError('both com port and com peer are required for COM mode')
    return None


def _run_com_server(
    com_port: str,
    com_peer: str,
    timeout: float | None,
    pty_path_file: str | None,
    chip_mode: str,
    on_detected: Callable[[str | None], None] | None,
) -> None:
    """Serve ROM protocol on a COM port (e.g. com0com null-modem pair on Windows)."""
    import serial

    _write_serial_endpoint(pty_path_file, com_peer)
    print(
        f'Mock bootloader (chip={chip_mode}) on {com_port} (client: {com_peer})',
        flush=True,
    )
    ser = serial.Serial(com_port, baudrate=115200, timeout=1.0)
    start = time.time()
    try:
        while timeout is None or time.time() - start < timeout:
            stop_event = threading.Event()
            handle_client(
                BootloaderConnection(serial_port=ser),
                stop_event,
                chip_mode,
                on_detected,
            )
    finally:
        ser.close()
        if timeout is not None and time.time() - start >= timeout:
            print('Timeout reached, shutting down', flush=True)
    print('Mock bootloader stopped', flush=True)


def _run_windows_pty_server(
    timeout: float | None,
    pty_path_file: str | None,
    chip_mode: str,
    on_detected: Callable[[str | None], None] | None,
) -> None:
    """CI-friendly fallback when no com0com pair is configured."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    endpoint = f'socket://127.0.0.1:{port}'
    _write_serial_endpoint(pty_path_file, endpoint)
    _tcp_listen_loop(
        srv, timeout, chip_mode, on_detected,
        f'Mock bootloader (chip={chip_mode}) on {endpoint} (Windows socket fallback)',
    )


def run_pty_server(
    timeout: float | None,
    pty_path_file: str | None,
    chip_mode: str = 'auto',
    state_file: str | None = None,
    com_port: str | None = None,
    com_peer: str | None = None,
) -> None:
    on_detected = _make_on_detected(state_file)
    com_pair = resolve_com_ports(com_port, com_peer)
    if com_pair is not None:
        _run_com_server(*com_pair, timeout, pty_path_file, chip_mode, on_detected)
    elif os.name == 'nt':
        _run_windows_pty_server(timeout, pty_path_file, chip_mode, on_detected)
    else:
        _run_unix_pty_server(timeout, pty_path_file, chip_mode, on_detected)
