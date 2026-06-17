# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Daemon control unit and integration tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from esp32_mock_bootloader import daemon

from esp32_mock_bootloader import constants, process, protocol_client
from esp32_mock_bootloader.registry import Registry
from tests.helpers import esptool


def test_socket_url_wildcard_bind():
    assert daemon.socket_url(9876, '0.0.0.0') == 'socket://127.0.0.1:9876'
    assert daemon.socket_url(9876, '::') == 'socket://127.0.0.1:9876'


def test_read_state_corrupt_json():
    path = daemon.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{not json', encoding='utf-8')
    assert daemon.read_state(39900) is None


def test_stop_daemon_when_not_running():
    assert daemon.stop_daemon(39901) is False


def test_stop_daemon_stale_pid():
    daemon.write_state(39902, {'pid': 999999, 'port': 39902})
    assert daemon.stop_daemon(39902) is True
    assert daemon.read_state(39902) is None


def test_start_force_replaces_daemon():
    port = process.reserve_tcp_port()
    try:
        first = daemon.start_daemon(port=port, chip_mode='auto')
        second = daemon.start_daemon(port=port, chip_mode='esp32', force=True)
        assert first['pid'] != second['pid']
        status = daemon.daemon_status(port)
        assert status['running']
        assert status['chip'] == 'esp32'
    finally:
        daemon.stop_daemon(port)


def test_daemon_status_stopped():
    info = daemon.daemon_status(39904)
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


def test_remove_state_missing_instance():
    daemon.remove_state(39999)


def test_unregister_instance_is_idempotent():
    daemon.unregister_instance(39998)
    daemon.unregister_instance(39998)


def test_start_daemon_exits_during_startup(monkeypatch):
    port = process.reserve_tcp_port()

    class DeadProc:
        pid = 424242

        def poll(self):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(daemon, '_spawn_detached', lambda *_a, **_k: DeadProc())
    monkeypatch.setattr(daemon, 'wait_for_port', lambda *_a, **_k: False)
    with pytest.raises(RuntimeError, match='exited during startup'):
        daemon.start_daemon(port=port, chip_mode='auto', startup_timeout=0.5)


def test_start_daemon_hang_terminates(monkeypatch):
    port = process.reserve_tcp_port()

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
        daemon.start_daemon(port=port, chip_mode='auto', startup_timeout=0.1)
    assert proc.terminated


def test_terminate_pid_windows_uses_taskkill(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, check=False):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(daemon.os, 'name', 'nt', raising=False)
    monkeypatch.setattr(daemon.subprocess, 'run', fake_run)
    daemon._terminate_pid(424244)
    assert calls and calls[0][:2] == ['taskkill', '/PID']


def test_spawn_detached_windows_flags(monkeypatch, tmp_path):
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


def test_stop_daemon_windows_taskkill(monkeypatch):
    daemon.write_state(39905, {'pid': 424244, 'port': 39905})
    monkeypatch.setattr(daemon, 'is_pid_running', lambda pid: pid == 424244)
    calls: list[int] = []
    monkeypatch.setattr(daemon, '_terminate_pid', lambda pid: calls.append(pid))
    assert daemon.stop_daemon(39905) is True
    assert calls == [424244]


@pytest.mark.skipif(os.name == 'nt', reason='SIGTERM via os.kill is Unix-only; Windows uses taskkill')
def test_stop_daemon_sends_sigterm(monkeypatch):
    pid = 424245
    daemon.write_state(39906, {'pid': pid, 'port': 39906})
    signals: list[tuple[int, int]] = []
    checks = {'count': 0}

    def fake_is_running(p: int) -> bool:
        return p == pid and checks['count'] == 0

    def fake_kill(p: int, sig: int) -> None:
        signals.append((p, sig))
        checks['count'] += 1

    monkeypatch.setattr(daemon, 'is_pid_running', fake_is_running)
    monkeypatch.setattr(os, 'kill', fake_kill)
    assert daemon.stop_daemon(39906) is True
    assert signals == [(pid, signal.SIGTERM)]


def test_stop_all_daemons():
    port_a = process.reserve_tcp_port()
    port_b = process.reserve_tcp_port()
    try:
        daemon.start_daemon(port=port_a, chip_mode='auto')
        daemon.start_daemon(port=port_b, chip_mode='auto')
        stopped = daemon.stop_all_daemons()
        assert sorted(stopped) == sorted([port_a, port_b])
        assert daemon.list_running_daemons() == []
    finally:
        daemon.stop_daemon(port_a)
        daemon.stop_daemon(port_b)


def test_stop_all_daemons_empty():
    assert daemon.stop_all_daemons() == []


def test_erase_flash_requires_client_url(monkeypatch, registry_root: Registry):
    port = process.reserve_tcp_port()
    monkeypatch.setattr(
        daemon,
        'daemon_status',
        lambda _port, base=None: {'running': True, 'url': None},
    )
    with pytest.raises(RuntimeError, match='no client URL'):
        daemon.erase_flash(port=port, base=registry_root.path)


def test_erase_flash_all_requires_client_url(monkeypatch, registry_root: Registry):
    monkeypatch.setattr(
        daemon,
        'list_running_daemons',
        lambda base=None: [{'port': 12345, 'url': None}],
    )
    with pytest.raises(RuntimeError, match='no client URL'):
        daemon.erase_flash(port='all', base=registry_root.path)


def test_erase_flash_at_url_bad_response(monkeypatch):
    class FakeSock:
        def settimeout(self, _value: float) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        'esp32_mock_bootloader.transport.connect_serial_endpoint',
        lambda _url: FakeSock(),
    )
    from esp32_mock_bootloader import protocol_client

    monkeypatch.setattr(protocol_client, 'send_sync', lambda _sock: None)
    monkeypatch.setattr(protocol_client, 'activate_stub', lambda _sock: None)
    monkeypatch.setattr(protocol_client, 'send_and_receive', lambda *_a, **_k: b'')
    with pytest.raises(RuntimeError, match='no response to ERASE_FLASH'):
        daemon._erase_flash_at_url('socket://127.0.0.1:9999')


def test_terminate_pid_ignores_invalid():
    daemon._terminate_pid(0)
    daemon._terminate_pid(-1)


def test_set_detected_chip_ignores_none(monkeypatch, registry_root: Registry):
    port = process.reserve_tcp_port()
    monkeypatch.setattr(daemon, 'is_pid_running', lambda pid: pid == 424299)
    daemon.write_state(
        port,
        {'pid': 424299, 'port': port, 'detected_chip': 'esp32'},
        base=registry_root.path,
    )
    daemon.set_detected_chip(port, None, base=registry_root.path)
    state = daemon.read_state(port, registry_root.path)
    assert state is not None
    assert state['detected_chip'] == 'esp32'


def test_prune_registry_removes_stale_instances(monkeypatch, tmp_path):
    daemon.register_instance(39911, {'pid': 1, 'port': 39911}, base=tmp_path)
    monkeypatch.setattr(daemon, 'is_pid_running', lambda _pid: False)
    daemon.prune_registry(tmp_path)
    assert '39911' not in daemon.load_registry(tmp_path)['instances']


def test_start_daemon_force_replaces_existing(monkeypatch, registry_root: Registry):
    port = process.reserve_tcp_port()
    daemon.start_daemon(port=port, chip_mode='esp32', base=registry_root.path)
    stopped: list[int] = []
    monkeypatch.setattr(
        daemon,
        'stop_daemon',
        lambda p, base=None: stopped.append(p) or True,
    )
    daemon.start_daemon(
        port=port, chip_mode='esp32', base=registry_root.path, force=True,
    )
    assert stopped == [port]


def test_load_registry_rejects_non_object_instances(tmp_path):
    path = daemon.registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"instances": "bad"}', encoding='utf-8')
    assert daemon.load_registry(tmp_path) == {'version': 1, 'instances': {}}


def test_runtime_dir_explicit_base(tmp_path):
    custom = tmp_path / 'custom-state'
    assert daemon.runtime_dir(custom) == custom.resolve()


def test_erase_flash_at_url_command_failure(monkeypatch):
    class FakeSock:
        def settimeout(self, _value: float) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        'esp32_mock_bootloader.transport.connect_serial_endpoint',
        lambda _url: FakeSock(),
    )
    from esp32_mock_bootloader import protocol, protocol_client

    monkeypatch.setattr(protocol_client, 'send_sync', lambda _sock: None)
    monkeypatch.setattr(protocol_client, 'activate_stub', lambda _sock: None)
    monkeypatch.setattr(protocol_client, 'send_and_receive', lambda *_a, **_k: b'\x00\xff')
    monkeypatch.setattr(protocol_client, 'slip_decode_frames', lambda raw: [raw] if raw else [])
    monkeypatch.setattr(
        protocol_client,
        'parse_response',
        lambda _frame: (0, protocol.CMD_ERASE_FLASH, 0, 0x01, b''),
    )
    with pytest.raises(RuntimeError, match='ERASE_FLASH failed'):
        daemon._erase_flash_at_url('socket://127.0.0.1:9999')


def test_runtime_dir_resolves_env_path(monkeypatch, tmp_path):
    nested = tmp_path / 'pytest-nested' / 'runtime'
    monkeypatch.setenv('ESP32_MOCK_BOOTLOADER_STATE_DIR', os.fspath(nested))
    resolved = daemon.runtime_dir()
    assert resolved == nested.resolve()
    assert resolved.is_absolute()


def test_runtime_dir_relative_env_uses_system_temp(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ESP32_MOCK_BOOTLOADER_STATE_DIR', 'state')
    resolved = daemon.runtime_dir()
    assert resolved.is_absolute()
    assert resolved == Path(tempfile.gettempdir()).resolve() / 'state'
    assert tmp_path not in resolved.parents


def test_runtime_dir_unix_path_without_drive_not_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        'ESP32_MOCK_BOOTLOADER_STATE_DIR',
        '/private/var/folders/T/pytest/test_stop_daemon_windows_taskk0/state',
    )
    resolved = daemon.runtime_dir()
    if os.name == 'nt':
        expected = Path('/private/var/folders/T/pytest/test_stop_daemon_windows_taskk0/state').resolve()
    else:
        expected = Path(
            '/private/var/folders/T/pytest/test_stop_daemon_windows_taskk0/state',
        ).resolve()
    assert resolved == expected
    assert tmp_path not in resolved.parents
