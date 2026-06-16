#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""List uncovered lines and functions from reports/coverage.xml."""

from __future__ import annotations

import argparse
import ast
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_XML = ROOT / 'reports' / 'coverage.xml'
DEFAULT_JSON = ROOT / 'reports' / 'coverage-gaps.json'
SRC = ROOT / 'src' / 'esp32_mock_bootloader'


@dataclass
class ModuleGaps:
    module: str
    path: str
    line_rate: float
    statements: int
    missed: int
    missed_lines: list[int]
    missed_ranges: list[str]
    functions: list[str]


def _module_key(filename: str) -> str:
    marker = 'esp32_mock_bootloader/'
    if marker in filename:
        return filename.split(marker, 1)[1].removesuffix('.py')
    return Path(filename).name.removesuffix('.py')


def _compress_ranges(lines: list[int]) -> list[str]:
    if not lines:
        return []
    sorted_lines = sorted(lines)
    ranges: list[str] = []
    start = prev = sorted_lines[0]
    for line_no in sorted_lines[1:]:
        if line_no == prev + 1:
            prev = line_no
            continue
        ranges.append(f'{start}-{prev}' if start != prev else str(start))
        start = prev = line_no
    ranges.append(f'{start}-{prev}' if start != prev else str(start))
    return ranges


def _functions_by_missed_lines(source: Path, missed: set[int]) -> list[tuple[str, int]]:
    """Return (function, missed_line_count) sorted by most missed."""
    if not missed or not source.is_file():
        return []
    tree = ast.parse(source.read_text(encoding='utf-8'))
    counts: dict[str, int] = {}

    def record(name: str, node: ast.AST) -> None:
        end = getattr(node, 'end_lineno', None) or node.lineno
        span = set(range(node.lineno, end + 1))
        hit = len(span & missed)
        if hit:
            counts[name] = counts.get(name, 0) + hit

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    record(f'{node.name}.{child.name}', child)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            record(node.name, node)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _function_names(ranked: list[tuple[str, int]], *, limit: int = 15) -> list[str]:
    names = [f'{name} ({count})' for name, count in ranked[:limit]]
    if len(ranked) > limit:
        names.append(f'… (+{len(ranked) - limit} more)')
    return names


def parse_coverage_gaps(xml_path: Path, *, src_root: Path = SRC) -> list[ModuleGaps]:
    root = ET.parse(xml_path).getroot()
    modules: list[ModuleGaps] = []

    for cls in root.findall('.//class'):
        filename = cls.get('filename', '')
        if not filename or '/testing/' in filename:
            continue
        module = _module_key(filename)
        lines = cls.findall('lines/line')
        missed_lines = sorted(
            int(line.get('number', '0'))
            for line in lines
            if int(line.get('hits', '0')) == 0
        )
        if not missed_lines:
            continue
        source = src_root / f'{module}.py'
        missed_set = set(missed_lines)
        ranked = _functions_by_missed_lines(source, missed_set)
        modules.append(ModuleGaps(
            module=module,
            path=filename,
            line_rate=round(float(cls.get('line-rate', '0')) * 100, 1),
            statements=len(lines),
            missed=len(missed_lines),
            missed_lines=missed_lines,
            missed_ranges=_compress_ranges(missed_lines),
            functions=_function_names(ranked),
        ))

    modules.sort(key=lambda item: (-item.missed, item.module))
    return modules


def format_text_report(modules: list[ModuleGaps], *, max_ranges: int = 30) -> str:
    if not modules:
        return 'All measured product modules are fully covered.\n'

    total_missed = sum(module.missed for module in modules)
    lines = [
        f'Coverage gaps — {len(modules)} module(s), {total_missed} uncovered line(s)',
        f'(product code only; {SRC.name}/testing/ omitted)',
        '',
    ]
    for module in modules:
        lines.append(
            f'{module.module} — {module.line_rate}% '
            f'({module.missed}/{module.statements} lines missed)',
        )
        if module.functions:
            lines.append(f'  functions: {", ".join(module.functions)}')
        ranges = module.missed_ranges
        if len(ranges) > max_ranges:
            shown = ', '.join(ranges[:max_ranges])
            lines.append(
                f'  lines: {shown}, … (+{len(ranges) - max_ranges} more ranges)',
            )
        else:
            lines.append(f'  lines: {", ".join(ranges)}')
        lines.append('')
    return '\n'.join(lines)


def format_github_summary(
    modules: list[ModuleGaps],
    *,
    top: int = 8,
    max_ranges: int = 12,
) -> str:
    if not modules:
        return '### Coverage gaps\n\nAll measured product modules are fully covered.\n'

    total_missed = sum(module.missed for module in modules)
    out = [
        '### Coverage gaps',
        '',
        f'{len(modules)} module(s) with uncovered lines '
        f'(**{total_missed}** lines total; `testing/` omitted).',
        '',
    ]
    for module in modules[:top]:
        fn = ', '.join(f'`{name}`' for name in module.functions[:12])
        ranges = module.missed_ranges
        if len(ranges) > max_ranges:
            line_text = ', '.join(f'`{r}`' for r in ranges[:max_ranges])
            line_text += f', … (+{len(ranges) - max_ranges} ranges)'
        else:
            line_text = ', '.join(f'`{r}`' for r in ranges) or '—'
        out.extend([
            f'<details>',
            f'<summary><code>{module.module}</code> — {module.line_rate}% '
            f'({module.missed} missed)</summary>',
            '',
            f'**Functions:** {fn or "—"}',
            '',
            f'**Lines:** {line_text}',
            '',
            '</details>',
            '',
        ])
    if len(modules) > top:
        out.append(f'_+{len(modules) - top} more module(s); see `coverage-gaps.json` artifact or local report._')
        out.append('')
    return '\n'.join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xml', type=Path, default=DEFAULT_XML, help='coverage.xml path')
    parser.add_argument('--json', type=Path, default=DEFAULT_JSON, help='write machine-readable report')
    parser.add_argument(
        '--github-summary',
        type=Path,
        metavar='FILE',
        help='append Markdown gap summary (e.g. $GITHUB_STEP_SUMMARY)',
    )
    parser.add_argument(
        '--max-ranges',
        type=int,
        default=30,
        help='max line ranges per module in text output',
    )
    parser.add_argument(
        '--top',
        type=int,
        default=8,
        help='modules shown in GitHub summary',
    )
    args = parser.parse_args(argv)

    if not args.xml.is_file():
        print(f'Missing {args.xml}; run pytest with --cov-report=xml:{args.xml}', file=sys.stderr)
        return 1

    modules = parse_coverage_gaps(args.xml)
    report = format_text_report(modules, max_ranges=args.max_ranges)
    print(report, end='')

    payload = {
        'modules': [asdict(module) for module in modules],
        'module_count': len(modules),
        'missed_line_count': sum(module.missed for module in modules),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote {args.json}', file=sys.stderr)

    if args.github_summary:
        args.github_summary.parent.mkdir(parents=True, exist_ok=True)
        with args.github_summary.open('a', encoding='utf-8') as handle:
            handle.write(format_github_summary(modules, top=args.top))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
