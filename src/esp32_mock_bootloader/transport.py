# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Client transports to a running mock (TCP, PTY, serial)."""

from __future__ import annotations

import socket
import time
from urllib.parse import urlparse

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
    'DEFAULT_CONNECT_TIMEOUT',
    'SerialLink',
    'connect',
    'connect_pty',
    'connect_serial_endpoint',
    'connect_transport',
]
