# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures and helpers for mock bootloader tests."""

from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import pytest

from esp32_mock_bootloader.chip_profiles import (  # noqa: F401
    CHIP_PROFILES,
    LEGACY_DETECT_REG,
    SUPPORTED_CHIPS,
    chips_with_unique_efuse,
    reference_chip as default_reference_chip,
)
from esptool.targets import CHIP_DEFS

# All esptool targets — single source via chip_profiles / esptool.targets.CHIP_LIST.
ESPTOOL_CHIPS = list(SUPPORTED_CHIPS)

TRANSPORTS = ['tcp', 'pty']


def reserve_tcp_port() -> int:
    """Bind to port 0 and return an ephemeral localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


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
CMD_READ_REG = 0x0A
CMD_WRITE_REG = 0x09
CMD_SPI_SET_PARAMS = 0x0B
CMD_SPI_ATTACH = 0x0D
CMD_CHANGE_BAUDRATE = 0x0F
CMD_FLASH_DEFL_BEGIN = 0x10
CMD_FLASH_DEFL_DATA = 0x11
CMD_FLASH_DEFL_END = 0x12
CMD_SPI_FLASH_MD5 = 0x13
CMD_GET_SECURITY_INFO = 0x14


@pytest.fixture
def reference_chip() -> str:
    return default_reference_chip()


def esptool_available() -> bool:
    if shutil.which('esptool'):
        return True
    try:
        subprocess.run(
            [sys.executable, '-m', 'esptool', 'version'],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


@pytest.fixture(scope='session', autouse=True)
def _log_esptool_version() -> None:
    if not esptool_available():
        return
    if shutil.which('esptool'):
        cmd = ['esptool', 'version']
    else:
        cmd = [sys.executable, '-m', 'esptool', 'version']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    version_line = (result.stdout or result.stderr).strip().splitlines()[:1]
    if version_line:
        print(f'\n[conftest] esptool reference client: {version_line[0]}')


def pytest_configure(config: pytest.Config) -> None:
    (Path(__file__).resolve().parents[1] / 'reports').mkdir(exist_ok=True)
    config.addinivalue_line(
        'markers',
        'esptool: integration tests that invoke the esptool CLI',
    )
    config.addinivalue_line(
        'markers',
        'transport: TCP and PTY transport integration tests',
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if esptool_available():
        return
    skip = pytest.mark.skip(reason='esptool not installed')
    for item in items:
        if 'esptool' in item.keywords:
            item.add_marker(skip)


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


def make_command(cmd: int, data: bytes = b'', checksum: int = 0) -> bytes:
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


class _SerialLink:
    """Duck-type socket interface for pyserial (PTY client)."""

    def __init__(self, port: str) -> None:
        import serial

        self._ser = serial.Serial(port, baudrate=115200, timeout=5.0)

    def sendall(self, data: bytes) -> None:
        self._ser.write(data)

    def recv(self, size: int) -> bytes:
        return self._ser.read(size)

    def settimeout(self, timeout: float) -> None:
        self._ser.timeout = timeout

    def close(self) -> None:
        self._ser.close()


def start_mock_pty(
    path_file: Path,
    timeout: float | None = 10.0,
    chip: str = 'auto',
) -> subprocess.Popen[bytes]:
    path_file.parent.mkdir(parents=True, exist_ok=True)
    if path_file.exists():
        path_file.unlink()
    cmd = [
        sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
        '--pty', '--pty-path-file', str(path_file),
        '--chip', chip,
    ]
    com_port = os.environ.get('ESP32_MOCK_COM_PORT')
    com_peer = os.environ.get('ESP32_MOCK_COM_PEER')
    if com_port and com_peer:
        cmd.extend(['--com-port', com_port, '--com-peer', com_peer])
    if timeout is not None:
        cmd.extend(['--timeout', str(timeout)])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(100):
        if path_file.is_file():
            pty_path = path_file.read_text(encoding='ascii').strip()
            if pty_path:
                time.sleep(0.5)
                return proc
        if proc.poll() is not None:
            err = proc.stderr.read().decode() if proc.stderr else ''
            raise RuntimeError(f'PTY mock exited during startup: {err}')
        time.sleep(0.1)
    proc.terminate()
    raise TimeoutError(f'PTY path file not created: {path_file}')


def read_pty_path(path_file: Path) -> str:
    return path_file.read_text(encoding='ascii').strip()


def connect_serial_endpoint(endpoint: str) -> socket.socket | _SerialLink:
    """Connect to a PTY device path (Unix) or socket:// URL (Windows --pty shim)."""
    if endpoint.startswith('socket://'):
        parsed = urlparse(endpoint)
        if parsed.scheme != 'socket' or parsed.port is None:
            raise ValueError(f'invalid socket endpoint: {endpoint}')
        host = parsed.hostname or '127.0.0.1'
        if host not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError(f'unsupported socket host in endpoint: {host}')
        return connect_to_server(parsed.port, host=host)
    return _SerialLink(endpoint)


def connect_pty(pty_path: str) -> socket.socket | _SerialLink:
    return connect_serial_endpoint(pty_path)


@contextmanager
def running_mock(
    transport: str,
    chip: str,
    *,
    port: int | None = None,
    path_file: Path | None = None,
    timeout: float | None = 30.0,
) -> Iterator[tuple[subprocess.Popen[bytes], int | None, str | None]]:
    """Start mock server over TCP or PTY; yield (proc, tcp_port, pty_path)."""
    if transport == 'tcp':
        proc, tcp_port = start_mock_server(port, timeout=timeout, chip=chip)
        try:
            yield proc, tcp_port, None
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        return

    pty_file = path_file or Path(tempfile.gettempdir()) / (
        f'esp32-mock-pty-{uuid.uuid4().hex}.path'
    )
    proc = start_mock_pty(pty_file, timeout=timeout, chip=chip)
    try:
        yield proc, None, read_pty_path(pty_file)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def connect_transport(
    transport: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
) -> socket.socket | _SerialLink:
    if transport == 'tcp':
        if port is None:
            raise ValueError('port required for TCP transport')
        return connect_to_server(port)
    if pty_path is None:
        raise ValueError('pty_path required for PTY transport')
    return connect_pty(pty_path)


def start_mock_server(
    port: int | None = None,
    timeout: float | None = 10.0,
    chip: str = 'auto',
) -> tuple[subprocess.Popen[bytes], int]:
    listen_port = port if port is not None else reserve_tcp_port()
    cmd = [
        sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
        '--port', str(listen_port),
        '--chip', chip,
    ]
    if timeout is not None:
        cmd.extend(['--timeout', str(timeout)])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    return proc, listen_port


def connect_to_server(
    port: int, host: str = '127.0.0.1', retries: int = 5,
) -> socket.socket:
    for i in range(retries):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((host, port))
            return s
        except ConnectionRefusedError:
            time.sleep(0.3)
            if i == retries - 1:
                raise
    raise ConnectionRefusedError('Could not connect to mock server')


def send_and_receive(
    sock: socket.socket | _SerialLink, data: bytes, recv_size: int = 4096,
) -> bytes:
    sock.sendall(data)
    time.sleep(0.1)
    try:
        return sock.recv(recv_size)
    except socket.timeout:
        return b''


def send_sync(sock: socket.socket | _SerialLink) -> None:
    sync_data = bytes([0x07, 0x07, 0x12, 0x20]) + (b'\x55' * 32)
    send_and_receive(sock, make_command(CMD_SYNC, sync_data), 8192)


def read_reg_value(sock: socket.socket | _SerialLink, addr: int) -> int | None:
    raw = send_and_receive(sock, make_command(CMD_READ_REG, struct.pack('<I', addr)))
    frames = slip_decode_frames(raw)
    if not frames:
        return None
    return parse_response(frames[0])[3]


def minimal_plain_flash(sock: socket.socket | _SerialLink) -> bool:
    send_sync(sock)
    fb = make_command(CMD_FLASH_BEGIN, struct.pack('<IIII', 0x100, 1, 0x100, 0x10000))
    fd = make_command(
        CMD_FLASH_DATA,
        struct.pack('<IIII', 0x100, 0, 0, 0) + (b'\xAB' * 0x100),
    )
    fe = make_command(CMD_FLASH_END, struct.pack('<I', 1))
    raw = send_and_receive(sock, fb + fd + fe, 4096)
    frames = slip_decode_frames(raw)
    if len(frames) < 3:
        return False
    cmds = [parse_response(frame)[1] for frame in frames]
    return cmds == [CMD_FLASH_BEGIN, CMD_FLASH_DATA, CMD_FLASH_END]


def run_esptool(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    if shutil.which('esptool'):
        cmd = ['esptool'] + list(args)
    else:
        cmd = [sys.executable, '-m', 'esptool'] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def create_fake_binary(path: Path, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x00' * size)
    return path


def _esptool_write_flash_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (
        f'rc={result.returncode}\nstdout: {result.stdout[-500:]}\n'
        f'stderr: {result.stderr[-300:]}'
    )


def _esptool_write_flash_ok(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 0 or 'Wrote' not in result.stdout:
        return False
    if 'Hash of data verified.' in result.stdout:
        return True
    # ESP8266 ROM verify can succeed without the Hash line (still uses SPI_FLASH_MD5).
    return (
        'Verifying written data' in result.stdout
        and 'Fatal error' not in result.stdout
        and 'Fatal error' not in result.stderr
    )


def esptool_write_flash(
    chip: str,
    bin_path: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
    no_stub: bool = False,
    timeout: float = 30.0,
    extra_args: tuple[str, ...] = (),
) -> tuple[bool, str]:
    if (port is None) == (pty_path is None):
        raise ValueError('exactly one of port or pty_path is required')
    esptool_port = pty_path if pty_path is not None else f'socket://localhost:{port}'
    args = [
        '--chip', chip,
        '--port', esptool_port,
        '--before', 'no-reset',
        '--after', 'no-reset',
    ]
    if no_stub:
        args.append('--no-stub')
    args.extend([
        'write-flash',
        *extra_args,
        '0x10000', bin_path,
    ])
    result = run_esptool(*args, timeout=timeout)
    return _esptool_write_flash_ok(result), _esptool_write_flash_detail(result)


def esptool_write_flash_no_stub(
    chip: str,
    bin_path: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """write-flash with --no-stub (ROM MD5: 32-byte hex response)."""
    return esptool_write_flash(
        chip, bin_path, port=port, pty_path=pty_path, no_stub=True, timeout=timeout,
    )


def esptool_write_flash_with_stub(
    chip: str,
    bin_path: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
    timeout: float = 30.0,
    extra_args: tuple[str, ...] = ('-z',),
) -> tuple[bool, str]:
    """write-flash with default stub (binary MD5: 16-byte digest response)."""
    return esptool_write_flash(
        chip,
        bin_path,
        port=port,
        pty_path=pty_path,
        no_stub=False,
        timeout=timeout,
        extra_args=extra_args,
    )
