# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Background daemon control for the mock bootloader."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIR = Path(os.environ.get(
    'ESP32_MOCK_BOOTLOADER_STATE_DIR',
    Path.home() / '.cache' / 'esp32-mock-bootloader',
))
DEFAULT_PORT = 9876
DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_BIND = '127.0.0.1'


def state_dir(path: str | Path | None = None) -> Path:
    return Path(path) if path else DEFAULT_STATE_DIR


def state_path(port: int, base: Path | None = None) -> Path:
    return state_dir(base) / f'port-{port}.json'


def log_path(port: int, base: Path | None = None) -> Path:
    return state_dir(base) / f'port-{port}.log'


def socket_url(port: int, bind: str = DEFAULT_BIND) -> str:
    host = bind if bind not in ('0.0.0.0', '::') else '127.0.0.1'
    return f'socket://{host}:{port}'


def read_state(port: int, base: Path | None = None) -> dict[str, Any] | None:
    path = state_path(port, base)
    if not path.is_file():
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_state(port: int, data: dict[str, Any], base: Path | None = None) -> Path:
    directory = state_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    path = state_path(port, directory)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    return path


def remove_state(port: int, base: Path | None = None) -> None:
    path = state_path(port, base)
    try:
        path.unlink()
    except OSError:
        pass


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok and exit_code.value == STILL_ACTIVE)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_for_port(port: int, bind: str = DEFAULT_BIND, timeout: float = 30.0) -> bool:
    host = bind if bind not in ('0.0.0.0', '::') else '127.0.0.1'
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _spawn_detached(cmd: list[str], log_file: Path) -> subprocess.Popen[bytes]:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fd = open(log_file, 'a', encoding='utf-8')
    kwargs: dict[str, Any] = {
        'stdout': log_fd,
        'stderr': subprocess.STDOUT,
        'stdin': subprocess.DEVNULL,
    }
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen(cmd, **kwargs)


def start_daemon(
    port: int = DEFAULT_PORT,
    chip_mode: str = 'auto',
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    bind: str = DEFAULT_BIND,
    base: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    existing = read_state(port, base)
    if existing and is_pid_running(int(existing.get('pid', 0))):
        if not force:
            raise RuntimeError(
                f'mock bootloader already running on port {port} (pid {existing["pid"]})'
            )
        stop_daemon(port, base)

    directory = state_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    state_file = state_path(port, directory)
    log_file = log_path(port, directory)

    cmd = [
        sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
        '--port', str(port),
        '--chip', chip_mode,
        '--bind', bind,
        '--state-file', str(state_file),
    ]
    proc = _spawn_detached(cmd, log_file)

    if not wait_for_port(port, bind, startup_timeout):
        if proc.poll() is not None:
            raise RuntimeError(
                f'mock bootloader process exited during startup (see {log_file})'
            )
        proc.terminate()
        raise RuntimeError(f'mock bootloader did not open port {port} in time')

    data = {
        'pid': proc.pid,
        'port': port,
        'chip': chip_mode,
        'bind': bind,
        'url': socket_url(port, bind),
        'log_file': str(log_file),
        'detected_chip': None,
    }
    write_state(port, data, directory)
    return data


def stop_daemon(port: int = DEFAULT_PORT, base: Path | None = None) -> bool:
    state = read_state(port, base)
    if not state:
        return False

    pid = int(state.get('pid', 0))
    if pid and is_pid_running(pid):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], check=False)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.time() + 5.0
            while time.time() < deadline and is_pid_running(pid):
                time.sleep(0.1)
            if is_pid_running(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    remove_state(port, base)
    return True


def daemon_status(
    port: int = DEFAULT_PORT,
    base: Path | None = None,
) -> dict[str, Any]:
    state = read_state(port, base) or {}
    pid = int(state.get('pid', 0))
    running = bool(pid and is_pid_running(pid))
    url = state.get('url') or socket_url(port, state.get('bind', DEFAULT_BIND))
    return {
        'running': running,
        'pid': pid if running else None,
        'port': port,
        'chip': state.get('chip', 'auto'),
        'detected_chip': state.get('detected_chip'),
        'url': url if running else None,
        'log_file': state.get('log_file'),
    }
