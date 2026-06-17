#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Compare coverage.xml against reports/coverage-baseline.json and write a summary."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / 'reports' / 'coverage-baseline.json'
XML_PATH = ROOT / 'reports' / 'coverage.xml'
SUMMARY_PATH = ROOT / 'reports' / 'coverage-summary.json'


def _module_key(filename: str) -> str:
    """Map coverage.xml class filename to baseline package key."""
    marker = 'esp32_mock_bootloader/'
    if marker in filename:
        return filename.split(marker, 1)[1].removesuffix('.py')
    return Path(filename).name.removesuffix('.py')


def parse_coverage_xml(path: Path) -> dict[str, float]:
    tree = ET.parse(path)
    root = tree.getroot()
    packages: dict[str, float] = {}
    for package in root.findall('.//package'):
        name = package.get('name', '')
        if 'esp32_mock_bootloader' not in name:
            continue
        line_rate = float(package.get('line-rate', '0'))
        packages['esp32_mock_bootloader'] = round(line_rate * 100, 1)
        for cls in package.findall('classes/class'):
            filename = cls.get('filename', '')
            if not filename:
                continue
            key = _module_key(filename)
            packages[key] = round(float(cls.get('line-rate', '0')) * 100, 1)
    total = root.get('line-rate')
    total_pct = round(float(total) * 100, 1) if total else 0.0
    return {'total': total_pct, 'packages': packages}


def _status_icon(measured: float, baseline: float | None) -> str:
    if baseline is None:
        return ':grey_question:'
    return ':white_check_mark:' if measured >= baseline else ':x:'


def _badge_color(measured: float, baseline: float) -> str:
    return 'brightgreen' if measured >= baseline else 'red'


def format_github_summary(summary: dict, *, artifact_note: bool = False) -> str:
    measured = summary['measured']
    baseline = summary['baseline']
    min_total = float(baseline.get('total', 0))
    min_packages: dict[str, float] = baseline.get('packages', {})
    passed = summary['passed']

    lines = [
        '### Coverage',
        '',
        f'![coverage](https://img.shields.io/badge/coverage-{measured["total"]}%25-'
        f'{_badge_color(measured["total"], min_total)})',
        '',
        f'**{"Passed" if passed else "Below baseline"}** — '
        f'total **{measured["total"]}%** (baseline {min_total}%)',
        '',
        '| Module | Measured | Baseline | |',
        '| --- | ---: | ---: | --- |',
        f'| **Total** | **{measured["total"]}%** | **{min_total}%** | '
        f'{_status_icon(measured["total"], min_total)} |',
    ]

    module_keys = sorted(
        key for key in measured['packages'] if key != 'esp32_mock_bootloader'
    )
    for key in module_keys:
        actual = measured['packages'][key]
        floor = min_packages.get(key)
        floor_text = f'{floor}%' if floor is not None else '—'
        lines.append(
            f'| `{key}` | {actual}% | {floor_text} | {_status_icon(actual, floor)} |',
        )

    if summary['failures']:
        lines.extend(['', '**Failures**', ''])
        lines.extend(f'- {item}' for item in summary['failures'])

    if artifact_note:
        lines.extend([
            '',
            'Download the **coverage-html** artifact for the interactive HTML report.',
        ])

    lines.append('')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--github-summary',
        type=Path,
        metavar='FILE',
        help='Append a Markdown coverage table to FILE (e.g. $GITHUB_STEP_SUMMARY)',
    )
    parser.add_argument(
        '--no-enforce',
        action='store_true',
        help='Report results but always exit 0 (for display-only job summaries)',
    )
    parser.add_argument(
        '--artifact-note',
        action='store_true',
        help='Mention the coverage-html artifact in the GitHub summary',
    )
    args = parser.parse_args(argv)

    if not XML_PATH.is_file():
        print(f'Missing {XML_PATH}; run pytest with --cov-report=xml:{XML_PATH}', file=sys.stderr)
        return 0 if args.no_enforce else 1
    if not BASELINE_PATH.is_file():
        print(f'Missing {BASELINE_PATH}', file=sys.stderr)
        return 0 if args.no_enforce else 1

    measured = parse_coverage_xml(XML_PATH)
    baseline = json.loads(BASELINE_PATH.read_text(encoding='utf-8'))
    min_total = float(baseline.get('total', 0))
    min_packages: dict[str, float] = baseline.get('packages', {})

    failures: list[str] = []
    if measured['total'] < min_total:
        failures.append(f'total {measured["total"]}% < baseline {min_total}%')

    for pkg, minimum in min_packages.items():
        actual = measured['packages'].get(pkg)
        if actual is None:
            failures.append(f'{pkg}: not measured')
        elif actual < minimum:
            failures.append(f'{pkg}: {actual}% < baseline {minimum}%')

    summary = {
        'measured': measured,
        'baseline': baseline,
        'passed': not failures,
        'failures': failures,
    }
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')

    if args.github_summary:
        args.github_summary.parent.mkdir(parents=True, exist_ok=True)
        with args.github_summary.open('a', encoding='utf-8') as handle:
            handle.write(format_github_summary(summary, artifact_note=args.artifact_note))

    print(f'Coverage total: {measured["total"]}% (baseline {min_total}%)')
    for pkg in sorted(measured['packages']):
        floor = min_packages.get(pkg, '-')
        print(f'  {pkg}: {measured["packages"][pkg]}% (baseline {floor}%)')

    if failures:
        print('Coverage below baseline:', file=sys.stderr)
        for item in failures:
            print(f'  - {item}', file=sys.stderr)
        return 0 if args.no_enforce else 1
    print(f'Summary written to {SUMMARY_PATH}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
