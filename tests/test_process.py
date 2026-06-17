# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Tests for process.py and transport.py — unit edge paths and integration."""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esp32_mock_bootloader import chips, constants, process, protocol, protocol_client, transport
from tests.helpers import esptool
from tests.helpers.advanced import raw_tcp, raw_transport

pytestmark = pytest.mark.advanced


# --- process.py / transport.py unit tests ---


class _HangProc:
    def __init__(self) -> None:
        self.kill_called = False
        self.terminate_called = False
        self._poll: int | None = None

    def poll(self) -> int | None:
        return self._poll

    def wait(self, timeout: float | None = None) -> int:
        if self.kill_called:
            return 0
        raise subprocess.TimeoutExpired(cmd='mock', timeout=timeout or 0)

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True
        self._poll = 0


def test_stop_subprocess_force_kill():
    proc = _HangProc()
    process.stop_subprocess(proc, timeout=0.01)
    assert proc.terminate_called
    assert proc.kill_called


def test_stop_subprocess_noop_when_exited():
    proc = MagicMock()
    proc.poll.return_value = 0
    process.stop_subprocess(proc)
    proc.wait.assert_not_called()


def test_start_server_fails_when_port_not_ready(monkeypatch):
    monkeypatch.setattr(process, 'wait_for_port', lambda *_a, **_k: False)

    class DeadProc:
        returncode = 1
        stderr = None

        def poll(self) -> int:
            return 1

        def wait(self, timeout=None):
            return 1

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(process.subprocess, 'Popen', lambda *_a, **_k: DeadProc())
    with pytest.raises(RuntimeError, match='did not open port'):
        process.start_server(port=12345, chip='esp32', startup_timeout=0.1)


def test_start_server_port_file_race_free(monkeypatch, tmp_path: Path):
    """When port is None, start_server uses a port-file (no TOCTOU race)."""
    class FakeProc:
        returncode = None
        stderr = None

        def poll(self) -> int | None:
            return None

        def wait(self, timeout=None):
            pass

        def terminate(self):
            pass

        def kill(self):
            pass

    def fake_popen(cmd, **kwargs):
        for i, arg in enumerate(cmd):
            if arg == '--port-file' and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_text('54321', encoding='ascii')
                break
        return FakeProc()

    monkeypatch.setattr(process.subprocess, 'Popen', fake_popen)
    proc, port = process.start_server(chip='esp32', startup_timeout=2.0)
    assert port == 54321


def test_start_server_port_file_proc_dies(monkeypatch, tmp_path: Path):
    """If subprocess dies before writing port-file, raise immediately."""
    class DeadProc:
        returncode = 2
        stderr = None

        def poll(self) -> int:
            return 2

        def wait(self, timeout=None):
            return 2

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(process.subprocess, 'Popen', lambda *_a, **_k: DeadProc())
    with pytest.raises(RuntimeError, match='exited during startup'):
        process.start_server(chip='esp32', startup_timeout=0.1)


def test_start_pty_env_var_branches(monkeypatch, tmp_path: Path):
    captured: list[list[str]] = []

    class OkProc:
        stderr = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            pass

    def fake_popen(cmd, **kwargs):
        for i, arg in enumerate(cmd):
            if arg == '--port-file' and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_text('/dev/ttyMOCK', encoding='ascii')
                break
        captured.append(list(cmd))
        return OkProc()

    monkeypatch.setattr(process.subprocess, 'Popen', fake_popen)

    monkeypatch.setenv('ESP32_MOCK_SERIAL_BIND', 'COM1')
    monkeypatch.setenv('ESP32_MOCK_PORT', 'COM2')
    path_file = tmp_path / 'pty.path'
    proc, endpoint = process.start_pty(path_file, chip='esp32')
    assert proc is not None
    assert endpoint == '/dev/ttyMOCK'
    assert '--serial-bind' in captured[0]
    assert 'COM2' in captured[0]

    captured.clear()
    monkeypatch.delenv('ESP32_MOCK_SERIAL_BIND', raising=False)
    path_file = tmp_path / 'pty2.path'
    process.start_pty(path_file, chip='esp32')
    assert '--port' in captured[0]
    assert 'COM2' in captured[0]

    captured.clear()
    monkeypatch.delenv('ESP32_MOCK_PORT', raising=False)
    monkeypatch.setenv('ESP32_MOCK_COM_PORT', 'COM3')
    path_file = tmp_path / 'pty3.path'
    process.start_pty(path_file, chip='esp32')
    assert '--serial-bind' in captured[0]
    assert 'COM3' in captured[0]


def test_start_pty_exits_during_startup(monkeypatch, tmp_path: Path):
    class DeadProc:
        returncode = 1
        stderr = None

        def poll(self) -> int:
            return 1

    monkeypatch.setattr(process.subprocess, 'Popen', lambda *_a, **_k: DeadProc())
    with pytest.raises(RuntimeError, match='exited during startup'):
        process.start_pty(tmp_path / 'missing.path', chip='esp32')


def test_start_pty_startup_timeout(monkeypatch, tmp_path: Path):
    class HangProc:
        terminated = False
        stderr = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            if not self.terminated:
                raise subprocess.TimeoutExpired('mock', timeout)

        def kill(self):
            self.terminated = True

    hang = HangProc()
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *_a, **_k: hang)
    with pytest.raises(TimeoutError, match='PTY path file not created'):
        process.start_pty(tmp_path / 'never.path', chip='esp32', startup_timeout=0.05)
    assert hang.terminated


def test_running_mock_tcp():
    with process.running_mock('tcp', 'esp32', timeout=None) as (proc, tcp_port, pty_path):
        assert proc.poll() is None
        assert isinstance(tcp_port, int)
        assert pty_path is None
        with raw_tcp(tcp_port) as sock:
            assert sock.fileno() >= 0


def test_transport_connect_retries(monkeypatch):
    port = process.reserve_tcp_port()
    proc, listen_port = process.start_server(chip='esp32', port=port, timeout=None)
    try:
        attempts = {'count': 0}
        real_connect = socket.socket.connect

        def flaky_connect(self, address):
            attempts['count'] += 1
            if attempts['count'] == 1:
                raise ConnectionRefusedError('not yet')
            return real_connect(self, address)

        monkeypatch.setattr(socket.socket, 'connect', flaky_connect)
        sock = transport.connect(listen_port, timeout=2.0)
        try:
            assert attempts['count'] >= 2
            assert sock.fileno() >= 0
        finally:
            sock.close()
    finally:
        process.stop_subprocess(proc)


def test_connect_transport_tcp_port():
    port = process.reserve_tcp_port()
    proc, listen_port = process.start_server(chip='esp32', port=port, timeout=None)
    try:
        sock = transport.connect_transport('tcp', port=listen_port)
        try:
            assert isinstance(sock, socket.socket)
        finally:
            sock.close()
    finally:
        process.stop_subprocess(proc)


def test_connect_refused_raises(monkeypatch):
    monkeypatch.setattr(transport, 'DEFAULT_CONNECT_TIMEOUT', 0.15)
    with pytest.raises(ConnectionRefusedError):
        transport.connect(59998, timeout=0.15)
    with pytest.raises(ValueError, match='port required'):
        transport.connect_transport('tcp')
    with pytest.raises(ValueError, match='pty_path required'):
        transport.connect_transport('pty')


def test_connect_pty_delegates_to_serial_endpoint(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(transport, 'connect_serial_endpoint', lambda endpoint: sentinel)
    assert transport.connect_pty('/dev/ttyX') is sentinel


def test_connect_serial_endpoint_localhost_alias(monkeypatch):
    port = process.reserve_tcp_port()
    proc, listen_port = process.start_server(chip='esp32', port=port, timeout=None)
    try:
        sock = transport.connect_serial_endpoint(f'socket://localhost:{listen_port}')
        try:
            assert isinstance(sock, socket.socket)
        finally:
            sock.close()
    finally:
        process.stop_subprocess(proc)


def test_serial_link_duck_socket(monkeypatch):
    class FakeSerial:
        instances: list[FakeSerial] = []

        def __init__(self, port: str, baudrate: int, timeout: float) -> None:
            self.port = port
            self.baudrate = baudrate
            self.timeout = timeout
            self.written = b''
            self.closed = False
            FakeSerial.instances.append(self)

        def write(self, data: bytes) -> None:
            self.written += data

        def read(self, size: int) -> bytes:
            return b'\xaa' * min(size, 1)

        def close(self) -> None:
            self.closed = True

    fake_module = type('serial', (), {'Serial': FakeSerial})()
    monkeypatch.setitem(__import__('sys').modules, 'serial', fake_module)

    link = transport.SerialLink('/dev/ttyTEST')
    link.settimeout(1.0)
    link.sendall(b'\x01\x02')
    assert link.recv(4) == b'\xaa'
    link.close()
    ser = FakeSerial.instances[0]
    assert ser.timeout == 1.0
    assert ser.written == b'\x01\x02'
    assert ser.closed


def test_connect_serial_endpoint_invalid_socket_url():
    with pytest.raises(ValueError, match='invalid socket endpoint'):
        transport.connect_serial_endpoint('socket://127.0.0.1')


def test_connect_serial_endpoint_unsupported_host():
    with pytest.raises(ValueError, match='unsupported socket host'):
        transport.connect_serial_endpoint('socket://192.168.1.1:1234')


# --- process.py / transport.py integration tests ---


def test_tcp_exit_on_disconnect_ignores_empty_probe():
    """Port-readiness probes must not end a one-shot server before the real client."""
    import socket as pysocket

    port = process.reserve_tcp_port()
    proc, listen_port = process.start_server(
        port=port, chip='esp32', timeout=None, exit_on_disconnect=True,
    )
    try:
        with pysocket.create_connection(('127.0.0.1', listen_port), timeout=1.0):
            pass
        time.sleep(0.1)
        assert proc.poll() is None
        with raw_tcp(listen_port) as sock:
            protocol_client.send_sync(sock)
        proc.wait(timeout=3)
        assert proc.returncode == 0
    finally:
        process.stop_subprocess(proc)


def test_foreground_exits_on_client_disconnect():
    port = process.reserve_tcp_port()
    proc = None
    try:
        proc, listen_port = process.start_server(
            port=port, timeout=30.0, chip='esp32', exit_on_disconnect=True,
        )
        with raw_tcp(listen_port) as sock:
            protocol_client.send_sync(sock)
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc is not None and proc.poll() is None:
            process.stop_subprocess(proc)


def test_subprocess_stdio_devnull_survives_many_sessions():
    """Regression: PIPE capture deadlocks the child when stderr/stdout fill."""
    for _ in range(12):
        proc, port = process.start_server(
            chip='esp32', timeout=None, exit_on_disconnect=True,
        )
        try:
            with raw_tcp(port) as sock:
                protocol_client.send_sync(sock)
            proc.wait(timeout=3)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                process.stop_subprocess(proc)


def test_run_windows_pty_server_socket_fallback(tmp_path):
    from esp32_mock_bootloader import server

    path_file = str(tmp_path / 'endpoint')
    thread = threading.Thread(
        target=server._run_windows_pty_server,
        args=(0.2, path_file, 'esp32', None),
        kwargs={'exit_on_disconnect': True},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 1.0
    endpoint = ''
    while time.monotonic() < deadline:
        if os.path.isfile(path_file):
            endpoint = open(path_file, encoding='ascii').read().strip()
            if endpoint:
                break
        time.sleep(0.01)
    assert endpoint.startswith('socket://')
    sock = transport.connect_serial_endpoint(endpoint)
    try:
        protocol_client.send_sync(sock)
    finally:
        sock.close()
    thread.join(timeout=1)


@pytest.mark.skipif(os.name == 'nt', reason='Unix PTY fd transport')
def test_cli_run_pty_exits_on_client_disconnect(tmp_path):
    path_file = tmp_path / 'mock.pty'
    proc, endpoint = process.start_pty(path_file, timeout=30.0, chip='esp32', exit_on_disconnect=True)
    try:
        slave_fd = os.open(endpoint, os.O_RDWR)
        try:
            os.write(slave_fd, protocol_client.make_command(protocol.CMD_SYNC, b'\x55' * 36))
        finally:
            os.close(slave_fd)
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        process.stop_subprocess(proc)


def test_pty_path_file_written(tmp_path: Path):
    path_file = tmp_path / 'mock.pty'
    proc, endpoint = process.start_pty(path_file, timeout=30.0, chip='auto', exit_on_disconnect=True)
    try:
        assert endpoint
        if os.environ.get('ESP32_MOCK_SERIAL_BIND') or os.environ.get('ESP32_MOCK_COM_PORT'):
            peer = os.environ.get('ESP32_MOCK_PORT') or os.environ.get('ESP32_MOCK_COM_PEER', 'COM19')
            assert endpoint == peer
        elif os.name == 'nt':
            assert endpoint.startswith('socket://')
        else:
            assert Path(endpoint).exists()
        with raw_transport('pty', pty_path=endpoint) as sock:
            assert protocol_client.minimal_plain_flash(sock)
    finally:
        process.stop_subprocess(proc)


@pytest.mark.esptool
@pytest.mark.parametrize('chip', ('esp32', 'esp32c3'))
def test_flash_id_pty_no_protocol_warnings(chip: str, tmp_path: Path):
    path_file = tmp_path / 'mock.pty'
    proc, pty_path = process.start_pty(path_file, timeout=30.0, chip=chip, exit_on_disconnect=True)
    try:
        result = esptool.run_flash_id(chip, pty_path=pty_path)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output[-800:]
        warns = esptool.forbidden_warnings(output, transport='pty')
        assert warns == [], '\n'.join(warns)
    finally:
        process.stop_subprocess(proc)


@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_protocol_smoke_pty(chip: str):
    with process.running_mock('pty', chip, timeout=30.0) as (_proc, _port, pty_path):
        with raw_transport('pty', pty_path=pty_path) as sock:
            assert protocol_client.minimal_plain_flash(sock)


@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_explicit_chip_detect_register_pty(chip: str):
    profile = chips.PROFILES[chip]
    if not profile.detect_magic:
        pytest.skip(f'{chip} has no detect magic register')
    with process.running_mock('pty', chip, timeout=30.0) as (_proc, _port, pty_path):
        with raw_transport('pty', pty_path=pty_path) as sock:
            protocol_client.send_sync(sock)
            assert protocol_client.read_reg_value(sock, profile.detect_reg) == profile.detect_magic


@pytest.mark.esptool
def test_cli_erase_flash_clears_image_via_raw_transport(cli):
    """Verify erase via raw protocol — staging uses esptool (self-managed port only)."""
    from esp32_mock_bootloader import daemon

    port = process.reserve_tcp_port()
    offset = constants.FLASH_APP_OFFSET
    length = 0x200
    try:
        result = cli('start', '--port', str(port), '--chip', 'esp32')
        assert result.returncode == 0, result.stderr

        with tempfile.TemporaryDirectory() as tmp:
            bin_file = esptool.create_pattern_binary(Path(tmp) / 'flash.bin', length, 0x5A)
            ok, detail = esptool.write_flash_no_stub('esp32', str(bin_file), port=port)
            assert ok, detail

        erase = cli('erase-flash', '--port', str(port))
        assert erase.returncode == 0, erase.stderr

        with raw_tcp(port) as sock:
            protocol_client.activate_stub(sock)
            data, digest = protocol_client.stub_read_flash(sock, offset, length)
        assert data == b'\xff' * length
        assert digest == hashlib.md5(data).digest()
    finally:
        daemon.stop_daemon(port)
