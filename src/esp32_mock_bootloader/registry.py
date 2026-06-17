# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Runtime registry for multi-instance mock bootloader coordination."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from esp32_mock_bootloader import daemon

if TYPE_CHECKING:
    from esp32_mock_bootloader.session import Session


class Registry:
    """Owns a runtime directory and coordinates child session teardown."""

    def __init__(
        self,
        base: Path | None = None,
        *,
        _owns_base: bool = False,
        worker_id: str | None = None,
    ) -> None:
        self._base = base
        self._owns_base = _owns_base
        self._worker_id = worker_id
        self._sessions: list[Session] = []

    @classmethod
    @contextmanager
    def temp(cls, *, worker_id: str | None = None) -> Iterator[Registry]:
        """Create an isolated registry directory; remove it on exit."""
        prefix = f'emb-{worker_id or "master"}-'
        root = Path(tempfile.mkdtemp(prefix=prefix))
        reg = cls(base=root, _owns_base=True, worker_id=worker_id)
        try:
            yield reg
        finally:
            reg._cleanup()

    @property
    def path(self) -> Path:
        return daemon.runtime_dir(self._base)

    def install_env(self, monkeypatch: object) -> None:
        """Point CLI subprocesses at this registry via environment."""
        setenv = getattr(monkeypatch, 'setenv')
        setenv('ESP32_MOCK_BOOTLOADER_STATE_DIR', os.fspath(self.path))

    def register_session(self, session: Session) -> None:
        if session not in self._sessions:
            self._sessions.append(session)

    def unregister_session(self, session: Session) -> None:
        if session in self._sessions:
            self._sessions.remove(session)

    def stop_all(self) -> list[int]:
        """Stop tracked sessions (reverse order) and any registry daemons."""
        ports: list[int] = []
        for session in reversed(list(self._sessions)):
            if session.running:
                ports.append(session.port)
            session._stop()
        self._sessions.clear()
        for info in list(daemon.list_running_daemons(self._base)):
            port = int(info['port'])
            daemon.stop_daemon(port, self._base)
            ports.append(port)
        return ports

    def _cleanup(self) -> None:
        self.stop_all()
        if self._owns_base and self._base is not None:
            shutil.rmtree(self._base, ignore_errors=True)

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._cleanup()


__all__ = ['Registry']
