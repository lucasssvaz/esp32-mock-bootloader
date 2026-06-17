# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Verify every example script runs."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = PROJECT_ROOT / 'examples'

EXAMPLE_MODULES: tuple[str, ...] = (
    'examples.basic.mock_endpoint',
    'examples.basic.esptool_upload',
    'examples.basic.context_manager',
    'examples.basic.two_mocks',
    'examples.advanced.daemon_vs_foreground',
    'examples.advanced.protocol_chip_detect',
    'examples.advanced.protocol_flash_write',
    'examples.advanced.transport_tcp_connect',
    'examples.advanced.handle_advanced_raw_slip',
    'examples.advanced.verify_upload_with_protocol',
)

ESPTOOL_EXAMPLES = frozenset({
    'examples.basic.esptool_upload',
    'examples.basic.context_manager',
    'examples.basic.two_mocks',
    'examples.advanced.verify_upload_with_protocol',
})


def _discovered_example_scripts() -> list[Path]:
    return [
        p for p in sorted(EXAMPLES_ROOT.rglob('*.py'))
        if p.name != '__init__.py'
    ]


def test_example_tree_matches_registry():
    discovered = {
        p.relative_to(PROJECT_ROOT).with_suffix('').as_posix().replace('/', '.')
        for p in _discovered_example_scripts()
    }
    assert discovered == set(EXAMPLE_MODULES)


@pytest.mark.parametrize(
    'module_path',
    tuple(m for m in EXAMPLE_MODULES if m not in ESPTOOL_EXAMPLES),
)
def test_example_run(module_path: str):
    mod = importlib.import_module(module_path)
    result = mod.run()
    assert result is not None


@pytest.mark.esptool
@pytest.mark.parametrize('module_path', sorted(ESPTOOL_EXAMPLES))
def test_example_run_esptool(module_path: str, esptool_available):
    mod = importlib.import_module(module_path)
    result = mod.run()
    assert result is not None


def test_mock_endpoint_returns_socket_url():
    from examples.basic import mock_endpoint

    info = mock_endpoint.run()
    assert info['running'] is True
    assert str(info['url']).startswith('socket://')


@pytest.mark.esptool
def test_esptool_upload_example(esptool_available):
    from examples.basic import esptool_upload

    assert esptool_upload.run()['ok'] is True


@pytest.mark.esptool
def test_context_manager_example(esptool_available):
    from examples.basic import context_manager

    assert context_manager.run()['ok'] is True


@pytest.mark.esptool
def test_two_mocks_example(esptool_available):
    from examples.basic import two_mocks

    assert two_mocks.run()['ok'] is True


def test_daemon_vs_foreground_example():
    from examples.advanced import daemon_vs_foreground

    info = daemon_vs_foreground.run()
    assert info['both_socket_urls'] is True


def test_protocol_chip_detect_example():
    from examples.advanced import protocol_chip_detect

    info = protocol_chip_detect.run(chip='esp32')
    assert info['matches'] is True


def test_protocol_flash_write_example():
    from examples.advanced import protocol_flash_write

    assert protocol_flash_write.run()['bytes_match'] is True


def test_transport_tcp_connect_example():
    from examples.advanced import transport_tcp_connect

    assert transport_tcp_connect.run()['matches'] is True


def test_handle_advanced_raw_slip_example():
    from examples.advanced import handle_advanced_raw_slip

    assert handle_advanced_raw_slip.run()['ok'] is True


@pytest.mark.esptool
def test_verify_upload_with_protocol_example(esptool_available):
    from examples.advanced import verify_upload_with_protocol

    assert verify_upload_with_protocol.run()['ok'] is True


@pytest.mark.parametrize('script', _discovered_example_scripts(), ids=lambda p: p.name)
def test_example_main_subprocess(script: Path, esptool_available, request: pytest.FixtureRequest):
    if script.name in {
        'esptool_upload.py',
        'context_manager.py',
        'two_mocks.py',
        'verify_upload_with_protocol.py',
    }:
        request.applymarker(pytest.mark.esptool)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f'{script.name} rc={proc.returncode}\nstderr:\n{proc.stderr[-500:]}'
    )
