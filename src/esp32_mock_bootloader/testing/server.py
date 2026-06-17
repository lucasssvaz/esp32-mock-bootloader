# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Subprocess server lifecycle and transport connection helpers."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from esp32_mock_bootloader.daemon import wait_for_port

DEFAULT_STARTUP_TIMEOUT = 5.0
DEFAULT_CONNECT_TIMEOUT = 3.0


class SerialLink:
    """Duck-type socket interface for pyserial (PTY client)."""

    def __init__(self, port: str) -> None:
        import serial

        self._ser = serial.Serial(port, baudrate=115200, timeout=0.05)

    def sendall(self, data: bytes) -> None:
        self._ser.write(data)

    def recv(self, size: int) -> bytes:
        return self._ser.read(size)

    def settimeout(self, timeout: float) -> None:
        self._ser.timeout = timeout

    def close(self) -> None:
        self._ser.close()


def stop_subprocess(proc: subprocess.Popen[bytes], *, timeout: float = 1.0) -> None:
    """Wait for a test server to exit; signal only if it is still running."""
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)


def reserve_tcp_port() -> int:
    """Bind to port 0 and return an ephemeral localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_server(
    port: int | None = None,
    timeout: float | None = 10.0,
    chip: str = 'auto',
    *,
    exit_on_disconnect: bool = False,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> tuple[subprocess.Popen[bytes], int]:
    """Start mock bootloader subprocess; return (proc, listen_port)."""
    listen_port = port if port is not None else reserve_tcp_port()
    cmd = [
        sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
        '--port', str(listen_port),
        '--chip', chip,
    ]
    if exit_on_disconnect:
        cmd.append('--exit-on-disconnect')
    if timeout is not None:
        cmd.extend(['--timeout', str(timeout)])
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not wait_for_port(listen_port, timeout=startup_timeout):
        exit_code = proc.poll()
        stop_subprocess(proc)
        raise RuntimeError(
            f'mock server did not open port {listen_port} (exit={exit_code})',
        )
    return proc, listen_port


# Backward-compatible alias for in-repo tests during migration.
start_mock_server = start_server


def connect(
    port: int,
    host: str = '127.0.0.1',
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> socket.socket:
    """Open a TCP connection to a running mock server."""
    deadline = time.time() + timeout
    delay = 0.05
    last_err: ConnectionRefusedError | None = None
    while time.time() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(5.0)
            sock.connect((host, port))
            return sock
        except ConnectionRefusedError as exc:
            sock.close()
            last_err = exc
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)
    raise last_err or ConnectionRefusedError(f'Could not connect to mock server on {host}:{port}')


connect_to_server = connect


def start_pty(
    path_file: Path,
    timeout: float | None = 10.0,
    chip: str = 'auto',
    *,
    exit_on_disconnect: bool = False,
) -> subprocess.Popen[bytes]:
    path_file.parent.mkdir(parents=True, exist_ok=True)
    if path_file.exists():
        path_file.unlink()
    cmd = [
        sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
        '--pty', '--pty-path-file', str(path_file),
        '--chip', chip,
    ]
    if exit_on_disconnect:
        cmd.append('--exit-on-disconnect')
    serial_bind = os.environ.get('ESP32_MOCK_SERIAL_BIND') or os.environ.get('ESP32_MOCK_COM_PORT')
    client_port = os.environ.get('ESP32_MOCK_PORT') or os.environ.get('ESP32_MOCK_COM_PEER')
    if serial_bind and client_port:
        cmd.extend(['--serial-bind', serial_bind, '--port', client_port])
    elif client_port:
        cmd.extend(['--port', client_port])
    elif serial_bind:
        cmd.extend(['--serial-bind', serial_bind])
    if timeout is not None:
        cmd.extend(['--timeout', str(timeout)])
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + DEFAULT_STARTUP_TIMEOUT
    while time.time() < deadline:
        if path_file.is_file():
            pty_path = path_file.read_text(encoding='ascii').strip()
            if pty_path:
                return proc
        if proc.poll() is not None:
            raise RuntimeError('PTY mock exited during startup')
        time.sleep(0.02)
    proc.terminate()
    raise TimeoutError(f'PTY path file not created: {path_file}')


start_mock_pty = start_pty


def read_pty_path(path_file: Path) -> str:
    return path_file.read_text(encoding='ascii').strip()


def connect_serial_endpoint(endpoint: str) -> socket.socket | SerialLink:
    """Connect to a PTY device path (Unix) or socket:// URL (Windows --pty shim)."""
    if endpoint.startswith('socket://'):
        parsed = urlparse(endpoint)
        if parsed.scheme != 'socket' or parsed.port is None:
            raise ValueError(f'invalid socket endpoint: {endpoint}')
        host = parsed.hostname or '127.0.0.1'
        if host not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError(f'unsupported socket host in endpoint: {host}')
        return connect(parsed.port, host=host)
    return SerialLink(endpoint)


def connect_pty(pty_path: str) -> socket.socket | SerialLink:
    return connect_serial_endpoint(pty_path)


@contextmanager
def running_server(
    chip: str,
    *,
    port: int | None = None,
    timeout: float | None = 30.0,
) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    """Start TCP mock server; yield (proc, port)."""
    proc, listen_port = start_server(port, timeout=timeout, chip=chip)
    try:
        yield proc, listen_port
    finally:
        stop_subprocess(proc)


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
        proc, tcp_port = start_server(
            port, timeout=timeout, chip=chip, exit_on_disconnect=True,
        )
        try:
            yield proc, tcp_port, None
        finally:
            stop_subprocess(proc)
        return

    pty_file = path_file or Path(tempfile.gettempdir()) / (
        f'esp32-mock-pty-{uuid.uuid4().hex}.path'
    )
    proc = start_pty(pty_file, timeout=timeout, chip=chip, exit_on_disconnect=True)
    try:
        yield proc, None, read_pty_path(pty_file)
    finally:
        stop_subprocess(proc)


def connect_transport(
    transport: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
) -> socket.socket | SerialLink:
    if transport == 'tcp':
        if port is None:
            raise ValueError('port required for TCP transport')
        return connect(port)
    if pty_path is None:
        raise ValueError('pty_path required for PTY transport')
    return connect_pty(pty_path)


__all__ = [
    'SerialLink',
    'connect',
    'connect_pty',
    'connect_serial_endpoint',
    'connect_to_server',
    'connect_transport',
    'read_pty_path',
    'reserve_tcp_port',
    'running_mock',
    'running_server',
    'start_mock_pty',
    'start_mock_server',
    'start_pty',
    'start_server',
    'stop_subprocess',
]
