# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Internal session lifecycle — not part of the public API."""

from __future__ import annotations

import subprocess
import tempfile
import time
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Iterator

from esp32_mock_bootloader import daemon
from esp32_mock_bootloader.client import Client, Transport
from esp32_mock_bootloader import process
from esp32_mock_bootloader import transport as transport_mod
from esp32_mock_bootloader.registry import Registry


class Session:
    """One mock bootloader instance (foreground subprocess or daemon)."""

    def __init__(
        self,
        chip: str = 'auto',
        port: int | None = None,
        *,
        mode: str = 'foreground',
        registry: Registry | None = None,
        bind: str = daemon.DEFAULT_BIND,
        startup_timeout: float = process.DEFAULT_STARTUP_TIMEOUT,
        exit_on_disconnect: bool = True,
        timeout: float | None = 10.0,
        _private_registry: Registry | None = None,
    ) -> None:
        self.chip = chip
        self._port = port
        self._mode = mode
        self._registry = registry
        self._private_registry = _private_registry
        self._bind = bind
        self._startup_timeout = startup_timeout
        self._exit_on_disconnect = exit_on_disconnect
        self._timeout = timeout
        self._proc: subprocess.Popen[bytes] | None = None
        self._daemon_data: dict[str, Any] | None = None
        self._started = False
        self._client: Client | None = None
        self._tracked: set[Transport] = set()

    @classmethod
    def foreground(
        cls,
        chip: str = 'auto',
        port: int | None = None,
        *,
        exit_on_disconnect: bool = True,
        timeout: float | None = 10.0,
        startup_timeout: float = process.DEFAULT_STARTUP_TIMEOUT,
    ) -> Session:
        return cls(
            chip=chip,
            port=port,
            mode='foreground',
            exit_on_disconnect=exit_on_disconnect,
            timeout=timeout,
            startup_timeout=startup_timeout,
        )

    @classmethod
    def daemon(
        cls,
        chip: str = 'auto',
        port: int | None = None,
        *,
        registry: Registry | None = None,
        bind: str = daemon.DEFAULT_BIND,
        startup_timeout: float = daemon.DEFAULT_STARTUP_TIMEOUT,
    ) -> Session:
        return cls(
            chip=chip,
            port=port,
            mode='daemon',
            registry=registry,
            bind=bind,
            startup_timeout=startup_timeout,
            exit_on_disconnect=False,
            timeout=None,
        )

    def _registry_base(self) -> Path | None:
        if self._registry is not None:
            return self._registry.path
        if self._private_registry is not None:
            return self._private_registry.path
        return None

    def _start(self) -> Session:
        if self._started:
            return self
        if self._mode == 'foreground':
            self._proc, self._port = process.start_server(
                self._port,
                timeout=self._timeout,
                chip=self.chip,
                exit_on_disconnect=self._exit_on_disconnect,
                startup_timeout=self._startup_timeout,
            )
        elif self._mode == 'daemon':
            if self._port is None:
                self._port = process.reserve_tcp_port()
            if self._registry is None and self._private_registry is None:
                self._private_registry = Registry(
                    Path(tempfile.mkdtemp(prefix='emb-session-')),
                    _owns_base=True,
                )
            base = self._registry_base()
            self._daemon_data = daemon.start_daemon(
                port=self._port,
                chip_mode=self.chip,
                startup_timeout=self._startup_timeout,
                bind=self._bind,
                base=base,
                force=True,
            )
            self._wait_daemon_ready(base)
        else:
            raise ValueError(f'unknown session mode: {self._mode!r}')
        self._started = True
        reg = self._registry or self._private_registry
        if reg is not None:
            reg.register_session(self)
        return self

    def _wait_daemon_ready(self, base: Path | None, *, deadline: float = 15.0) -> None:
        end = time.time() + deadline
        while time.time() < end:
            running = daemon.list_running_daemons(base)
            if any(int(info['port']) == self._port for info in running):
                return
            registered = daemon.load_registry(base).get('instances', {})
            if str(self._port) in registered:
                time.sleep(0.15)
                running = daemon.list_running_daemons(base)
                if any(int(info['port']) == self._port for info in running):
                    return
            time.sleep(0.05)
        raise RuntimeError(f'daemon on port {self._port} did not become ready')

    def _stop(self) -> None:
        if not self._started:
            return
        self._close_all_transports()
        if self._client is not None:
            self._client._close_transport()
        reg = self._registry or self._private_registry
        if reg is not None:
            reg.unregister_session(self)
        if self._mode == 'foreground' and self._proc is not None:
            process.stop_subprocess(self._proc)
            self._proc = None
        elif self._mode == 'daemon' and self._port is not None:
            daemon.stop_daemon(self._port, self._registry_base())
            self._daemon_data = None
        if self._private_registry is not None:
            self._private_registry._cleanup()
            self._private_registry = None
        self._started = False

    def _open_transport(self) -> Transport:
        handle = transport_mod.connect(self.port)
        self._tracked.add(handle)
        return handle

    def _untrack_transport(self, transport: Transport) -> None:
        self._tracked.discard(transport)

    def _close_all_transports(self) -> None:
        for transport in list(self._tracked):
            try:
                transport.close()
            except OSError:
                pass
        self._tracked.clear()

    @property
    def running(self) -> bool:
        if not self._started:
            return False
        if self._mode == 'foreground':
            return self._proc is not None and self._proc.poll() is None
        state = daemon.read_state(self.port, self._registry_base())
        if not state:
            return False
        return daemon.is_pid_running(int(state.get('pid', 0)))

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError('Session is not started')
        return self._port

    @property
    def url(self) -> str:
        if self._daemon_data is not None:
            return str(self._daemon_data['url'])
        return daemon.socket_url(self.port, self._bind)

    @property
    def returncode(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.poll()

    @property
    def proc(self) -> subprocess.Popen[bytes] | None:
        return self._proc

    @property
    def detected_chip(self) -> str | None:
        state = daemon.read_state(self.port, self._registry_base())
        if not state:
            return None
        detected = state.get('detected_chip')
        return str(detected) if detected else None

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(self)
        return self._client

    def __enter__(self) -> Session:
        return self._start()

    def __exit__(self, *_exc: object) -> None:
        self._stop()


class SessionGroup:
    """Internal coordinator for multiple sessions sharing one registry."""

    def __init__(self, registry: Registry | None = None) -> None:
        self._registry = registry
        self._registry_cm: AbstractContextManager[Registry] | None = None
        self._sessions: list[Session] = []

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            raise RuntimeError('SessionGroup is not entered')
        return self._registry

    def add_daemon(
        self,
        chip: str = 'auto',
        port: int | None = None,
        *,
        bind: str = daemon.DEFAULT_BIND,
        startup_timeout: float = daemon.DEFAULT_STARTUP_TIMEOUT,
    ) -> Session:
        session = Session.daemon(
            chip=chip,
            port=port,
            registry=self._registry,
            bind=bind,
            startup_timeout=startup_timeout,
        )
        session._start()
        self._sessions.append(session)
        return session

    def add_foreground(
        self,
        chip: str = 'auto',
        port: int | None = None,
        *,
        exit_on_disconnect: bool = True,
        timeout: float | None = 10.0,
        startup_timeout: float = process.DEFAULT_STARTUP_TIMEOUT,
    ) -> Session:
        session = Session.foreground(
            chip=chip,
            port=port,
            exit_on_disconnect=exit_on_disconnect,
            timeout=timeout,
            startup_timeout=startup_timeout,
        )
        session._start()
        self._registry.register_session(session)
        self._sessions.append(session)
        return session

    def _stop_all(self) -> None:
        for session in reversed(self._sessions):
            session._stop()
        self._sessions.clear()

    def __enter__(self) -> SessionGroup:
        if self._registry is None:
            self._registry_cm = Registry.temp()
            self._registry = self._registry_cm.__enter__()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop_all()
        if self._registry_cm is not None:
            self._registry_cm.__exit__(None, None, None)
            self._registry_cm = None
            self._registry = None


@contextmanager
def running_server(
    chip: str,
    *,
    port: int | None = None,
    timeout: float | None = 30.0,
    exit_on_disconnect: bool = False,
) -> Iterator[Session]:
    with Session.foreground(
        chip=chip,
        port=port,
        timeout=timeout,
        exit_on_disconnect=exit_on_disconnect,
    ) as session:
        yield session
