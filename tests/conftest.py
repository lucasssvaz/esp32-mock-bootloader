# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Shared pytest fixtures for mock bootloader tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from esp32_mock_bootloader import chips, daemon, server
from esp32_mock_bootloader.testing import esptool
from esp32_mock_bootloader.testing import server as testing_server

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
def registry_root(monkeypatch: pytest.MonkeyPatch, _xdist_worker_id: str) -> Path:
    """Isolated registry under the OS temp dir (never under the repo)."""
    root = Path(tempfile.mkdtemp(prefix=f'emb-{_xdist_worker_id}-'))
    monkeypatch.setenv('ESP32_MOCK_BOOTLOADER_STATE_DIR', os.fspath(root))
    yield root
    _stop_registered_daemons()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_registry(registry_root: Path) -> None:
    """Registry path is set by the registry_root fixture."""
    _ = registry_root


@pytest.fixture
def cli(registry_root: Path):
    """Run the CLI in a subprocess with the isolated registry env."""

    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env['ESP32_MOCK_BOOTLOADER_STATE_DIR'] = os.fspath(registry_root)
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
                # Registry updated before the process is observable as running.
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
def _stop_leaked_daemons(registry_root: Path) -> None:
    """Ensure background daemons from a failed test do not affect the next one."""
    _ = registry_root
    yield
    _stop_registered_daemons()


@pytest.fixture
def mock_server(reference_chip):
    """One-shot TCP server: exits when the client disconnects (like CI upload tests)."""
    proc, port = testing_server.start_server(
        chip=reference_chip,
        timeout=None,
        exit_on_disconnect=True,
    )
    try:
        yield port, proc
    finally:
        testing_server.stop_subprocess(proc)


@pytest.fixture
def mock_server_persistent(reference_chip):
    """TCP server that stays up across multiple client connections."""
    proc, port = testing_server.start_server(
        chip=reference_chip,
        timeout=30.0,
        exit_on_disconnect=False,
    )
    try:
        yield port, proc
    finally:
        testing_server.stop_subprocess(proc)


@pytest.fixture
def esptool_port(reference_chip):
    """Mock server that stays up across multiple esptool invocations."""
    proc, port = testing_server.start_server(
        timeout=60.0,
        chip=reference_chip,
        exit_on_disconnect=False,
    )
    try:
        yield port
    finally:
        testing_server.stop_subprocess(proc)


@pytest.fixture(autouse=True)
def _reset_reference_chip_cache() -> None:
    chips.reference_chip.cache_clear()
    yield
    chips.reference_chip.cache_clear()


@pytest.fixture(autouse=True)
def _reset_mock_flash() -> None:
    """Isolate in-process unit tests; subprocess servers keep their own flash."""
    server.reset_flash_image()
    yield
    server.reset_flash_image()


@pytest.fixture
def reference_chip() -> str:
    return chips.reference_chip()


@pytest.fixture(scope='session', autouse=True)
def _log_esptool_version() -> None:
    if not esptool.esptool_available():
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
    config.addinivalue_line(
        'markers',
        'com0com: Windows com0com integration (requires admin + setupc)',
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if esptool.esptool_available():
        return
    skip = pytest.mark.skip(reason='esptool not installed')
    for item in items:
        if 'esptool' in item.keywords:
            item.add_marker(skip)
