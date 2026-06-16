#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Verify pytest-xdist + subprocess coverage matches serial collection."""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / 'reports'
XML_PATH = REPORTS / 'coverage.xml'

# Subprocess-heavy tests that exercise server.handle_client via mock_server.
SUBSET = [
    'tests/test_protocol.py::test_stub_read_flash',
    'tests/test_protocol.py::test_stub_erase_flash',
    'tests/test_protocol.py::test_stub_erase_region',
    'tests/test_protocol.py::test_rom_read_flash_slow',
    'tests/test_protocol.py::test_flash_data_checksum_error',
    'tests/test_protocol_edge.py::test_mem_data_checksum_error_stub_mode',
    'tests/test_protocol_edge.py::test_flash_defl_data_checksum_error',
    'tests/test_protocol_edge.py::test_rom_stub_only_commands_rejected_before_stub',
]

MAX_GAP_PCT = 2.0


def _server_line_rate(xml_path: Path) -> float:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    for cls in root.findall('.//class'):
        filename = cls.get('filename', '')
        if filename.endswith('server.py'):
            return float(cls.get('line-rate', '0')) * 100.0
    raise SystemExit(f'server.py not found in {xml_path}')


def _run_pytest(n_workers: str, xml_out: Path) -> None:
    REPORTS.mkdir(exist_ok=True)
    cmd = [
        sys.executable, '-m', 'pytest',
        f'-n{n_workers}',
        '--cov-config=pyproject.toml',
        '--cov=esp32_mock_bootloader',
        f'--cov-report=xml:{xml_out}',
        '--cov-report=',
        '-q',
        *SUBSET,
    ]
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--max-gap',
        type=float,
        default=MAX_GAP_PCT,
        help=f'Max allowed parallel-vs-serial server %% gap (default {MAX_GAP_PCT})',
    )
    args = parser.parse_args()

    parallel_xml = REPORTS / 'coverage-parallel-subset.xml'
    serial_xml = REPORTS / 'coverage-serial-subset.xml'

    _run_pytest('auto', parallel_xml)
    parallel_rate = _server_line_rate(parallel_xml)

    _run_pytest('0', serial_xml)
    serial_rate = _server_line_rate(serial_xml)

    gap = serial_rate - parallel_rate
    print(f'server.py line coverage: parallel={parallel_rate:.1f}% serial={serial_rate:.1f}% gap={gap:.1f}%')

    if gap > args.max_gap:
        print(
            f'ERROR: parallel coverage is {gap:.1f}% below serial (limit {args.max_gap}%). '
            'Check coverage subprocess patch / pytest-cov xdist combine.',
            file=sys.stderr,
        )
        return 1

    # Also sanity-check the main CI report if present (post full suite).
    if XML_PATH.is_file():
        main_rate = _server_line_rate(XML_PATH)
        print(f'server.py line coverage (full suite): {main_rate:.1f}%')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
