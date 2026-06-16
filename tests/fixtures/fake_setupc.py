#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Minimal setupc.exe stand-in for com0com unit tests (cross-platform)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_PORT_RE = re.compile(r'PortName=([^,\s]+)', re.IGNORECASE)
_DEFAULT_STATE = Path(__file__).with_name('.fake_setupc_state.json')


def _state_path() -> Path:
    override = os.environ.get('ESP32_MOCK_FAKE_SETUPC_STATE')
    return Path(override) if override else _DEFAULT_STATE


def _load_pairs() -> list[list[str]]:
    path = _state_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return data
    return []


def _save_pairs(pairs: list[list[str]]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pairs), encoding='utf-8')


def _list_output(pairs: list[list[str]]) -> str:
    lines: list[str] = []
    for idx, (server, peer) in enumerate(pairs):
        lines.append(f'CNCA{idx} PortName={server},EmuBR=yes,EmuOverrun=yes')
        lines.append(f'CNCB{idx} PortName={peer},EmuBR=yes,EmuOverrun=yes')
    return '\n'.join(lines) + ('\n' if lines else '')


def _install(line: str, pairs: list[list[str]]) -> None:
    ports = _PORT_RE.findall(line)
    if len(ports) >= 2:
        pairs.append([ports[0], ports[1]])


def _remove(pair_id: int, pairs: list[list[str]]) -> None:
    if 0 <= pair_id < len(pairs):
        del pairs[pair_id]


def main() -> int:
    pairs = _load_pairs()
    for raw in sys.stdin:
        line = raw.strip()
        if not line or line.lower() == 'quit':
            break
        low = line.lower()
        if low == 'list':
            sys.stdout.write(_list_output(pairs))
            sys.stdout.flush()
        elif low.startswith('install '):
            _install(line, pairs)
            _save_pairs(pairs)
            sys.stdout.write(' install completed\n')
            sys.stdout.flush()
        elif low.startswith('remove '):
            try:
                _remove(int(line.split()[1]), pairs)
            except (IndexError, ValueError):
                sys.stderr.write('error: bad remove command\n')
                return 1
            _save_pairs(pairs)
            sys.stdout.write(' remove completed\n')
            sys.stdout.flush()
        else:
            sys.stderr.write(f'error: unknown command: {line}\n')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
