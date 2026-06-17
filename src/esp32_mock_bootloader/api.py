# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Public programmatic API — mock_bootloader factory and MockHandle."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, TextIO

from esp32_mock_bootloader import daemon, instances
from esp32_mock_bootloader.instances import (
    register_foreground_handle,
    unregister_foreground_handle,
)
from esp32_mock_bootloader.registry import Registry
from esp32_mock_bootloader.session import Session

Mode = Literal['daemon', 'foreground']
StatusFormat = Literal['data', 'text', 'json']


def _registry_from_env() -> Registry | None:
    env = os.environ.get('ESP32_MOCK_BOOTLOADER_STATE_DIR')
    if not env:
        return None
    return Registry(Path(env))


class _AdvancedAccess:
    def __init__(self, handle: MockHandle) -> None:
        self._handle = handle

    @property
    def transport(self):
        from esp32_mock_bootloader.advanced import protocol

        return protocol.connect(self._handle)._ensure_transport()

    def send_raw(self, packet: bytes, recv_size: int = 4096) -> bytes:
        from esp32_mock_bootloader.advanced import protocol

        return protocol.connect(self._handle).send_and_receive(packet, recv_size)


class MockHandle:
    """One running mock bootloader endpoint."""

    def __init__(
        self,
        chip: str = 'auto',
        port: int | None = None,
        *,
        mode: Mode = 'foreground',
        exit_on_disconnect: bool = False,
        timeout: float | None = None,
        bind: str = daemon.DEFAULT_BIND,
        startup_timeout: float = daemon.DEFAULT_STARTUP_TIMEOUT,
        registry: Registry | None = None,
        autostart: bool = True,
    ) -> None:
        self._chip = chip
        self._requested_port = port
        self._mode = mode
        self._exit_on_disconnect = exit_on_disconnect
        self._timeout = timeout
        self._bind = bind
        self._startup_timeout = startup_timeout
        self._registry = registry if registry is not None else _registry_from_env()
        self._port: int | None = None
        self._session: Session | None = None
        if autostart:
            self.start()

    @property
    def chip(self) -> str:
        return self._chip

    @property
    def detected_chip(self) -> str | None:
        if self._session is None:
            return None
        return self._session.detected_chip

    @property
    def returncode(self) -> int | None:
        if self._session is None:
            return None
        return self._session.returncode

    @property
    def advanced(self) -> _AdvancedAccess:
        return _AdvancedAccess(self)

    def _require_started(self) -> Session:
        if self._session is None or not self._session._started:  # noqa: SLF001
            raise RuntimeError('mock bootloader is not started')
        return self._session

    def start(self) -> MockHandle:
        if self._session is not None and self._session._started:  # noqa: SLF001
            return self
        if self._mode == 'foreground':
            self._session = Session.foreground(
                chip=self._chip,
                port=self._requested_port,
                exit_on_disconnect=self._exit_on_disconnect,
                timeout=self._timeout,
                startup_timeout=self._startup_timeout,
            )
        else:
            self._session = Session.daemon(
                chip=self._chip,
                port=self._requested_port,
                registry=self._registry,
                bind=self._bind,
                startup_timeout=self._startup_timeout,
            )
        self._session._start()  # noqa: SLF001
        self._port = self._session.port
        if self._mode == 'foreground':
            register_foreground_handle(self)
        return self

    def stop(self) -> None:
        if self._session is None:
            return
        if self._mode == 'foreground':
            unregister_foreground_handle(self)
        self._session._stop()  # noqa: SLF001
        self._session = None
        self._port = None

    def url(self) -> str:
        self._require_started()
        result = instances.url(self._port)
        assert isinstance(result, str)
        return result

    def port(self) -> int:
        self._require_started()
        result = instances.port(self._port)
        assert isinstance(result, int)
        return result

    def status(
        self,
        *,
        format: StatusFormat = 'data',
        file: TextIO | None = None,
    ) -> dict | str | None:
        self._require_started()
        return instances.status(self._port, format=format, file=file)

    def erase_flash(self) -> int:
        self._require_started()
        erased = instances.erase_flash(self._port)
        return erased[0]

    def __enter__(self) -> MockHandle:
        if self._session is None or not self._session._started:  # noqa: SLF001
            self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def __del__(self) -> None:
        if self._mode == 'foreground':
            try:
                self.stop()
            except Exception:
                pass


def mock_bootloader(
    chip: str = 'auto',
    *,
    port: int | None = None,
    mode: Mode = 'foreground',
    exit_on_disconnect: bool = False,
    timeout: float | None = None,
    startup_timeout: float = daemon.DEFAULT_STARTUP_TIMEOUT,
    autostart: bool = True,
    registry: Registry | None = None,
) -> MockHandle:
    """Create and optionally start a mock bootloader instance.

    Default mode is **foreground**: a subprocess server that stops automatically
    when the handle is garbage-collected or used as a context manager. Pass
    ``mode="daemon"`` for a background daemon (CLI ``start`` equivalent).
    """
    return MockHandle(
        chip=chip,
        port=port,
        mode=mode,
        exit_on_disconnect=exit_on_disconnect,
        timeout=timeout,
        startup_timeout=startup_timeout,
        autostart=autostart,
        registry=registry,
    )


__all__ = ['MockHandle', 'mock_bootloader']
