# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""PTY and mock-serial transport unit tests."""

from __future__ import annotations

import io
import os
import socket
import struct
import threading
import time

import pytest

from esp32_mock_bootloader import protocol
from esp32_mock_bootloader import server
import esp32_mock_bootloader.testing as mock


class _MockSerial:
    """Minimal pyserial stand-in backed by in-memory buffers."""

    def __init__(self) -> None:
        self._rx = bytearray()
        self._tx = bytearray()
        self.timeout = 1.0

    def feed(self, data: bytes) -> None:
        self._rx.extend(data)

    def read(self, size: int) -> bytes:
        if not self._rx:
            return b''
        chunk = bytes(self._rx[:size])
        del self._rx[:size]
        return chunk

    def write(self, data: bytes) -> int:
        self._tx.extend(data)
        return len(data)

    @property
    def written(self) -> bytes:
        return bytes(self._tx)

    def close(self) -> None:
        pass


def test_bootloader_connection_mock_serial_round_trip():
    ser = _MockSerial()
    conn = server.BootloaderConnection(serial_port=ser)
    conn.sendall(b'\xc0hello\xc0')
    assert ser.written == b'\xc0hello\xc0'
    ser.feed(b'\xc0ok\xc0')
    assert conn.recv(64) == b'\xc0ok\xc0'
    conn.close()


def test_bootloader_connection_serial_timeout_on_empty():
    ser = _MockSerial()
    conn = server.BootloaderConnection(serial_port=ser)
    with pytest.raises(socket.timeout):
        conn.recv(8)


@pytest.mark.skipif(os.name == 'nt', reason='Unix PTY fd transport')
def test_handle_client_over_pty_master_fd():
    import pty

    master, slave = pty.openpty()
    pty_transport = server.PtyMasterTransport(master, slave)
    stop_event = threading.Event()

    def serve() -> None:
        server.handle_client(
            server.BootloaderConnection(pty=pty_transport),
            stop_event,
            'esp32',
        )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    try:
        sync = mock.protocol.make_command(protocol.CMD_SYNC, b'\x55' * 36)
        os.write(slave, sync)
        buf = bytearray()
        deadline = 5.0
        import time

        start = time.monotonic()
        while time.monotonic() - start < deadline:
            try:
                chunk = os.read(slave, 4096)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            if not chunk:
                break
            buf.extend(chunk)
            frames = server.slip_decode_frames(bytes(buf))
            if frames:
                _d, cmd, _s, _v, _data = mock.protocol.parse_response(frames[0])
                assert cmd == protocol.CMD_SYNC
                break
        else:
            pytest.fail('no SYNC response on PTY')
    finally:
        stop_event.set()
        os.close(slave)
        pty_transport.close()
        thread.join(timeout=2)


def test_handle_client_over_mock_serial():
    ser = _MockSerial()
    stop_event = threading.Event()

    def serve() -> None:
        server.handle_client(
            server.BootloaderConnection(serial_port=ser),
            stop_event,
            'esp32',
        )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    try:
        sync = mock.protocol.make_command(protocol.CMD_SYNC, b'\x55' * 36)
        ser.feed(sync)
        deadline = 5.0
        import time

        start = time.monotonic()
        while time.monotonic() - start < deadline:
            if ser.written:
                frames = server.slip_decode_frames(ser.written)
                if frames:
                    _d, cmd, _s, _v, _data = mock.protocol.parse_response(frames[0])
                    assert cmd == protocol.CMD_SYNC
                    break
            time.sleep(0.05)
        else:
            pytest.fail('no SYNC response on mock serial')
    finally:
        stop_event.set()
        thread.join(timeout=2)


@pytest.mark.skipif(os.name == 'nt', reason='Unix PTY fd transport')
def test_cli_run_pty_exits_on_client_disconnect(tmp_path):
    path_file = tmp_path / 'mock.pty'
    proc = mock.server.start_pty(path_file, timeout=30.0, chip='esp32', exit_on_disconnect=True)
    try:
        endpoint = mock.server.read_pty_path(path_file)
        slave_fd = os.open(endpoint, os.O_RDWR)
        try:
            os.write(slave_fd, mock.protocol.make_command(protocol.CMD_SYNC, b'\x55' * 36))
        finally:
            os.close(slave_fd)
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        mock.server.stop_subprocess(proc)


def test_pty_master_transport_releases_slave_on_first_byte():
    import pty

    master, slave = pty.openpty()
    path = os.ttyname(slave)
    transport = server.PtyMasterTransport(master, slave, track_disconnect=True)
    try:
        client = os.open(path, os.O_RDWR)
        try:
            os.write(client, b'\x01')
            assert transport.recv(64) == b'\x01'
            assert not transport.ignore_eof_for_disconnect()
        finally:
            os.close(client)
        assert transport.recv(64) == b''
    finally:
        transport.close()


def test_handle_client_exit_on_disconnect_waits_for_eof():
    """exit_on_disconnect defers session end until the transport signals disconnect."""
    from serial.serialutil import SerialException

    class _DisconnectAfterSyncSerial:
        def __init__(self) -> None:
            self.timeout = 1.0
            self._reads = 0

        def read(self, size: int) -> bytes:
            self._reads += 1
            if self._reads == 1:
                return mock.protocol.make_command(protocol.CMD_SYNC, b'\x55' * 36)
            raise SerialException('port closed')

        def write(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None:
            pass

    ser = _DisconnectAfterSyncSerial()
    stop_event = threading.Event()
    disconnected: list[bool] = []

    def serve() -> None:
        disconnected.append(
            server.handle_client(
                server.BootloaderConnection(serial_port=ser),
                stop_event,
                'esp32',
                exit_on_disconnect=True,
            ),
        )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert disconnected == [True]


def test_bootloader_connection_serial_exception():
    from serial.serialutil import SerialException

    class _BrokenSerial:
        timeout = 0.05

        def read(self, size: int) -> bytes:
            raise SerialException('gone')

    conn = server.BootloaderConnection(serial_port=_BrokenSerial())
    assert conn.recv(8) == b''


def test_run_com_server_writes_client_port(tmp_path, monkeypatch):
    import sys

    class _FakeSerial:
        def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.05) -> None:
            self.port = port
            self.timeout = timeout

        def read(self, size: int) -> bytes:
            return b''

        def write(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None:
            pass

    fake_serial_mod = type(sys)('serial')
    fake_serial_mod.Serial = _FakeSerial
    monkeypatch.setitem(sys.modules, 'serial', fake_serial_mod)
    monkeypatch.setattr(server, 'handle_client', lambda *args, **kwargs: False)

    path_file = str(tmp_path / 'client.port')
    server._run_com_server(
        'COM_BIND',
        'COM_CLIENT',
        timeout=0.05,
        pty_path_file=path_file,
        chip_mode='esp32',
        on_detected=None,
        exit_on_disconnect=False,
    )
    assert (tmp_path / 'client.port').read_text(encoding='ascii') == 'COM_CLIENT'


def test_run_pty_server_dispatch(monkeypatch, tmp_path):
    com_calls: list[tuple[str, str]] = []
    win_calls: list[str] = []

    monkeypatch.setattr(
        server,
        'resolve_serial_pair',
        lambda client_port=None, serial_bind=None: ('COM1', 'COM2')
        if client_port == 'COM2'
        else None,
    )
    monkeypatch.setattr(
        server,
        '_run_com_server',
        lambda bind, client, *rest, **kwargs: com_calls.append((bind, client)),
    )
    monkeypatch.setattr(
        server,
        '_run_windows_pty_server',
        lambda *rest, **kwargs: win_calls.append('win'),
    )
    monkeypatch.setattr(server, '_run_unix_pty_server', lambda *rest, **kwargs: None)

    server.run_pty_server(0.05, str(tmp_path / 'a'), 'esp32', client_port='COM2')
    assert com_calls == [('COM1', 'COM2')]

    monkeypatch.setattr(server, 'resolve_serial_pair', lambda *a, **k: None)
    monkeypatch.setattr(os, 'name', 'nt')
    server.run_pty_server(0.05, str(tmp_path / 'b'), 'esp32')
    assert win_calls == ['win']


def test_run_windows_pty_server_socket_fallback(tmp_path):
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
    sock = mock.server.connect_serial_endpoint(endpoint)
    try:
        mock.protocol.send_sync(sock)
    finally:
        sock.close()
    thread.join(timeout=1)
