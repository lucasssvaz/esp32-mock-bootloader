# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Daemon control unit and integration tests."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from esp32_mock_bootloader import daemon

from conftest import reserve_tcp_port


def test_socket_url_wildcard_bind():
    assert daemon.socket_url(9876, '0.0.0.0') == 'socket://127.0.0.1:9876'
    assert daemon.socket_url(9876, '::') == 'socket://127.0.0.1:9876'


def test_read_state_corrupt_json(tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    path = daemon.state_path(39900, state_dir)
    path.write_text('{not json', encoding='utf-8')
    assert daemon.read_state(39900, state_dir) is None


def test_stop_daemon_when_not_running(tmp_path):
    state_dir = tmp_path / 'state'
    assert daemon.stop_daemon(39901, state_dir) is False


def test_stop_daemon_stale_pid(tmp_path):
    state_dir = tmp_path / 'state'
    daemon.write_state(39902, {'pid': 999999, 'port': 39902}, state_dir)
    assert daemon.stop_daemon(39902, state_dir) is True
    assert daemon.read_state(39902, state_dir) is None


def test_start_force_replaces_daemon(tmp_path):
    port = reserve_tcp_port()
    state_dir = tmp_path / 'state'
    try:
        first = daemon.start_daemon(port=port, chip_mode='auto', base=state_dir)
        second = daemon.start_daemon(
            port=port, chip_mode='esp32', base=state_dir, force=True,
        )
        assert first['pid'] != second['pid']
        status = daemon.daemon_status(port, state_dir)
        assert status['running']
        assert status['chip'] == 'esp32'
    finally:
        daemon.stop_daemon(port, state_dir)


def test_daemon_status_stopped(tmp_path):
    state_dir = tmp_path / 'state'
    info = daemon.daemon_status(39904, state_dir)
    assert info['running'] is False
    assert info['pid'] is None


def test_is_pid_running_invalid():
    assert daemon.is_pid_running(0) is False
    assert daemon.is_pid_running(-1) is False


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-m', 'esp32_mock_bootloader.cli', *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_start_force(tmp_path):
    port = reserve_tcp_port()
    state_dir = tmp_path / 'state'
    try:
        assert _cli(
            'start', '--port', str(port), '--chip', 'auto',
            '--state-dir', str(state_dir),
        ).returncode == 0
        replaced = _cli(
            'start', '--port', str(port), '--chip', 'esp32',
            '--state-dir', str(state_dir), '--force',
        )
        assert replaced.returncode == 0, replaced.stderr
        status = json.loads(_cli(
            'status', '--port', str(port), '--state-dir', str(state_dir), '--json',
        ).stdout)
        assert status['chip'] == 'esp32'
    finally:
        daemon.stop_daemon(port, state_dir)
