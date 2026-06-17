# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""CLI-parity operations on running mock bootloader instances."""

from __future__ import annotations

import json
import os
import sys
import weakref
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal, TextIO

from esp32_mock_bootloader import daemon

if TYPE_CHECKING:
    from esp32_mock_bootloader.api import MockHandle

PortTarget = int | Literal['all'] | None
StatusFormat = Literal['data', 'text', 'json']
ResolveMode = Literal['single', 'multi']

_FOREGROUND_HANDLES: weakref.WeakSet[MockHandle] = weakref.WeakSet()


def register_foreground_handle(handle: MockHandle) -> None:
    _FOREGROUND_HANDLES.add(handle)


def unregister_foreground_handle(handle: MockHandle) -> None:
    _FOREGROUND_HANDLES.discard(handle)


def _registry_base(base: Path | None = None) -> Path | None:
    if base is not None:
        return base
    env = os.environ.get('ESP32_MOCK_BOOTLOADER_STATE_DIR')
    if env:
        return Path(env)
    return None


def _foreground_status(handle: MockHandle) -> dict[str, Any] | None:
    session = handle._session  # noqa: SLF001
    if not session._started:  # noqa: SLF001
        return None
    proc = session.proc
    running = session.running
    pid = proc.pid if proc is not None and running else None
    return {
        'running': running,
        'pid': pid,
        'port': session.port,
        'chip': session.chip,
        'detected_chip': session.detected_chip,
        'url': session.url if running else None,
        'log_file': None,
        'mode': 'foreground',
    }


def _list_foreground_statuses() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for handle in list(_FOREGROUND_HANDLES):
        info = _foreground_status(handle)
        if info is not None and info['running']:
            records.append(info)
    return records


def _merge_status_records(
    daemon_records: list[dict[str, Any]],
    foreground_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_port = {int(info['port']): info for info in daemon_records}
    for info in foreground_records:
        port = int(info['port'])
        if port not in by_port:
            by_port[port] = info
    return [by_port[key] for key in sorted(by_port)]


def resolve_port(
    port: PortTarget = None,
    *,
    base: Path | None = None,
) -> tuple[ResolveMode, int | list[dict[str, Any]]]:
    """Return ('single', port) or ('multi', status records)."""
    registry_base = _registry_base(base)
    if port is not None and port != 'all':
        return 'single', int(port)

    daemon_records = daemon.list_running_daemons(registry_base)
    foreground_records = _list_foreground_statuses()
    running = _merge_status_records(daemon_records, foreground_records)

    if port == 'all':
        return 'multi', running

    if len(running) > 1:
        return 'multi', running
    if len(running) == 1:
        return 'single', int(running[0]['port'])
    return 'single', daemon.DEFAULT_PORT


def format_status(records: list[dict[str, Any]]) -> str:
    header = f"{'PORT':<8} {'PID':<8} {'CHIP':<10} {'DETECTED':<12} {'MODE':<12} URL"
    lines = [header, '-' * len(header)]
    for info in records:
        detected = info.get('detected_chip') or '-'
        mode = info.get('mode') or '-'
        pid = info.get('pid')
        pid_text = str(pid) if pid is not None else '-'
        url = info.get('url') or '-'
        lines.append(
            f"{info['port']:<8} "
            f"{pid_text:<8} "
            f"{str(info.get('chip', '-')):<10} "
            f"{detected!s:<12} "
            f"{mode!s:<12} "
            f"{url}",
        )
    return '\n'.join(lines) + '\n'


def _status_records(
    port: PortTarget = None,
    *,
    base: Path | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    mode, target = instances.resolve_port(port, base=base)
    registry_base = _registry_base(base)
    if mode == 'single':
        port_int = int(target)
        info = daemon.daemon_status(port_int, registry_base)
        if info['running']:
            return info
        for handle in _FOREGROUND_HANDLES:
            fg = _foreground_status(handle)
            if fg is not None and int(fg['port']) == port_int:
                return fg
        return info

    if isinstance(target, list):
        return target
    return []


def status(
    port: PortTarget = None,
    *,
    format: StatusFormat = 'data',
    file: TextIO | None = None,
    base: Path | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | str | None:
    data = _status_records(port, base=base)
    if format == 'data':
        return data
    if format == 'json':
        if isinstance(data, dict):
            payload: dict[str, Any] = data
        else:
            payload = {'instances': data}
        text = json.dumps(payload, indent=2) + '\n'
    else:
        if isinstance(data, dict):
            records = [data]
        else:
            records = data
        text = format_status(records)
    if file is not None:
        file.write(text)
        return None
    return text


def url(
    port: PortTarget = None,
    *,
    bind: str = daemon.DEFAULT_BIND,
    base: Path | None = None,
) -> str | list[tuple[int, str]]:
    mode, target = instances.resolve_port(port, base=base)
    registry_base = _registry_base(base)
    if mode == 'single':
        port_int = int(target)
        state = daemon.read_state(port_int, registry_base)
        if state and state.get('url'):
            return str(state['url'])
        for handle in _FOREGROUND_HANDLES:
            fg = _foreground_status(handle)
            if fg is not None and int(fg['port']) == port_int and fg.get('url'):
                return str(fg['url'])
        return daemon.socket_url(port_int, bind)

    records = target if isinstance(target, list) else []
    pairs: list[tuple[int, str]] = []
    for info in records:
        url_value = info.get('url')
        if url_value:
            pairs.append((int(info['port']), str(url_value)))
    return pairs


def port(
    port: PortTarget = None,
    *,
    base: Path | None = None,
) -> int | list[int]:
    mode, target = instances.resolve_port(port, base=base)
    if mode == 'single':
        return int(target)
    records = target if isinstance(target, list) else []
    return [int(info['port']) for info in records]


def stop(
    port: PortTarget = None,
    *,
    base: Path | None = None,
) -> list[int]:
    mode, target = instances.resolve_port(port, base=base)
    stopped: list[int] = []
    if mode == 'single':
        port_int = int(target)
        for handle in list(_FOREGROUND_HANDLES):
            if handle._port == port_int and handle._session._started:  # noqa: SLF001
                handle.stop()
                stopped.append(port_int)
                return stopped
        if daemon.stop_daemon(port_int, _registry_base(base)):
            stopped.append(port_int)
        return stopped

    records = target if isinstance(target, list) else []
    for info in records:
        port_int = int(info['port'])
        for handle in list(_FOREGROUND_HANDLES):
            if handle._port == port_int and handle._session._started:  # noqa: SLF001
                handle.stop()
                stopped.append(port_int)
                break
        else:
            if daemon.stop_daemon(port_int, _registry_base(base)):
                stopped.append(port_int)
    return stopped


def erase_flash(
    port: PortTarget = None,
    *,
    base: Path | None = None,
) -> list[int]:
    mode, target = instances.resolve_port(port, base=base)
    registry_base = _registry_base(base)
    if mode == 'single':
        return daemon.erase_flash(port=int(target), base=registry_base)
    if not isinstance(target, list) or not target:
        raise RuntimeError('no mock bootloader daemon is running')
    erased: list[int] = []
    for info in target:
        erased.extend(daemon.erase_flash(port=int(info['port']), base=registry_base))
    return erased


class _Instances:
    resolve_port = staticmethod(resolve_port)
    status = staticmethod(status)
    url = staticmethod(url)
    port = staticmethod(port)
    stop = staticmethod(stop)
    erase_flash = staticmethod(erase_flash)
    format_status = staticmethod(format_status)


instances = _Instances()

__all__ = [
    'PortTarget',
    'StatusFormat',
    'format_status',
    'instances',
    'erase_flash',
    'port',
    'resolve_port',
    'status',
    'stop',
    'url',
]
