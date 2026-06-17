# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""High-level programmatic API for downstream Python projects."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from esp32_mock_bootloader import daemon
from esp32_mock_bootloader.testing import server


class MockBootloader:
    """Context manager wrapping daemon start/stop for CI and integration tests."""

    def __init__(
        self,
        chip: str = 'auto',
        port: int | None = None,
        *,
        bind: str = daemon.DEFAULT_BIND,
        startup_timeout: float = daemon.DEFAULT_STARTUP_TIMEOUT,
        registry_dir: Path | None = None,
    ) -> None:
        self.chip = chip
        self._port = port
        self._bind = bind
        self._startup_timeout = startup_timeout
        self._registry_dir = registry_dir
        self._data: dict[str, Any] | None = None

    def start(self) -> MockBootloader:
        port = self._port if self._port is not None else server.reserve_tcp_port()
        self._port = port
        self._data = daemon.start_daemon(
            port=port,
            chip_mode=self.chip,
            startup_timeout=self._startup_timeout,
            bind=self._bind,
            base=self._registry_dir,
            force=True,
        )
        return self

    def stop(self) -> None:
        if self._port is not None:
            daemon.stop_daemon(self._port, self._registry_dir)
        self._data = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError('MockBootloader is not started')
        return self._port

    @property
    def url(self) -> str:
        if self._data is not None:
            return str(self._data['url'])
        return daemon.socket_url(self.port, self._bind)

    @property
    def detected_chip(self) -> str | None:
        state = daemon.read_state(self.port, self._registry_dir)
        if not state:
            return None
        detected = state.get('detected_chip')
        return str(detected) if detected else None

    def connect(self) -> socket.socket:
        return server.connect(self.port)

    def __enter__(self) -> MockBootloader:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
