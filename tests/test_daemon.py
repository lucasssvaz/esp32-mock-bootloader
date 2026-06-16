# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Daemon control unit and integration tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

import pytest

from esp32_mock_bootloader import daemon

import esp32_mock_bootloader.testing as mock


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
    port = mock.server.reserve_tcp_port()
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


def test_is_pid_running_windows_branch(monkeypatch):
    import ctypes
    import os

    monkeypatch.setattr(os, 'name', 'nt')

    class FakeKernel32:
        STILL_ACTIVE = 259

        def OpenProcess(self, _access, _inherit, pid):
            return 1 if pid == 42 else 0

        def GetExitCodeProcess(self, _handle, exit_code):
            ctypes.cast(exit_code, ctypes.POINTER(ctypes.c_ulong)).contents.value = self.STILL_ACTIVE
            return 1

        def CloseHandle(self, _handle):
            return 1

    fake = type('Windll', (), {'kernel32': FakeKernel32()})()
    monkeypatch.setattr(ctypes, 'windll', fake, raising=False)
    assert daemon.is_pid_running(42) is True
    assert daemon.is_pid_running(99) is False


def test_wait_for_port_timeout():
    assert daemon.wait_for_port(59999, timeout=0.2) is False


def test_remove_state_missing_file(tmp_path):
    daemon.remove_state(39999, tmp_path / 'state')


def test_remove_state_os_error(monkeypatch, tmp_path):
    state_dir = tmp_path / 'state'
    path = daemon.state_path(39998, state_dir)
    path.parent.mkdir(parents=True)
    path.write_text('{}', encoding='utf-8')

    def boom(_self):
        raise OSError('denied')

    monkeypatch.setattr(type(path), 'unlink', boom, raising=False)
    daemon.remove_state(39998, state_dir)


def test_start_daemon_exits_during_startup(tmp_path, monkeypatch):
    port = mock.server.reserve_tcp_port()
    state_dir = tmp_path / 'state'

    class DeadProc:
        pid = 424242

        def poll(self):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(daemon, '_spawn_detached', lambda *_a, **_k: DeadProc())
    monkeypatch.setattr(daemon, 'wait_for_port', lambda *_a, **_k: False)
    with pytest.raises(RuntimeError, match='exited during startup'):
        daemon.start_daemon(port=port, chip_mode='auto', base=state_dir, startup_timeout=0.5)


def test_start_daemon_hang_terminates(tmp_path, monkeypatch):
    port = mock.server.reserve_tcp_port()
    state_dir = tmp_path / 'state'

    class HangProc:
        pid = 424243
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    proc = HangProc()
    monkeypatch.setattr(daemon, '_spawn_detached', lambda *_a, **_k: proc)
    monkeypatch.setattr(daemon, 'wait_for_port', lambda *_a, **_k: False)
    with pytest.raises(RuntimeError, match='did not open port'):
        daemon.start_daemon(port=port, chip_mode='auto', base=state_dir, startup_timeout=0.1)
    assert proc.terminated


def test_spawn_detached_windows_flags(monkeypatch, tmp_path):
    import pathlib

    monkeypatch.setattr(daemon, 'Path', pathlib.PosixPath)
    monkeypatch.setattr(os, 'name', 'nt')
    monkeypatch.setattr(daemon.subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, raising=False)
    monkeypatch.setattr(daemon.subprocess, 'DETACHED_PROCESS', 0x8, raising=False)
    captured: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)

        class _Proc:
            pid = 1

        return _Proc()

    monkeypatch.setattr(daemon.subprocess, 'Popen', fake_popen)
    daemon._spawn_detached([sys.executable, '-c', 'pass'], tmp_path / 'log.txt')
    assert 'creationflags' in captured


def test_stop_daemon_windows_taskkill(tmp_path, monkeypatch):
    import pathlib

    monkeypatch.setattr(daemon, 'Path', pathlib.PosixPath)
    monkeypatch.setattr(os, 'name', 'nt')
    state_dir = tmp_path / 'state'
    daemon.write_state(39905, {'pid': 424244}, state_dir)
    monkeypatch.setattr(daemon, 'is_pid_running', lambda pid: pid == 424244)
    calls: list[list[str]] = []

    def fake_run(cmd, check=False):
        calls.append(list(cmd))

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert daemon.stop_daemon(39905, state_dir) is True
    assert calls and calls[0][:2] == ['taskkill', '/PID']


def test_stop_daemon_sends_sigterm(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    pid = 424245
    daemon.write_state(39906, {'pid': pid, 'port': 39906}, state_dir)
    signals: list[tuple[int, int]] = []
    checks = {'count': 0}

    def fake_is_running(p: int) -> bool:
        return p == pid and checks['count'] == 0

    def fake_kill(p: int, sig: int) -> None:
        signals.append((p, sig))
        checks['count'] += 1

    monkeypatch.setattr(daemon, 'is_pid_running', fake_is_running)
    monkeypatch.setattr(os, 'kill', fake_kill)
    assert daemon.stop_daemon(39906, state_dir) is True
    assert signals == [(pid, signal.SIGTERM)]


def test_cli_run_pty_smoke(tmp_path):
    import time

    path_file = tmp_path / 'pty.path'
    proc = subprocess.Popen(
        [
            sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
            '--pty', '--pty-path-file', str(path_file),
            '--chip', 'esp32',
            '--timeout', '30',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if path_file.is_file() and path_file.read_text(encoding='ascii').strip():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert path_file.is_file()
        assert path_file.read_text(encoding='ascii').strip()
    finally:
        if proc.poll() is None:
            mock.server.stop_subprocess(proc)



def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-m', 'esp32_mock_bootloader.cli', *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_start_force(tmp_path):
    port = mock.server.reserve_tcp_port()
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
