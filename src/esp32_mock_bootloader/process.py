# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Spawn and stop local mock bootloader subprocesses."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from esp32_mock_bootloader.daemon import wait_for_port

DEFAULT_STARTUP_TIMEOUT = 15.0


def stop_subprocess(proc: subprocess.Popen[bytes], *, timeout: float = 1.0) -> None:
    """Wait for a foreground server to exit; signal only if it is still running."""
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)


def reserve_tcp_port() -> int:
    """Bind to port 0 and return an ephemeral localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_server(
    port: int | None = None,
    timeout: float | None = 10.0,
    chip: str = 'auto',
    *,
    exit_on_disconnect: bool = False,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> tuple[subprocess.Popen[bytes], int]:
    """Start mock bootloader subprocess; return (proc, listen_port).

    When *port* is None the server binds to port 0 (OS-assigned) and writes
    the actual port to a temp file — no TOCTOU race with other processes.
    """
    use_port_file = port is None
    port_file: Path | None = None

    if use_port_file:
        port_file = Path(tempfile.gettempdir()) / f'emb-port-{uuid.uuid4().hex}'
        listen_port_arg = 0
    else:
        listen_port_arg = port

    cmd = [
        sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
        '--port', str(listen_port_arg),
        '--chip', chip,
    ]
    if port_file:
        cmd.extend(['--port-file', str(port_file)])
    if exit_on_disconnect:
        cmd.append('--exit-on-disconnect')
    if timeout is not None:
        cmd.extend(['--timeout', str(timeout)])
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    if use_port_file:
        listen_port = _wait_port_file(proc, port_file, startup_timeout)
    else:
        listen_port = port  # type: ignore[assignment]
        if not wait_for_port(listen_port, timeout=startup_timeout):
            exit_code = proc.poll()
            stderr_tail = b''
            if proc.stderr:
                stderr_tail = proc.stderr.read(2048)
            stop_subprocess(proc)
            raise RuntimeError(
                f'mock server did not open port {listen_port} '
                f'(exit={exit_code}, stderr={stderr_tail.decode(errors="replace")!r})',
            )
    return proc, listen_port


def _wait_port_file(
    proc: subprocess.Popen[bytes],
    port_file: Path,
    timeout: float,
) -> int:
    """Wait for the server to write its actual port to *port_file*."""
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr_tail = b''
                if proc.stderr:
                    stderr_tail = proc.stderr.read(2048)
                stop_subprocess(proc)
                raise RuntimeError(
                    f'mock server exited during startup '
                    f'(exit={proc.returncode}, stderr={stderr_tail.decode(errors="replace")!r})',
                )
            if port_file.is_file():
                content = port_file.read_text(encoding='ascii').strip()
                if content:
                    return int(content)
            time.sleep(0.02)
        stop_subprocess(proc)
        raise RuntimeError('mock server did not write port file in time')
    finally:
        try:
            port_file.unlink(missing_ok=True)
        except OSError:
            pass


def start_pty(
    path_file: Path | None = None,
    timeout: float | None = 10.0,
    chip: str = 'auto',
    *,
    exit_on_disconnect: bool = False,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> tuple[subprocess.Popen[bytes], str]:
    """Start a PTY mock server subprocess; return (proc, pty_endpoint).

    When *path_file* is None a temp file is created automatically for the
    subprocess to report its endpoint — same pattern as start_server's
    port-file mechanism.
    """
    owns_path_file = path_file is None
    if owns_path_file:
        path_file = Path(tempfile.gettempdir()) / f'emb-pty-{uuid.uuid4().hex}.path'
    path_file.parent.mkdir(parents=True, exist_ok=True)
    if path_file.exists():
        path_file.unlink()
    cmd = [
        sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
        '--pty', '--port-file', str(path_file),
        '--chip', chip,
    ]
    if exit_on_disconnect:
        cmd.append('--exit-on-disconnect')
    serial_bind = os.environ.get('ESP32_MOCK_SERIAL_BIND') or os.environ.get('ESP32_MOCK_COM_PORT')
    client_port = os.environ.get('ESP32_MOCK_PORT') or os.environ.get('ESP32_MOCK_COM_PEER')
    if serial_bind and client_port:
        cmd.extend(['--serial-bind', serial_bind, '--port', client_port])
    elif client_port:
        cmd.extend(['--port', client_port])
    elif serial_bind:
        cmd.extend(['--serial-bind', serial_bind])
    if timeout is not None:
        cmd.extend(['--timeout', str(timeout)])
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        endpoint = _wait_pty_file(proc, path_file, startup_timeout)
    finally:
        if owns_path_file:
            try:
                path_file.unlink(missing_ok=True)
            except OSError:
                pass
    return proc, endpoint


def _wait_pty_file(
    proc: subprocess.Popen[bytes],
    path_file: Path,
    timeout: float,
) -> str:
    """Wait for the PTY server to write its endpoint to *path_file*."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path_file.is_file():
            content = path_file.read_text(encoding='ascii').strip()
            if content:
                return content
        if proc.poll() is not None:
            stderr_tail = b''
            if proc.stderr:
                stderr_tail = proc.stderr.read(2048)
            raise RuntimeError(
                f'PTY mock exited during startup '
                f'(exit={proc.returncode}, stderr={stderr_tail.decode(errors="replace")!r})',
            )
        time.sleep(0.02)
    stop_subprocess(proc)
    raise TimeoutError(f'PTY path file not created within {timeout}s')


@contextmanager
def running_mock(
    transport: str,
    chip: str,
    *,
    port: int | None = None,
    timeout: float | None = 30.0,
) -> Iterator[tuple[subprocess.Popen[bytes], int | None, str | None]]:
    """Start mock server over TCP or PTY; yield (proc, tcp_port, pty_path)."""
    if transport == 'tcp':
        proc, tcp_port = start_server(
            port, timeout=timeout, chip=chip, exit_on_disconnect=True,
        )
        try:
            yield proc, tcp_port, None
        finally:
            stop_subprocess(proc)
        return

    proc, endpoint = start_pty(timeout=timeout, chip=chip, exit_on_disconnect=True)
    try:
        yield proc, None, endpoint
    finally:
        stop_subprocess(proc)


__all__ = [
    'DEFAULT_STARTUP_TIMEOUT',
    'reserve_tcp_port',
    'running_mock',
    'start_pty',
    'start_server',
    'stop_subprocess',
]
