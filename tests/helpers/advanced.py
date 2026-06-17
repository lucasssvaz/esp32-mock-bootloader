# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Opt-in helpers for tests that exercise raw transport / process APIs."""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Iterator

from esp32_mock_bootloader import transport


@contextmanager
def raw_tcp(port: int, *, host: str = '127.0.0.1') -> Iterator[socket.socket]:
    """Explicit raw TCP socket — only for advanced transport tests."""
    sock = transport.connect(port, host=host)
    try:
        yield sock
    finally:
        sock.close()


@contextmanager
def raw_transport(
    transport_name: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
) -> Iterator[socket.socket | transport.SerialLink]:
    """Explicit transport handle — only for advanced PTY/TCP tests."""
    handle = transport.connect_transport(transport_name, port=port, pty_path=pty_path)
    try:
        yield handle
    finally:
        handle.close()
