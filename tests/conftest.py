# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Shared pytest fixtures for mock bootloader tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from esp32_mock_bootloader import chips, daemon, instances, mock_bootloader, server
from esp32_mock_bootloader.registry import Registry
from esp32_mock_bootloader.session import SessionGroup

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _runtime_leaks_under(root: Path) -> list[Path]:
    leaked: list[Path] = []
    for name in ('private', 'state'):
        path = root / name
        if path.exists():
            leaked.append(path)
    return leaked


def _stop_registered_daemons() -> None:
    """Stop every daemon listed in the current registry."""
    if not daemon.load_registry().get('instances'):
        return
    for info in list(daemon.list_running_daemons()):
        daemon.stop_daemon(int(info['port']))


@pytest.fixture(scope='session')
def _xdist_worker_id(request: pytest.FixtureRequest) -> str:
    """pytest-xdist worker id ('gw0', …) or 'master' when running serially."""
    workerinput = getattr(request.config, 'workerinput', None)
    if isinstance(workerinput, dict):
        return str(workerinput.get('workerid', 'master'))
    return 'master'


@pytest.fixture
def registry_root(monkeypatch: pytest.MonkeyPatch, _xdist_worker_id: str) -> Registry:
    """Isolated registry under the OS temp dir (never under the repo)."""
    with Registry.temp(worker_id=_xdist_worker_id) as reg:
        reg.install_env(monkeypatch)
        yield reg


@pytest.fixture(autouse=True)
def _isolated_registry(registry_root: Registry) -> None:
    """Registry path is set by the registry_root fixture."""
    _ = registry_root


@pytest.fixture
def cli(registry_root: Registry):
    """Run the CLI in a subprocess with the isolated registry env."""

    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env['ESP32_MOCK_BOOTLOADER_STATE_DIR'] = os.fspath(registry_root.path)
        return subprocess.run(
            [sys.executable, '-m', 'esp32_mock_bootloader.cli', *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=_PROJECT_ROOT,
        )

    return _run


@pytest.fixture(autouse=True)
def _no_runtime_leaks_under_project_root() -> None:
    """Fail if tests create registry dirs under the repo."""
    before = _runtime_leaks_under(_PROJECT_ROOT)
    yield
    after = _runtime_leaks_under(_PROJECT_ROOT)
    leaked = [p for p in after if p not in before]
    assert not leaked, f'runtime files leaked under {_PROJECT_ROOT}: {leaked}'


@pytest.fixture
def wait_for_daemons():
    """Poll until the expected number of daemons are registered and running."""

    import time

    def _wait(count: int, timeout: float = 15.0) -> list[dict]:
        deadline = time.time() + timeout
        last: list[dict] = []
        while time.time() < deadline:
            last = daemon.list_running_daemons()
            if len(last) >= count:
                return last
            registered = len(daemon.load_registry().get('instances', {}))
            if registered >= count and len(last) < count:
                time.sleep(0.15)
                last = daemon.list_running_daemons()
                if len(last) >= count:
                    return last
            time.sleep(0.05)
        raise AssertionError(
            f'expected >={count} running daemon(s), got {len(last)}: {last}',
        )

    return _wait


@pytest.fixture(autouse=True)
def _stop_leaked_daemons(registry_root: Registry) -> None:
    """Ensure background daemons from a failed test do not affect the next one."""
    _ = registry_root
    yield
    _stop_registered_daemons()


@pytest.fixture
def mock_server(reference_chip):
    """One-shot TCP server: exits when the client disconnects (like CI upload tests)."""
    with mock_bootloader(
        chip=reference_chip,
        mode='foreground',
        timeout=None,
        exit_on_disconnect=True,
        startup_timeout=30.0,
    ) as mock:
        yield mock


@pytest.fixture
def mock_server_persistent(reference_chip):
    """TCP server that stays up across multiple client connections."""
    with mock_bootloader(
        chip=reference_chip,
        mode='foreground',
        timeout=30.0,
        exit_on_disconnect=False,
        startup_timeout=30.0,
    ) as mock:
        yield mock


@pytest.fixture
def esptool_port(reference_chip):
    """Mock server that stays up across multiple esptool invocations."""
    with mock_bootloader(
        timeout=60.0,
        chip=reference_chip,
        mode='foreground',
        exit_on_disconnect=False,
    ) as mock:
        yield mock


@pytest.fixture
def session_group(registry_root: Registry):
    """Internal multi-instance group under the isolated registry (CLI subprocess tests)."""
    with SessionGroup(registry_root) as group:
        yield group


@pytest.fixture(autouse=True)
def _reset_reference_chip_cache() -> None:
    chips.reference_chip.cache_clear()
    yield
    chips.reference_chip.cache_clear()


@pytest.fixture(scope='session')
def reference_chip() -> str:
    return chips.reference_chip()


@pytest.fixture(scope='session')
def esptool_available() -> bool:
    from tests.helpers import esptool as esptool_helpers

    if not esptool_helpers.esptool_available():
        pytest.skip('esptool not available')
    return True
