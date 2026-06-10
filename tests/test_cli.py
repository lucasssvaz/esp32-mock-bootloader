# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""CLI daemon start/stop/status round-trip tests."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from esp32_mock_bootloader import daemon
from esp32_mock_bootloader.chip_profiles import SUPPORTED_CHIPS

from conftest import reserve_tcp_port


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-m', 'esp32_mock_bootloader.cli', *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_url_default():
    result = _cli('url', '--port', '9876')
    assert result.returncode == 0
    assert result.stdout.strip() == 'socket://127.0.0.1:9876'


def test_cli_lists_supported_chips():
    result = _cli('chips')
    assert result.returncode == 0
    for chip in SUPPORTED_CHIPS:
        assert chip in result.stdout


def test_cli_chips_json():
    result = _cli('chips', '--json')
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == set(SUPPORTED_CHIPS)
    for chip in SUPPORTED_CHIPS:
        assert 'detect_reg' in payload[chip]


def test_cli_status_human_readable(tmp_path):
    port = reserve_tcp_port()
    state_dir = tmp_path / 'daemon-state'
    try:
        start = _cli(
            'start', '--port', str(port), '--chip', 'auto',
            '--state-dir', str(state_dir),
        )
        assert start.returncode == 0
        status = _cli('status', '--port', str(port), '--state-dir', str(state_dir))
        assert status.returncode == 0
        assert 'status: running' in status.stdout
        assert f'port: {port}' in status.stdout
        assert 'url: socket://' in status.stdout
    finally:
        daemon.stop_daemon(port, state_dir)


def test_cli_status_stopped_exit_code(tmp_path):
    state_dir = tmp_path / 'daemon-state'
    result = _cli('status', '--port', '39703', '--state-dir', str(state_dir))
    assert result.returncode == 1
    assert 'status: stopped' in result.stdout


def test_start_status_stop_round_trip(tmp_path):
    port = reserve_tcp_port()
    state_dir = tmp_path / 'daemon-state'
    chip = SUPPORTED_CHIPS[0]

    start = _cli(
        'start', '--port', str(port), '--chip', chip,
        '--state-dir', str(state_dir),
    )
    assert start.returncode == 0, start.stderr

    status = _cli('status', '--port', str(port), '--state-dir', str(state_dir), '--json')
    assert status.returncode == 0, status.stderr
    info = json.loads(status.stdout)
    assert info['running'] is True
    assert info['chip'] == chip
    assert info['url'] == f'socket://127.0.0.1:{port}'

    url = _cli('url', '--port', str(port), '--state-dir', str(state_dir))
    assert url.returncode == 0
    assert url.stdout.strip() == f'socket://127.0.0.1:{port}'

    stop = _cli('stop', '--port', str(port), '--state-dir', str(state_dir))
    assert stop.returncode == 0

    after = _cli('status', '--port', str(port), '--state-dir', str(state_dir))
    assert after.returncode == 1
    assert daemon.read_state(port, state_dir) is None


def test_start_refuses_double_start(tmp_path):
    port = reserve_tcp_port()
    state_dir = tmp_path / 'daemon-state'
    try:
        first = _cli(
            'start', '--port', str(port), '--chip', 'auto',
            '--state-dir', str(state_dir),
        )
        assert first.returncode == 0
        second = _cli(
            'start', '--port', str(port), '--chip', 'auto',
            '--state-dir', str(state_dir),
        )
        assert second.returncode != 0
        assert 'already running' in second.stderr
    finally:
        daemon.stop_daemon(port, state_dir)
