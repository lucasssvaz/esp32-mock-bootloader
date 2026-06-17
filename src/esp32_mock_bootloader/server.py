# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Mock ESP32 ROM bootloader SLIP protocol server."""

from __future__ import annotations

import errno
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
from esp32_mock_bootloader import registers
from esptool.targets import CHIP_DEFS

_CLIENT_RECV_TIMEOUT = 0.05
_FLASH_END_IDLE_SEC = 3.0


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
        chip = _profile_chip(self)
        if chip is None:
            return 0
        chip_id = chips.PROFILES[chip].image_chip_id
        return 0 if chip_id is None else chip_id


def _profile_chip(session: ChipSession) -> str | None:
    if session.chip_mode != 'auto':
        return session.chip_mode
    if session.detected_chip is not None:
        return session.detected_chip
    return None


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
    return registers.read_reg_value(chip, addr)


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

    def reset(self) -> None:
        self.data[:] = b'\xff' * len(self.data)
        self._defl_offset = 0
        self._decompressor = None
        self._plain_offset = 0

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


_persistent_flash: FlashImage | None = None


def get_flash_image() -> FlashImage:
    """Return the process-wide mock SPI flash (created on first use)."""
    global _persistent_flash
    if _persistent_flash is None:
        _persistent_flash = FlashImage()
    return _persistent_flash


def reset_flash_image() -> None:
    """Erase the process-wide mock flash (for tests)."""
    global _persistent_flash
    if _persistent_flash is not None:
        _persistent_flash.reset()
    else:
        _persistent_flash = None


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
    if inferred is not None and session.detected_chip is None:
        session.note_detection(inferred, f'READ_REG 0x{addr:08x}')
        if _chips_matching_efuse(addr):
            return make_response(protocol.CMD_READ_REG, value=0, stub=session.stub_active)
    elif inferred is not None:
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
    return _profile_chip(session)


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
    if active is not None:
        chip_id = session.image_chip_id()
        payload = struct.pack(
            '<IBBBBBBBBII', 0, 0, 0, 0, 0, 0, 0, 0, 0, chip_id, 0,
        ) + b'\x00\x00'
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


class PtyMasterTransport:
    """Unix PTY master-side I/O for clients that open a device path.

    The kernel reports hangup (EOF on the master) only when every slave
    reference is closed.  If this process keeps its slave fd open, a client
    disconnect is invisible.  When *track_disconnect* is set (for
    ``--exit-on-disconnect``), the server releases its slave fd after the
    first byte from the client so a later client close becomes a real EOF.
    """

    def __init__(
        self,
        master_fd: int,
        slave_fd: int,
        *,
        track_disconnect: bool = False,
    ) -> None:
        self._master = master_fd
        self._slave = slave_fd
        self._track_disconnect = track_disconnect
        self._slave_released = False
        self._saw_client_data = False
        self._timeout = _CLIENT_RECV_TIMEOUT

    @classmethod
    def create(cls, *, track_disconnect: bool = False) -> PtyMasterTransport:
        import pty

        master, slave = pty.openpty()
        return cls(master, slave, track_disconnect=track_disconnect)

    @property
    def client_path(self) -> str:
        return os.ttyname(self._slave)

    def set_timeout(self, timeout: float) -> None:
        self._timeout = timeout

    def recv(self, size: int) -> bytes:
        ready, _, _ = select.select([self._master], [], [], self._timeout)
        if not ready:
            raise socket.timeout()
        try:
            chunk = os.read(self._master, size)
        except OSError as exc:
            # Linux reports PTY hangup as EIO once all slave fds are closed.
            if exc.errno != errno.EIO:
                raise
            return b''
        if chunk:
            self._on_client_data()
        return chunk

    def sendall(self, data: bytes) -> None:
        os.write(self._master, data)

    def close(self) -> None:
        os.close(self._master)
        try:
            os.close(self._slave)
        except OSError:
            pass

    def ignore_eof_for_disconnect(self) -> bool:
        """Whether an empty read should be ignored while waiting for a client."""
        return self._track_disconnect and not self._saw_client_data

    def _on_client_data(self) -> None:
        if self._saw_client_data:
            return
        self._saw_client_data = True
        if self._track_disconnect:
            self._release_slave()

    def _release_slave(self) -> None:
        if not self._slave_released:
            os.close(self._slave)
            self._slave_released = True


class BootloaderConnection:
    """Socket, Unix PTY master, or pyserial serial-port transport."""

    def __init__(
        self,
        sock: socket.socket | None = None,
        pty: PtyMasterTransport | None = None,
        serial_port: object | None = None,
        *,
        track_disconnect: bool = False,
    ) -> None:
        modes = sum(mode is not None for mode in (sock, pty, serial_port))
        if modes != 1:
            raise ValueError('exactly one of sock, pty, or serial_port is required')
        self._sock = sock
        self._pty = pty
        self._serial = serial_port
        self._timeout = _CLIENT_RECV_TIMEOUT
        self._track_disconnect = track_disconnect
        self._saw_client_data = False

    def set_timeout(self, timeout: float) -> None:
        self._timeout = timeout
        if self._sock is not None:
            self._sock.settimeout(timeout)
        if self._serial is not None:
            self._serial.timeout = timeout
        if self._pty is not None:
            self._pty.set_timeout(timeout)

    def recv(self, size: int) -> bytes:
        if self._sock is not None:
            chunk = self._sock.recv(size)
            if chunk:
                self._saw_client_data = True
            return chunk
        if self._serial is not None:
            try:
                data = self._serial.read(size)
            except Exception as exc:
                from serial.serialutil import SerialException

                if isinstance(exc, SerialException):
                    return b''
                raise
            if not data:
                raise socket.timeout()
            self._saw_client_data = True
            return data
        assert self._pty is not None
        chunk = self._pty.recv(size)
        if chunk:
            self._saw_client_data = True
        return chunk

    def sendall(self, data: bytes) -> None:
        if self._sock is not None:
            self._sock.sendall(data)
        elif self._serial is not None:
            self._serial.write(data)
        else:
            assert self._pty is not None
            self._pty.sendall(data)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
        if self._serial is not None:
            self._serial.close()

    def ignore_eof_for_disconnect(self) -> bool:
        """Ignore peer EOF until real client data arrives (PTY slave not open yet)."""
        if self._pty is not None:
            return self._pty.ignore_eof_for_disconnect()
        return False

    def abort_empty_session(self) -> bool:
        """True when an empty read should end this accept without a real disconnect."""
        return self._sock is not None and self._track_disconnect and not self._saw_client_data


def handle_client(
    conn: BootloaderConnection,
    stop_event: threading.Event,
    chip_mode: str = 'auto',
    on_detected: Callable[[str | None], None] | None = None,
    *,
    exit_on_disconnect: bool = False,
    flash: FlashImage | None = None,
) -> bool:
    """Handle one client session. Returns True if the transport reported disconnect."""
    session = ChipSession(chip_mode)
    if on_detected and session.detected_chip:
        on_detected(session.detected_chip)

    buf = bytearray()
    if flash is None:
        flash = get_flash_image()
    ram = RamImage()
    flash_ended = False
    flash_end_at = 0.0
    client_disconnected = False
    try:
        conn.set_timeout(_CLIENT_RECV_TIMEOUT)
        while not stop_event.is_set():
            if flash_ended and not exit_on_disconnect:
                if time.monotonic() - flash_end_at >= _FLASH_END_IDLE_SEC:
                    break
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                client_disconnected = True
                break
            if not chunk:
                if exit_on_disconnect and conn.abort_empty_session():
                    break
                if exit_on_disconnect and conn.ignore_eof_for_disconnect():
                    continue
                client_disconnected = True
                break
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
                    flash_end_at = time.monotonic()
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
                    flash_end_at = time.monotonic()
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
                            client_disconnected = True
                            break
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
                        flash_end_at = time.monotonic()
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
                        client_disconnected = True
                        break
            if client_disconnected:
                break
    finally:
        if on_detected:
            on_detected(session.detected_chip)
        conn.close()
    return client_disconnected


def _make_on_detected(port: int | None) -> Callable[[str | None], None] | None:
    if port is None:
        return None

    from esp32_mock_bootloader import daemon

    def _update(detected: str | None) -> None:
        try:
            daemon.set_detected_chip(port, detected)
        except OSError:
            pass

    return _update


def _tcp_listen_loop(
    srv: socket.socket,
    timeout: float | None,
    chip_mode: str,
    on_detected: Callable[[str | None], None] | None,
    label: str,
    *,
    exit_on_disconnect: bool = False,
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
            disconnected = handle_client(
                BootloaderConnection(sock=conn, track_disconnect=exit_on_disconnect),
                stop_event,
                chip_mode,
                on_detected,
                exit_on_disconnect=exit_on_disconnect,
            )
            print('Client disconnected', flush=True)
            if exit_on_disconnect and disconnected:
                break
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
    *,
    exit_on_disconnect: bool = False,
    track_registry: bool = False,
    port_file: str | None = None,
) -> None:
    from esp32_mock_bootloader import daemon

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((bind, port))
    except OSError:
        if port == 0:
            raise
        srv.bind((bind, 0))
    srv.listen(1)
    actual_port = srv.getsockname()[1]

    if port_file:
        with open(port_file, 'w', encoding='ascii') as f:
            f.write(str(actual_port))

    on_detected = _make_on_detected(actual_port)
    if track_registry:
        daemon.register_instance(actual_port, {
            'pid': os.getpid(),
            'port': actual_port,
            'chip': chip_mode,
            'bind': bind,
            'url': daemon.socket_url(actual_port, bind),
            'log_file': None,
            'detected_chip': None,
            'mode': 'foreground',
        })
    label = f'Mock bootloader (chip={chip_mode}) listening on {bind}:{actual_port}'
    try:
        _tcp_listen_loop(
            srv, timeout, chip_mode, on_detected, label,
            exit_on_disconnect=exit_on_disconnect,
        )
    finally:
        if track_registry:
            daemon.unregister_instance(actual_port)


def _run_unix_pty_server(
    timeout: float | None,
    port_file: str | None,
    chip_mode: str,
    on_detected: Callable[[str | None], None] | None,
    *,
    exit_on_disconnect: bool = False,
) -> None:
    pty_transport = PtyMasterTransport.create(track_disconnect=exit_on_disconnect)
    _write_serial_endpoint(port_file, pty_transport.client_path)
    print(
        f'Mock bootloader (chip={chip_mode}) on PTY {pty_transport.client_path}',
        flush=True,
    )
    start = time.time()
    try:
        while timeout is None or time.time() - start < timeout:
            stop_event = threading.Event()
            disconnected = handle_client(
                BootloaderConnection(pty=pty_transport),
                stop_event,
                chip_mode,
                on_detected,
                exit_on_disconnect=exit_on_disconnect,
            )
            if exit_on_disconnect and disconnected:
                break
    finally:
        pty_transport.close()
        if timeout is not None and time.time() - start >= timeout:
            print('Timeout reached, shutting down', flush=True)
    print('Mock bootloader stopped', flush=True)


def is_serial_port_name(port: str) -> bool:
    """True when *port* names a device path rather than a TCP port number."""
    if port.startswith('/dev/'):
        return True
    normalized = port.upper().removeprefix('\\\\.\\')
    return normalized.startswith('COM')


def resolve_serial_pair(
    client_port: str | None = None,
    serial_bind: str | None = None,
) -> tuple[str, str] | None:
    """Return (mock_bind, client_port) for null-modem serial mode, if configured.

    When only one port is given, the paired com0com port is looked up automatically.
    Legacy env vars ``ESP32_MOCK_COM_PORT`` / ``ESP32_MOCK_COM_PEER`` are still read.
    """
    from esp32_mock_bootloader.com0com import find_paired_port

    bind = (
        serial_bind
        or os.environ.get('ESP32_MOCK_SERIAL_BIND')
        or os.environ.get('ESP32_MOCK_COM_PORT')
    )
    client = (
        client_port
        or os.environ.get('ESP32_MOCK_PORT')
        or os.environ.get('ESP32_MOCK_COM_PEER')
    )
    if bind and client:
        return bind, client
    if bind or client:
        alone = bind or client
        assert alone is not None
        peer = find_paired_port(alone)
        if peer is None:
            raise ValueError(
                f'could not find a null-modem pair for serial port {alone!r}; '
                'pass --serial-bind as well, or use com0com on Windows',
            )
        if bind:
            return bind, peer
        return peer, alone
    return None


def _run_com_server(
    serial_bind: str,
    client_port: str,
    timeout: float | None,
    port_file: str | None,
    chip_mode: str,
    on_detected: Callable[[str | None], None] | None,
    *,
    exit_on_disconnect: bool = False,
) -> None:
    """Serve ROM protocol on a serial device path via pyserial.

    Typical use is a Windows com0com null-modem pair, but any OS path that
    pyserial can open works (e.g. ``/dev/ttyUSB0`` on Linux).
    """
    import serial

    _write_serial_endpoint(port_file, client_port)
    print(
        f'Mock bootloader (chip={chip_mode}) on {serial_bind} (client: {client_port})',
        flush=True,
    )
    ser = serial.Serial(serial_bind, baudrate=115200, timeout=_CLIENT_RECV_TIMEOUT)
    start = time.time()
    try:
        while timeout is None or time.time() - start < timeout:
            stop_event = threading.Event()
            disconnected = handle_client(
                BootloaderConnection(serial_port=ser),
                stop_event,
                chip_mode,
                on_detected,
                exit_on_disconnect=exit_on_disconnect,
            )
            if exit_on_disconnect and disconnected:
                break
    finally:
        ser.close()
        if timeout is not None and time.time() - start >= timeout:
            print('Timeout reached, shutting down', flush=True)
    print('Mock bootloader stopped', flush=True)


def _run_windows_pty_server(
    timeout: float | None,
    port_file: str | None,
    chip_mode: str,
    on_detected: Callable[[str | None], None] | None,
    *,
    exit_on_disconnect: bool = False,
) -> None:
    """CI-friendly fallback when no com0com pair is configured."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    endpoint = f'socket://127.0.0.1:{port}'
    _write_serial_endpoint(port_file, endpoint)
    _tcp_listen_loop(
        srv, timeout, chip_mode, on_detected,
        f'Mock bootloader (chip={chip_mode}) on {endpoint} (Windows socket fallback)',
        exit_on_disconnect=exit_on_disconnect,
    )


def run_pty_server(
    timeout: float | None,
    port_file: str | None,
    chip_mode: str = 'auto',
    *,
    client_port: str | None = None,
    serial_bind: str | None = None,
    exit_on_disconnect: bool = False,
) -> None:
    on_detected = _make_on_detected(None)
    com_pair = resolve_serial_pair(client_port, serial_bind)
    if com_pair is not None:
        _run_com_server(
            *com_pair, timeout, port_file, chip_mode, on_detected,
            exit_on_disconnect=exit_on_disconnect,
        )
    elif os.name == 'nt':
        _run_windows_pty_server(
            timeout, port_file, chip_mode, on_detected,
            exit_on_disconnect=exit_on_disconnect,
        )
    else:
        _run_unix_pty_server(
            timeout, port_file, chip_mode, on_detected,
            exit_on_disconnect=exit_on_disconnect,
        )
