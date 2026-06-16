# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""com0com virtual COM pair management on Windows (setupc.exe)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

SETUPC_CANDIDATES = (
    Path(r'C:\Program Files (x86)\com0com\setupc.exe'),
    Path(r'C:\Program Files\com0com\setupc.exe'),
)

CNCA_RE = re.compile(r'^(CNCA(\d+))\s+(.+)$', re.IGNORECASE)
PORT_NAME_RE = re.compile(r'PortName=([^,\s]+)', re.IGNORECASE)
REAL_PORT_RE = re.compile(r'RealPortName=([^,\s]+)', re.IGNORECASE)


@dataclass(frozen=True)
class ComPair:
    pair_id: int
    server: str
    peer: str


class Com0ComError(RuntimeError):
    pass


def find_setupc() -> Path:
    override = os.environ.get('ESP32_MOCK_SETUPC')
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise Com0ComError(f'ESP32_MOCK_SETUPC does not exist: {override}')

    found = shutil.which('setupc')
    if found:
        return Path(found)

    for candidate in SETUPC_CANDIDATES:
        if candidate.is_file():
            return candidate

    raise Com0ComError(
        'setupc.exe not found. Install com0com and/or set ESP32_MOCK_SETUPC.\n'
        'Typical path: C:\\Program Files (x86)\\com0com\\setupc.exe',
    )


def run_setupc(setupc: Path, *commands: str) -> str:
    script = '\n'.join(commands) + '\nquit\n'
    try:
        result = subprocess.run(
            [str(setupc)],
            input=script,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError as exc:
        raise Com0ComError(f'Failed to run {setupc}: {exc}') from exc

    output = (result.stdout or '') + (result.stderr or '')
    lowered = output.lower()
    if result.returncode != 0 or 'access is denied' in lowered or 'requires elevation' in lowered:
        raise Com0ComError(
            f'setupc failed (exit {result.returncode}). '
            'Run from an elevated command prompt.\n'
            f'Commands: {commands!r}\n'
            f'Output:\n{output.strip()}',
        )
    for line in output.splitlines():
        low = line.lower()
        if low.startswith('error') or ' install ' in low and 'failed' in low:
            raise Com0ComError(f'setupc error:\n{output.strip()}')
    return output


def _port_names_from_params(params: str) -> tuple[str | None, str | None]:
    real = REAL_PORT_RE.search(params)
    if real:
        return real.group(1), None
    port = PORT_NAME_RE.search(params)
    if port and port.group(1).upper() != 'COM#':
        return port.group(1), None
    return None, port.group(1) if port else None


def parse_pairs(list_output: str) -> list[tuple[int, str, str]]:
    lines = list_output.splitlines()
    pairs: list[tuple[int, str, str]] = []
    idx = 0
    while idx < len(lines):
        match = CNCA_RE.match(lines[idx].strip())
        if not match:
            idx += 1
            continue
        pair_id = int(match.group(2))
        cnca_port, _ = _port_names_from_params(match.group(3))
        cncb_port = None
        if idx + 1 < len(lines):
            cncb_line = lines[idx + 1].strip()
            if cncb_line.upper().startswith(f'CNCB{pair_id}'):
                cncb_port, _ = _port_names_from_params(cncb_line.split(None, 1)[1])
        if cnca_port and cncb_port:
            pairs.append((pair_id, cnca_port, cncb_port))
        idx += 1
    return pairs


def find_pair_id(list_output: str, *port_names: str) -> int | None:
    wanted = {name.upper() for name in port_names}
    for pair_id, cnca, cncb in parse_pairs(list_output):
        if {cnca.upper(), cncb.upper()} == wanted:
            return pair_id
    return None


def find_paired_port(port: str) -> str | None:
    """Return the other end of a com0com pair, or None if unknown."""
    try:
        setupc = find_setupc()
    except Com0ComError:
        return None
    listing = run_setupc(setupc, 'list')
    wanted = port.upper()
    for _pair_id, cnca, cncb in parse_pairs(listing):
        if cnca.upper() == wanted:
            return cncb
        if cncb.upper() == wanted:
            return cnca
    return None


def wait_for_ports(*port_names: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    wanted = {name.upper() for name in port_names}
    while time.monotonic() < deadline:
        from serial.tools import list_ports

        present = {
            port.device.upper()
            for port in list_ports.comports()
            if port.device.upper().startswith('COM')
        }
        if wanted.issubset(present):
            return
        time.sleep(0.2)
    raise Com0ComError(f'Timed out waiting for ports: {", ".join(port_names)}')


def install_pair(setupc: Path, server: str, peer: str) -> ComPair:
    listing = run_setupc(setupc, 'list')
    existing = find_pair_id(listing, server, peer)
    if existing is not None:
        run_setupc(setupc, f'remove {existing}')

    install_cmd = (
        f'install PortName={server},EmuBR=yes,EmuOverrun=yes '
        f'PortName={peer},EmuBR=yes,EmuOverrun=yes'
    )
    output = run_setupc(setupc, install_cmd, 'list')
    pair_id = find_pair_id(output, server, peer)
    if pair_id is None:
        raise Com0ComError(
            f'Installed com0com pair but could not find {server}/{peer} in:\n{output.strip()}',
        )
    wait_for_ports(server, peer)
    return ComPair(pair_id=pair_id, server=server, peer=peer)


def remove_pair(setupc: Path, pair: ComPair) -> None:
    run_setupc(setupc, f'remove {pair.pair_id}')


def keep_com_pair() -> bool:
    return os.environ.get('ESP32_MOCK_KEEP_COM_PAIR', '').lower() in ('1', 'true', 'yes')


@contextmanager
def com0com_pair(server: str, peer: str) -> Iterator[ComPair]:
    setupc = find_setupc()
    pair = install_pair(setupc, server, peer)
    try:
        yield pair
    finally:
        if not keep_com_pair():
            try:
                remove_pair(setupc, pair)
            except Com0ComError:
                pass
