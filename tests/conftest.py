# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Shared pytest fixtures for mock bootloader tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from esp32_mock_bootloader import chips
from esp32_mock_bootloader.testing import esptool, server


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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if esptool.esptool_available():
        return
    skip = pytest.mark.skip(reason='esptool not installed')
    for item in items:
        if 'esptool' in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def mock_server(reference_chip):
    """Yield (port, proc) for a mock server on a unique port."""
    proc, port = server.start_server(chip=reference_chip)
    try:
        yield port, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.fixture
def esptool_port(reference_chip):
    proc, port = server.start_server(timeout=30.0, chip=reference_chip)
    try:
        yield port
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
