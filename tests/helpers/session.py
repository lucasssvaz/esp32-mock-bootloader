# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Test helpers for attaching to an already-running daemon."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from esp32_mock_bootloader.advanced import protocol
from esp32_mock_bootloader.api import MockHandle
from esp32_mock_bootloader.client import Client
from esp32_mock_bootloader.session import Session


@contextmanager
def attach_session(port: int, chip: str = 'esp32') -> Iterator[Client]:
    """Yield a protocol client for a daemon started outside this context."""
    session = Session.daemon(chip=chip, port=port)
    session._port = port  # noqa: SLF001
    session._started = True  # noqa: SLF001
    handle = MockHandle(chip=chip, port=port, mode='daemon', autostart=False)
    handle._session = session  # noqa: SLF001
    handle._port = port  # noqa: SLF001
    client = protocol.connect(handle)
    try:
        yield client
    finally:
        client._close_transport()
