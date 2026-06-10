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

from esp32_mock_bootloader.chip_profiles import CHIP_PROFILES, LEGACY_DETECT_REG

SLIP_END = 0xC0
SLIP_ESC = 0xDB
SLIP_ESC_END = 0xDC
SLIP_ESC_DD = 0xDD

CMD_FLASH_BEGIN = 0x02
CMD_FLASH_DATA = 0x03
CMD_FLASH_END = 0x04
CMD_MEM_BEGIN = 0x05
CMD_MEM_END = 0x06
CMD_MEM_DATA = 0x07
CMD_SYNC = 0x08
CMD_WRITE_REG = 0x09
CMD_READ_REG = 0x0A
CMD_SPI_SET_PARAMS = 0x0B
CMD_SPI_ATTACH = 0x0D
CMD_CHANGE_BAUDRATE = 0x0F
CMD_FLASH_DEFL_BEGIN = 0x10
CMD_FLASH_DEFL_DATA = 0x11
CMD_FLASH_DEFL_END = 0x12
CMD_SPI_FLASH_MD5 = 0x13
CMD_GET_SECURITY_INFO = 0x14

ROM_SYNC_VALUE = 0x20120707
FLASH_MOCK_SIZE = 4 * 1024 * 1024


EFUSE_WINDOW = 0x200

class ChipSession:
    """Per-connection chip detection state."""

    def __init__(self, chip_mode: str) -> None:
        if chip_mode != 'auto' and chip_mode not in CHIP_PROFILES:
            raise ValueError(f'unknown chip: {chip_mode}')
        self.chip_mode = chip_mode
        self.detected_chip: str | None = chip_mode if chip_mode != 'auto' else None
        self._logged_detection = False
        self.seen_addrs: set[int] = set()
        self.get_security_info_seen = False
        self.stub_active = False

    def note_detection(self, chip: str, source: str) -> None:
        if self.chip_mode != 'auto':
            return
        if self.detected_chip is None:
            self.detected_chip = chip
        if not self._logged_detection:
            print(f'Detected chip: {chip} ({source})', flush=True)
            self._logged_detection = True

    def image_chip_id(self) -> int:
        if self.detected_chip is not None:
            chip_id = CHIP_PROFILES[self.detected_chip].image_chip_id
            return 0 if chip_id is None else chip_id
        return 0


def _chips_matching_efuse(addr: int) -> list[str]:
    return [
        chip for chip, profile in CHIP_PROFILES.items()
        if profile.efuse_base <= addr < profile.efuse_base + EFUSE_WINDOW
    ]


def _chips_matching_detect_reg(addr: int) -> list[str]:
    return [
        chip for chip, profile in CHIP_PROFILES.items()
        if profile.detect_magic and addr == profile.detect_reg
    ]


def _infer_chip_from_addr(addr: int) -> str | None:
    """Infer SoC from a register address (unique matches only)."""
    if addr == LEGACY_DETECT_REG:
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
    profile = CHIP_PROFILES[chip]
    if profile.detect_magic and addr == profile.detect_reg:
        return profile.detect_magic
    if profile.efuse_base <= addr < profile.efuse_base + EFUSE_WINDOW:
        return 0
    return 0


class FlashImage:
    """Minimal flash backing store for compressed writes and MD5 verification."""

    def __init__(self) -> None:
        self.data = bytearray(b'\xff' * FLASH_MOCK_SIZE)
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

    def md5_stub_response(self, payload: bytes, *, stub: bool = False) -> bytes:
        addr, size, _, _ = struct.unpack_from('<IIII', payload, 0)
        digest = hashlib.md5(bytes(self.data[addr:addr + size])).digest()
        if stub:
            # Flasher stub: 16-byte binary digest plus 2 status bytes.
            resp_data = digest + struct.pack('<BB', 0, 0)
        else:
            # ROM bootloader: 32-byte ASCII hex digest plus 2 status bytes.
            resp_data = digest.hex().encode('ascii') + struct.pack('<BB', 0, 0)
        header = struct.pack('<BBHI', 0x01, CMD_SPI_FLASH_MD5, len(resp_data), 0)
        return slip_encode(header + resp_data)


def slip_encode(data: bytes) -> bytes:
    out = bytearray([SLIP_END])
    for b in data:
        if b == SLIP_END:
            out.extend([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out.extend([SLIP_ESC, SLIP_ESC_DD])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


def slip_decode_frames(raw: bytes) -> list[bytes]:
    frames = []
    current = bytearray()
    in_frame = False
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == SLIP_END:
            if in_frame and current:
                frames.append(bytes(current))
                current = bytearray()
            in_frame = True
        elif in_frame:
            if b == SLIP_ESC:
                i += 1
                if i < len(raw):
                    if raw[i] == SLIP_ESC_END:
                        current.append(SLIP_END)
                    elif raw[i] == SLIP_ESC_DD:
                        current.append(SLIP_ESC)
                    else:
                        current.append(raw[i])
            else:
                current.append(b)
        i += 1
    return frames


def make_response(cmd: int, value: int = 0, status: int = 0, error: int = 0) -> bytes:
    status_bytes = struct.pack('<BBBB', status, error, 0, 0)
    size = len(status_bytes)
    header = struct.pack('<BBHI', 0x01, cmd, size, value)
    return slip_encode(header + status_bytes)


def make_sync_response() -> bytes:
    return make_response(CMD_SYNC, value=ROM_SYNC_VALUE)


def handle_read_reg(data: bytes, session: ChipSession) -> bytes:
    if len(data) < 4:
        return make_response(CMD_READ_REG, value=0)
    addr = struct.unpack('<I', data[:4])[0]
    session.seen_addrs.add(addr)

    if session.chip_mode != 'auto':
        chip = session.chip_mode
        return make_response(CMD_READ_REG, value=_read_reg_value_for_chip(addr, chip))

    if session.detected_chip is not None:
        return make_response(
            CMD_READ_REG,
            value=_read_reg_value_for_chip(addr, session.detected_chip),
        )

    # esptool --chip <soc> validation probes the legacy ESP32 magic register
    # (0x40001000) for many SoC classes. Never return ESP32 magic until the
    # session has chip-specific evidence (unique detect/efuse addresses).
    if addr == LEGACY_DETECT_REG:
        return make_response(CMD_READ_REG, value=0)

    inferred = _infer_chip_from_addr(addr)
    if inferred is not None:
        session.note_detection(inferred, f'READ_REG 0x{addr:08x}')

    if session.detected_chip is not None:
        return make_response(
            CMD_READ_REG,
            value=_read_reg_value_for_chip(addr, session.detected_chip),
        )

    for chip, profile in CHIP_PROFILES.items():
        if profile.detect_magic and addr == profile.detect_reg:
            session.note_detection(chip, f'READ_REG 0x{addr:08x}')
            return make_response(CMD_READ_REG, value=profile.detect_magic)

    efuse_matches = _chips_matching_efuse(addr)
    if len(efuse_matches) == 1:
        session.note_detection(efuse_matches[0], f'READ_REG efuse 0x{addr:08x}')
    elif len(efuse_matches) > 1 and session.detected_chip is None:
        # Shared efuse window (e.g. early C3 reads) — respond without guessing.
        pass

    if efuse_matches:
        return make_response(CMD_READ_REG, value=0)

    return make_response(CMD_READ_REG, value=0)


def _active_chip(session: ChipSession) -> str | None:
    if session.chip_mode != 'auto':
        return session.chip_mode
    return session.detected_chip


def handle_get_security_info(session: ChipSession) -> bytes:
    session.get_security_info_seen = True
    active = _active_chip(session)
    if active is not None and not CHIP_PROFILES[active].supports_security_info:
        return make_response(CMD_GET_SECURITY_INFO, status=1, error=0x05)
    if session.chip_mode != 'auto' or session.detected_chip is not None:
        chip_id = session.image_chip_id()
        payload = struct.pack('<IBBBBBBBBII', 0, 0, 0, 0, 0, 0, 0, 0, 0, chip_id, 0)
        header = struct.pack('<BBHI', 0x01, CMD_GET_SECURITY_INFO, len(payload), 0)
        return slip_encode(header + payload)

    # Before chip-specific READ_REG traffic, return ROM error so esptool does not
    # treat the session as ESP32 (chip_id 0) during connect validation.
    return make_response(CMD_GET_SECURITY_INFO, status=1, error=0x05)


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
            last_end = buf.rfind(SLIP_END)
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
                data = frame[8:] if len(frame) > 8 else b''

                response = b''
                if cmd == CMD_SYNC:
                    for _ in range(8):
                        response += make_sync_response()
                elif cmd == CMD_READ_REG:
                    response = handle_read_reg(data, session)
                    if on_detected:
                        on_detected(session.detected_chip)
                elif cmd == CMD_GET_SECURITY_INFO:
                    response = handle_get_security_info(session)
                elif cmd == CMD_FLASH_BEGIN:
                    flash.begin_plain(data)
                    response = make_response(cmd)
                elif cmd == CMD_FLASH_DATA:
                    flash.write_plain_block(data)
                    response = make_response(cmd)
                elif cmd == CMD_FLASH_END:
                    response = make_response(cmd)
                    flash_ended = True
                elif cmd == CMD_FLASH_DEFL_BEGIN:
                    flash.begin_defl(data)
                    response = make_response(cmd)
                elif cmd == CMD_FLASH_DEFL_DATA:
                    flash.write_defl_block(data)
                    response = make_response(cmd)
                elif cmd == CMD_FLASH_DEFL_END:
                    flash.end_defl()
                    response = make_response(cmd)
                    flash_ended = True
                elif cmd == CMD_MEM_BEGIN:
                    response = make_response(cmd)
                elif cmd == CMD_MEM_DATA:
                    response = make_response(cmd)
                elif cmd == CMD_MEM_END:
                    if len(data) >= 8:
                        stay_in_loader, entrypoint = struct.unpack_from('<II', data, 0)
                        if stay_in_loader == 0 and entrypoint != 0:
                            session.stub_active = True
                    response = make_response(cmd)
                    ohai = bytes([SLIP_END, 0x4F, 0x48, 0x41, 0x49, SLIP_END])
                    response += ohai
                elif cmd == CMD_WRITE_REG:
                    response = make_response(cmd)
                elif cmd == CMD_SPI_SET_PARAMS:
                    response = make_response(cmd)
                elif cmd == CMD_SPI_ATTACH:
                    response = make_response(cmd)
                elif cmd == CMD_CHANGE_BAUDRATE:
                    response = make_response(cmd)
                elif cmd == CMD_SPI_FLASH_MD5:
                    response = flash.md5_stub_response(data, stub=session.stub_active)
                else:
                    response = make_response(cmd)

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
