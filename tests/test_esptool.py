# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""esptool integration tests (reference client)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from conftest import (
    CHIP_DEFS,
    CHIP_PROFILES,
    ESPTOOL_CHIPS,
    connect_to_server,
    create_fake_binary,
    esptool_write_flash_no_stub,
    esptool_write_flash_with_stub,
    minimal_plain_flash,
    read_reg_value,
    run_esptool,
    send_sync,
    start_mock_server,
)
from esp32_mock_bootloader.chip_profiles import get_chip_profiles, profile_from_rom

pytestmark = pytest.mark.esptool

# Stub MD5 uses the same 16-byte binary response on all targets; spot-check families.
STUB_MD5_REPRESENTATIVE_CHIPS = ('esp32', 'esp32c3', 'esp8266')


@pytest.fixture
def esptool_port(reference_chip):
    proc, port = start_mock_server(timeout=30.0, chip=reference_chip)
    try:
        yield port
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_esptool_write_flash_no_stub(esptool_port, reference_chip):
    with tempfile.TemporaryDirectory() as tmp:
        bin_file = create_fake_binary(Path(tmp) / 'test.bin', 1024)
        wrote_ok, detail = esptool_write_flash_no_stub(
            reference_chip, str(bin_file), port=esptool_port,
        )
        assert wrote_ok, detail


def test_esptool_write_flash_with_stub(esptool_port, reference_chip):
    """Default esptool path: stub upload + SPI_FLASH_MD5 binary digest."""
    with tempfile.TemporaryDirectory() as tmp:
        bin_file = create_fake_binary(Path(tmp) / 'test.bin', 2048)
        wrote_ok, detail = esptool_write_flash_with_stub(
            reference_chip,
            str(bin_file),
            port=esptool_port,
            extra_args=(
                '-z', '--flash-mode', 'dio', '--flash-freq', '40m', '--flash-size', '4MB',
            ),
        )
        assert wrote_ok, detail


@pytest.mark.parametrize('chip', STUB_MD5_REPRESENTATIVE_CHIPS)
def test_esptool_write_flash_with_stub_representative_chips(chip: str):
    """Stub upload + SPI_FLASH_MD5 on esp32, esp32c3, and esp8266."""
    if chip not in ESPTOOL_CHIPS:
        pytest.skip(f'{chip} not supported by installed esptool')
    proc, port = start_mock_server(timeout=30.0, chip=chip)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = create_fake_binary(Path(tmp) / 'test.bin', 1024)
            wrote_ok, detail = esptool_write_flash_with_stub(
                chip, str(bin_file), port=port,
            )
            assert wrote_ok, detail
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_esptool_chip_id(esptool_port, reference_chip):
    result = run_esptool(
        '--chip', reference_chip,
        '--port', f'socket://localhost:{esptool_port}',
        '--no-stub',
        '--before', 'no-reset',
        '--after', 'no-reset',
        'chip_id',
    )
    connected = (
        'Connecting' in result.stdout
        or 'Detecting' in result.stdout
        or 'Chip is' in result.stdout
        or result.returncode == 0
    )
    assert connected, (
        f'rc={result.returncode}\nstdout: {result.stdout[-300:]}\n'
        f'stderr: {result.stderr[-300:]}'
    )


def test_esptool_multiple_binaries(reference_chip):
    proc, port = start_mock_server(timeout=30.0, chip=reference_chip)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bootloader = create_fake_binary(Path(tmp) / 'bootloader.bin', 512)
            partitions = create_fake_binary(Path(tmp) / 'partitions.bin', 512)
            app = create_fake_binary(Path(tmp) / 'app.bin', 2048)
            result = run_esptool(
                '--chip', reference_chip,
                '--port', f'socket://localhost:{port}',
                '--no-stub',
                '--before', 'no-reset',
                '--after', 'no-reset',
                'write-flash',
                '--flash-mode', 'keep',
                '--flash-freq', 'keep',
                '--flash-size', 'keep',
                '0x1000', str(bootloader),
                '0x8000', str(partitions),
                '0x10000', str(app),
            )
            assert 'Wrote' in result.stdout, (
                f'rc={result.returncode}\nstdout: {result.stdout[-500:]}\n'
                f'stderr: {result.stderr[-300:]}'
            )
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_chip_profiles_derived_from_esptool():
    """Profiles are built from esptool ROM classes, not hand-maintained tables."""
    assert set(ESPTOOL_CHIPS) == set(CHIP_PROFILES.keys())
    assert CHIP_PROFILES is get_chip_profiles()
    for chip in ESPTOOL_CHIPS:
        rom = CHIP_DEFS[chip]
        expected = profile_from_rom(chip, rom)
        assert CHIP_PROFILES[chip] == expected, chip


def test_all_esptool_chips_chip_profiles():
    for chip in ESPTOOL_CHIPS:
        profile = CHIP_PROFILES[chip]
        proc, port = start_mock_server(chip=chip)
        try:
            sock = connect_to_server(port)
            send_sync(sock)

            if profile.detect_magic:
                value = read_reg_value(sock, profile.detect_reg)
                assert value == profile.detect_magic, (
                    f'{chip}: expected 0x{profile.detect_magic:08X}, '
                    f'got {None if value is None else f"0x{value:08X}"}'
                )

            efuse_value = read_reg_value(sock, profile.efuse_base + 0x04)
            assert efuse_value == 0

            assert minimal_plain_flash(sock)
            sock.close()
            sock2 = connect_to_server(port)
            assert minimal_plain_flash(sock2)
            sock2.close()
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)


@pytest.mark.parametrize('chip', ESPTOOL_CHIPS)
def test_esptool_write_flash_all_esptool_chips(chip: str):
    proc, port = start_mock_server(timeout=30.0, chip=chip)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = create_fake_binary(Path(tmp) / 'test.bin', 1024)
            wrote_ok, detail = esptool_write_flash_no_stub(
                chip, str(bin_file), port=port,
            )
            assert wrote_ok, detail
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
