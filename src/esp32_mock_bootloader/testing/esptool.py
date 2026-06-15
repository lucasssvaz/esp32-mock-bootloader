# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""esptool CLI integration helpers for upload tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from esp32_mock_bootloader.testing import constants


def esptool_available() -> bool:
    if shutil.which('esptool'):
        return True
    try:
        subprocess.run(
            [sys.executable, '-m', 'esptool', 'version'],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def run_esptool(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    if shutil.which('esptool'):
        cmd = ['esptool'] + list(args)
    else:
        cmd = [sys.executable, '-m', 'esptool'] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def create_fake_binary(path: Path, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x00' * size)
    return path


def create_pattern_binary(path: Path, size: int, pattern: int = 0xAA) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes([pattern & 0xFF] * size))
    return path


def _write_flash_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (
        f'rc={result.returncode}\nstdout: {result.stdout[-500:]}\n'
        f'stderr: {result.stderr[-300:]}'
    )


def _write_flash_ok(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 0 or 'Wrote' not in result.stdout:
        return False
    if 'Hash of data verified.' in result.stdout:
        return True
    return (
        'Verifying written data' in result.stdout
        and 'Fatal error' not in result.stdout
        and 'Fatal error' not in result.stderr
    )


def write_flash_at(
    chip: str,
    address: int,
    bin_path: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
    no_stub: bool = False,
    diff_with: str | None = None,
    trust_flash_content: bool = False,
    timeout: float = 60.0,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run esptool write-flash at a specific offset with optional --diff-with."""
    if (port is None) == (pty_path is None):
        raise ValueError('exactly one of port or pty_path is required')
    esptool_port = pty_path if pty_path is not None else f'socket://localhost:{port}'
    args = [
        '--chip', chip,
        '--port', esptool_port,
        '--before', 'no-reset',
        '--after', 'no-reset',
    ]
    if no_stub:
        args.append('--no-stub')
    args.append('write-flash')
    args.extend(extra_args)
    if trust_flash_content:
        args.append('--trust-flash-content')
    args.extend([hex(address), bin_path])
    if diff_with is not None:
        args.extend(['--diff-with', diff_with])
    return run_esptool(*args, timeout=timeout)


def write_flash(
    chip: str,
    bin_path: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
    no_stub: bool = False,
    timeout: float = 30.0,
    extra_args: tuple[str, ...] = (),
) -> tuple[bool, str]:
    if (port is None) == (pty_path is None):
        raise ValueError('exactly one of port or pty_path is required')
    esptool_port = pty_path if pty_path is not None else f'socket://localhost:{port}'
    args = [
        '--chip', chip,
        '--port', esptool_port,
        '--before', 'no-reset',
        '--after', 'no-reset',
    ]
    if no_stub:
        args.append('--no-stub')
    args.extend([
        'write-flash',
        *extra_args,
        hex(constants.FLASH_APP_OFFSET), bin_path,
    ])
    result = run_esptool(*args, timeout=timeout)
    return _write_flash_ok(result), _write_flash_detail(result)


def write_flash_no_stub(
    chip: str,
    bin_path: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """write-flash with --no-stub (ROM MD5: 32-byte hex response)."""
    return write_flash(
        chip, bin_path, port=port, pty_path=pty_path, no_stub=True, timeout=timeout,
    )


def write_flash_with_stub(
    chip: str,
    bin_path: str,
    *,
    port: int | None = None,
    pty_path: str | None = None,
    timeout: float = 30.0,
    extra_args: tuple[str, ...] = ('-z',),
) -> tuple[bool, str]:
    """write-flash with default stub (binary MD5: 16-byte digest response)."""
    return write_flash(
        chip,
        bin_path,
        port=port,
        pty_path=pty_path,
        no_stub=False,
        timeout=timeout,
        extra_args=extra_args,
    )


# Backward-compatible aliases matching prior conftest names.
esptool_write_flash = write_flash
esptool_write_flash_at = write_flash_at
esptool_write_flash_no_stub = write_flash_no_stub
esptool_write_flash_with_stub = write_flash_with_stub

__all__ = [
    'create_fake_binary',
    'create_pattern_binary',
    'esptool_available',
    'esptool_write_flash',
    'esptool_write_flash_at',
    'esptool_write_flash_no_stub',
    'esptool_write_flash_with_stub',
    'run_esptool',
    'write_flash',
    'write_flash_at',
    'write_flash_no_stub',
    'write_flash_with_stub',
]
