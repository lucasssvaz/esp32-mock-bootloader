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
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

DEFAULT_PORT = 9876
DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_BIND = '127.0.0.1'
REGISTRY_VERSION = 1


def _is_truly_absolute(path: str) -> bool:
    """Return whether *path* is usable as an absolute path on this OS."""
    if sys.platform == 'win32':
        return bool(Path(path).drive) or path.startswith('\\\\') or path.startswith('/')
    return os.path.isabs(path)


def _local_path(*parts: str) -> Path:
    """Platform path type (immune to tests monkeypatching ``os.name``)."""
    if sys.platform == 'win32':
        return Path(*parts)
    import pathlib

    return pathlib.PosixPath(*parts)


def _canonical_runtime_path(value: str | Path) -> Path:
    """Return an absolute runtime directory, never anchored to the process cwd."""
    expanded = os.path.expanduser(os.path.expandvars(os.fspath(value)))
    if _is_truly_absolute(expanded):
        return _local_path(expanded).resolve()
    return _local_path(tempfile.gettempdir(), expanded).resolve()


def runtime_dir(base: Path | None = None) -> Path:
    """Directory for registry, logs, and other runtime files."""
    if base is not None:
        return _canonical_runtime_path(base)
    override = os.environ.get('ESP32_MOCK_BOOTLOADER_STATE_DIR')
    if override:
        return _canonical_runtime_path(override)
    return _canonical_runtime_path(Path(tempfile.gettempdir()) / 'esp32-mock-bootloader')


def registry_path(base: Path | None = None) -> Path:
    return runtime_dir(base) / 'registry.json'


def _registry_lock_path(base: Path | None = None) -> Path:
    return runtime_dir(base) / '.registry.lock'


def log_path(port: int, base: Path | None = None) -> Path:
    return runtime_dir(base) / f'port-{port}.log'


def socket_url(port: int, bind: str = DEFAULT_BIND) -> str:
    host = bind if bind not in ('0.0.0.0', '::') else '127.0.0.1'
    return f'socket://{host}:{port}'


@contextmanager
def _registry_lock(base: Path | None = None) -> Iterator[None]:
    path = _registry_lock_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+', encoding='utf-8') as lock_file:
        lock_backend = 'fcntl'
        try:
            if os.name == 'nt':
                try:
                    import msvcrt
                except ImportError:
                    pass
                else:
                    lock_backend = 'msvcrt'
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            if lock_backend == 'fcntl':
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if lock_backend == 'msvcrt':
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _empty_registry() -> dict[str, Any]:
    return {'version': REGISTRY_VERSION, 'instances': {}}


def load_registry(base: Path | None = None) -> dict[str, Any]:
    path = registry_path(base)
    if not path.is_file():
        return _empty_registry()
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty_registry()
    if not isinstance(data.get('instances'), dict):
        return _empty_registry()
    return data


def save_registry(data: dict[str, Any], base: Path | None = None) -> None:
    directory = runtime_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    path = registry_path(base)
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    # Re-ensure the directory exists: teardown may race parallel saves under xdist.
    directory.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, path)


def prune_registry(base: Path | None = None) -> None:
    with _registry_lock(base):
        data = load_registry(base)
        instances = data['instances']
        stale = [
            key for key, inst in instances.items()
            if not is_pid_running(int(inst.get('pid', 0)))
        ]
        if not stale:
            return
        for key in stale:
            del instances[key]
        save_registry(data, base)


def _mutate_registry(
    mutator: Callable[[dict[str, Any]], None],
    base: Path | None = None,
) -> None:
    with _registry_lock(base):
        data = load_registry(base)
        mutator(data)
        save_registry(data, base)


def register_instance(
    port: int,
    instance: dict[str, Any],
    base: Path | None = None,
) -> None:
    key = str(port)

    def _add(data: dict[str, Any]) -> None:
        data['instances'][key] = instance

    _mutate_registry(_add, base)


def unregister_instance(port: int, base: Path | None = None) -> None:
    key = str(port)

    def _remove(data: dict[str, Any]) -> None:
        data['instances'].pop(key, None)

    _mutate_registry(_remove, base)


def set_detected_chip(
    port: int,
    detected: str | None,
    base: Path | None = None,
) -> None:
    if detected is None:
        return
    key = str(port)

    def _update(data: dict[str, Any]) -> None:
        inst = data['instances'].get(key)
        if not inst or inst.get('detected_chip') == detected:
            return
        inst['detected_chip'] = detected

    _mutate_registry(_update, base)


def read_state(port: int, base: Path | None = None) -> dict[str, Any] | None:
    """Return the registry entry for *port*, or None if absent or not running."""
    prune_registry(base)
    inst = load_registry(base)['instances'].get(str(port))
    if not inst:
        return None
    if not is_pid_running(int(inst.get('pid', 0))):
        return None
    return dict(inst)


def write_state(port: int, data: dict[str, Any], base: Path | None = None) -> Path:
    """Replace the registry entry for *port* (mainly for tests)."""
    register_instance(port, data, base)
    return registry_path(base)


def remove_state(port: int, base: Path | None = None) -> None:
    unregister_instance(port, base)


# Backward compatibility for tests that referenced per-port JSON paths.
def state_dir(base: Path | None = None) -> Path:
    return runtime_dir(base)


def state_path(port: int, base: Path | None = None) -> Path:
    return registry_path(base)


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
            time.sleep(0.05)
    return False


def _spawn_detached(cmd: list[str], log_file: Path) -> subprocess.Popen[bytes]:
    log_str = os.fspath(log_file)
    os.makedirs(os.path.dirname(log_str), exist_ok=True)
    log_fd = open(log_str, 'a', encoding='utf-8')
    kwargs: dict[str, Any] = {
        'stdout': log_fd,
        'stderr': subprocess.STDOUT,
        'stdin': subprocess.DEVNULL,
        'env': os.environ.copy(),
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

    log_file = log_path(port, base)

    cmd = [
        sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
        '--daemon-child',
        '--port', str(port),
        '--chip', chip_mode,
        '--bind', bind,
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
        'mode': 'daemon',
    }
    register_instance(port, data, base)
    return data


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], check=False)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 2.0
    while time.time() < deadline and is_pid_running(pid):
        time.sleep(0.05)
    if is_pid_running(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def stop_daemon(port: int = DEFAULT_PORT, base: Path | None = None) -> bool:
    with _registry_lock(base):
        data = load_registry(base)
        inst = data['instances'].pop(str(port), None)
        if inst is None:
            save_registry(data, base)
            return False
        save_registry(data, base)

    pid = int(inst.get('pid', 0))
    if pid and pid != os.getpid() and is_pid_running(pid):
        _terminate_pid(pid)

    return True


def stop_all_daemons(base: Path | None = None) -> list[int]:
    """Stop every running instance. Returns TCP port(s) that were stopped."""
    ports = [int(info['port']) for info in list_running_daemons(base)]
    stopped: list[int] = []
    for port in ports:
        if stop_daemon(port, base):
            stopped.append(port)
    return stopped


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
        'mode': state.get('mode'),
    }


def list_running_daemons(base: Path | None = None) -> list[dict[str, Any]]:
    """Return status dicts for every running instance in the registry."""
    prune_registry(base)
    running: list[dict[str, Any]] = []
    for key in sorted(load_registry(base)['instances'], key=int):
        info = daemon_status(int(key), base)
        if info['running']:
            running.append(info)
    return running


def erase_flash(
    port: int | str = 'all',
    base: Path | None = None,
) -> list[int]:
    """Erase mock SPI flash on running server(s). Returns affected TCP port(s)."""
    if port != 'all':
        port_int = int(port)
        info = daemon_status(port_int, base)
        if not info['running']:
            raise RuntimeError(f'no mock bootloader running on port {port_int}')
        url = info['url']
        if not url:
            raise RuntimeError(f'mock bootloader on port {port_int} has no client URL')
        _erase_flash_at_url(url)
        return [port_int]

    daemons = list_running_daemons(base)
    if not daemons:
        raise RuntimeError('no mock bootloader daemon is running')
    erased: list[int] = []
    for info in daemons:
        url = info['url']
        if not url:
            raise RuntimeError(f'mock bootloader on port {info["port"]} has no client URL')
        _erase_flash_at_url(url)
        erased.append(int(info['port']))
    return erased


def _erase_flash_at_url(url: str) -> None:
    from esp32_mock_bootloader import protocol
    from esp32_mock_bootloader import protocol_client
    from esp32_mock_bootloader.transport import connect_serial_endpoint

    sock = connect_serial_endpoint(url)
    sock.settimeout(10.0)
    try:
        protocol_client.send_sync(sock)
        protocol_client.activate_stub(sock)
        raw = protocol_client.send_and_receive(sock, protocol_client.make_command(protocol.CMD_ERASE_FLASH))
        frames = protocol_client.slip_decode_frames(raw)
        if not frames:
            raise RuntimeError('no response to ERASE_FLASH')
        _direction, cmd, _size, value, _data = protocol_client.parse_response(frames[0])
        if cmd != protocol.CMD_ERASE_FLASH or value != 0:
            raise RuntimeError(f'ERASE_FLASH failed (cmd=0x{cmd:02x} status={value})')
    finally:
        sock.close()
