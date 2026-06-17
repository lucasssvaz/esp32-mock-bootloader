# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""esptool integration and connect-fidelity tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from esp32_mock_bootloader import constants, protocol_client
from tests.helpers import esptool
from esp32_mock_bootloader import mock_bootloader, protocol
from esp32_mock_bootloader.advanced import protocol as protocol_api
from esp32_mock_bootloader import chips
from esp32_mock_bootloader import registers

pytestmark = pytest.mark.esptool

# Stub MD5 uses the same 16-byte binary response on all targets; spot-check families.
STUB_MD5_REPRESENTATIVE_CHIPS = ('esp32', 'esp32c3', 'esp8266')

# esptool --diff-with compares 4 KiB sectors; four sectors = 16 KiB (matches esptool tests).
DIFF_FLASH_SIZE = protocol.FLASH_SECTOR_SIZE * constants.DIFF_FLASH_SECTOR_COUNT
DIFF_FLASH_ADDR = constants.FLASH_APP_OFFSET


def test_esptool_write_flash_no_stub(esptool_port, reference_chip):
    with tempfile.TemporaryDirectory() as tmp:
        bin_file = esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
        wrote_ok, detail = esptool.write_flash_no_stub(reference_chip, str(bin_file), port=esptool_port.port())
        assert wrote_ok, detail


def test_esptool_write_flash_with_stub(esptool_port, reference_chip):
    """Default esptool path: stub upload + SPI_FLASH_MD5 binary digest."""
    with tempfile.TemporaryDirectory() as tmp:
        bin_file = esptool.create_fake_binary(Path(tmp) / 'test.bin', 2048)
        wrote_ok, detail = esptool.write_flash_with_stub(
            reference_chip,
            str(bin_file),
            port=esptool_port.port(),
            extra_args=(
                '-z', '--flash-mode', 'dio', '--flash-freq', '40m', '--flash-size', '4MB',
            ),
        )
        assert wrote_ok, detail


@pytest.mark.parametrize('chip', STUB_MD5_REPRESENTATIVE_CHIPS)
def test_esptool_write_flash_with_stub_representative_chips(chip: str):
    """Stub upload + SPI_FLASH_MD5 on esp32, esp32c3, and esp8266."""
    if chip not in constants.ESPTOOL_CHIPS:
        pytest.skip(f'{chip} not supported by installed esptool')
    with mock_bootloader(chip=chip, timeout=30.0, exit_on_disconnect=False, mode='foreground') as mock:
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
            wrote_ok, detail = esptool.write_flash_with_stub(
                chip, str(bin_file), port=mock.port(),
            )
            assert wrote_ok, detail


def test_esptool_chip_id(esptool_port, reference_chip):
    result = esptool.run_esptool(
        '--chip', reference_chip,
        '--port', esptool_port.url(),
        '--no-stub',
        '--before', 'no-reset',
        '--after', 'no-reset',
        'chip-id',
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
    with mock_bootloader(chip=reference_chip, timeout=30.0, exit_on_disconnect=False, mode='foreground') as mock:
        with tempfile.TemporaryDirectory() as tmp:
            bootloader = esptool.create_fake_binary(Path(tmp) / 'bootloader.bin', 512)
            partitions = esptool.create_fake_binary(Path(tmp) / 'partitions.bin', 512)
            app = esptool.create_fake_binary(Path(tmp) / 'app.bin', 2048)
            result = esptool.run_esptool(
                '--chip', reference_chip,
                '--port', f'socket://localhost:{mock.port()}',
                '--no-stub',
                '--before', 'no-reset',
                '--after', 'no-reset',
                'write-flash',
                '--flash-mode', 'keep',
                '--flash-freq', 'keep',
                '--flash-size', 'keep',
                '0x1000', str(bootloader),   # ESP-IDF 2nd-stage bootloader offset
                '0x8000', str(partitions),   # partition table offset
                hex(constants.FLASH_APP_OFFSET), str(app),
            )
            assert 'Wrote' in result.stdout, (
                f'rc={result.returncode}\nstdout: {result.stdout[-500:]}\n'
                f'stderr: {result.stderr[-300:]}'
            )



def test_all_esptool_chips_chip_profiles():
    for chip in constants.ESPTOOL_CHIPS:
        profile = chips.PROFILES[chip]
        with mock_bootloader(chip=chip, exit_on_disconnect=False, mode='foreground') as mock:
            client = protocol_api.connect(mock)
            protocol_api.connect(mock).sync()
            if profile.detect_magic:
                value = protocol_api.connect(mock).read_reg(profile.detect_reg)
                assert value == profile.detect_magic, (
                    f'{chip}: expected 0x{profile.detect_magic:08X}, '
                    f'got {None if value is None else f"0x{value:08X}"}'
                )
            efuse_addr = profile.efuse_base + 0x04
            efuse_value = protocol_api.connect(mock).read_reg(efuse_addr)
            expected = registers.rom_profile(chip).get(efuse_addr, 0)
            assert efuse_value == expected
            assert protocol_api.connect(mock).minimal_plain_flash()
            assert protocol_api.connect(mock).minimal_plain_flash()


@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_esptool_write_flash_all_esptool_chips(chip: str):
    with mock_bootloader(chip=chip, timeout=30.0, exit_on_disconnect=False, mode='foreground') as mock:
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = esptool.create_fake_binary(Path(tmp) / 'test.bin', 1024)
            wrote_ok, detail = esptool.write_flash_no_stub(
                chip, str(bin_file), port=mock.port(),
            )
            assert wrote_ok, detail


def _make_old_new_pair(tmp: Path, *, change_first_sector: bool = True) -> tuple[Path, Path]:
    # 0xAA fill is arbitrary; 0xBB in first 100 bytes forces a sector diff for --diff-with.
    old = esptool.create_pattern_binary(tmp / 'old.bin', DIFF_FLASH_SIZE, 0xAA)
    new_data = bytearray(old.read_bytes())
    if change_first_sector:
        new_data[0:100] = bytes([0xBB] * 100)
    new = tmp / 'new.bin'
    new.write_bytes(new_data)
    return old, new


def test_esptool_diff_with_changed_sectors_no_stub(esptool_port, reference_chip):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old, new = _make_old_new_pair(tmp_path)
        first = esptool.write_flash_at(
            reference_chip, DIFF_FLASH_ADDR, str(old), port=esptool_port.port(), no_stub=True,
        )
        assert first.returncode == 0, first.stderr
        second = esptool.write_flash_at(
            reference_chip,
            DIFF_FLASH_ADDR,
            str(new),
            port=esptool_port.port(),
            diff_with=str(old),
            no_stub=True,
        )
        assert second.returncode == 0, second.stderr
        assert 'Changed data sectors found' in second.stdout


def test_esptool_diff_with_changed_sectors_stub(esptool_port, reference_chip):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old, new = _make_old_new_pair(tmp_path)
        first = esptool.write_flash_at(
            reference_chip,
            DIFF_FLASH_ADDR,
            str(old),
            port=esptool_port.port(),
            extra_args=('-z',),
        )
        assert first.returncode == 0, first.stderr
        second = esptool.write_flash_at(
            reference_chip,
            DIFF_FLASH_ADDR,
            str(new),
            port=esptool_port.port(),
            diff_with=str(old),
            extra_args=('-z',),
        )
        assert second.returncode == 0, second.stderr
        assert 'Changed data sectors found' in second.stdout


def test_esptool_diff_with_identical_sectors_no_stub(esptool_port, reference_chip):
    if reference_chip == 'esp8266':
        pytest.skip('ESP8266 ROM lacks SPI_FLASH_MD5')
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old = esptool.create_pattern_binary(tmp_path / 'old.bin', DIFF_FLASH_SIZE, 0xCC)  # distinct fill per test
        new = tmp_path / 'new.bin'
        new.write_bytes(old.read_bytes())
        first = esptool.write_flash_at(
            reference_chip, DIFF_FLASH_ADDR, str(old), port=esptool_port.port(), no_stub=True,
        )
        assert first.returncode == 0, first.stderr
        second = esptool.write_flash_at(
            reference_chip,
            DIFF_FLASH_ADDR,
            str(new),
            port=esptool_port.port(),
            diff_with=str(old),
            no_stub=True,
        )
        assert second.returncode == 0, second.stderr
        assert 'No changed sectors found' in second.stdout
        assert 'verified' in second.stdout.lower()


def test_esptool_diff_with_identical_sectors_stub(esptool_port, reference_chip):
    """Second upload with --diff-with skips when daemon flash persists across connections."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old = esptool.create_pattern_binary(tmp_path / 'old.bin', DIFF_FLASH_SIZE, 0xCE)
        new = tmp_path / 'new.bin'
        new.write_bytes(old.read_bytes())
        first = esptool.write_flash_at(
            reference_chip,
            DIFF_FLASH_ADDR,
            str(old),
            port=esptool_port.port(),
            extra_args=('-z',),
        )
        assert first.returncode == 0, first.stderr
        second = esptool.write_flash_at(
            reference_chip,
            DIFF_FLASH_ADDR,
            str(new),
            port=esptool_port.port(),
            diff_with=str(old),
            extra_args=('-z',),
        )
        assert second.returncode == 0, second.stderr
        assert 'No changed sectors found' in second.stdout
        assert 'verified' in second.stdout.lower()
        assert 'flashing the whole image' not in second.stdout


def test_esptool_diff_with_trust_flash_content_no_stub(esptool_port, reference_chip):
    if reference_chip == 'esp8266':
        pytest.skip('ESP8266 ROM lacks SPI_FLASH_MD5')
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old = esptool.create_pattern_binary(tmp_path / 'old.bin', DIFF_FLASH_SIZE, 0xDD)  # distinct fill per test
        new = tmp_path / 'new.bin'
        new.write_bytes(old.read_bytes())
        first = esptool.write_flash_at(
            reference_chip, DIFF_FLASH_ADDR, str(old), port=esptool_port.port(), no_stub=True,
        )
        assert first.returncode == 0, first.stderr
        second = esptool.write_flash_at(
            reference_chip,
            DIFF_FLASH_ADDR,
            str(new),
            port=esptool_port.port(),
            no_stub=True,
            diff_with=str(old),
            trust_flash_content=True,
        )
        assert second.returncode == 0, second.stderr
        assert 'skipping write and verification' in second.stdout


def test_esptool_diff_with_fallback_full_reflash_no_stub(esptool_port, reference_chip):
    """Flash mismatch triggers full reflash after fast path MD5 failure."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old, new = _make_old_new_pair(tmp_path)
        # Stage flash with 0xEE so MD5 differs from --diff-with reference (0xAA), forcing full reflash.
        different = esptool.create_pattern_binary(tmp_path / 'different.bin', DIFF_FLASH_SIZE, 0xEE)
        staged = esptool.write_flash_at(
            reference_chip, DIFF_FLASH_ADDR, str(different), port=esptool_port.port(), no_stub=True,
        )
        assert staged.returncode == 0, staged.stderr
        result = esptool.write_flash_at(
            reference_chip,
            DIFF_FLASH_ADDR,
            str(new),
            port=esptool_port.port(),
            no_stub=True,
            diff_with=str(old),
        )
        assert result.returncode == 0, result.stderr
        assert (
            'Reflashing the whole file' in result.stdout
            or 'Verification failed after fast reflash' in result.stdout
            or 'flashing the whole image' in result.stdout
        )


@pytest.mark.parametrize('chip', constants.ESPTOOL_CHIPS)
def test_flash_id_matching_chip_no_protocol_warnings(chip: str):
    with mock_bootloader(chip=chip, timeout=60.0, exit_on_disconnect=False, mode='foreground') as mock:
        result = esptool.run_flash_id(chip, port=mock.port())
        output = result.stdout + result.stderr
        assert result.returncode == 0, output[-800:]
        warns = esptool.forbidden_warnings(output, transport='socket')
        assert warns == [], '\n'.join(warns)
        mac = registers.mac_bytes_for_chip(chip)
        mac_line = ':'.join(f'{b:02x}' for b in mac)
        assert mac_line in output


@pytest.mark.parametrize('chip', ('esp32', 'esp32c3', 'esp8266'))
def test_flash_id_esptool_auto_detects_fixed_mock(chip: str):
    """esptool --chip auto uses ROM probes; mock --chip X must match the virtual SoC."""
    if chip not in constants.ESPTOOL_CHIPS:
        pytest.skip(f'{chip} not supported by installed esptool')
    with mock_bootloader(chip=chip, timeout=60.0, exit_on_disconnect=False, mode='foreground') as mock:
        result = esptool.run_esptool(
            '--chip', 'auto',
            '--port', f'socket://localhost:{mock.port()}',
            '--before', 'no-reset',
            '--after', 'no-reset',
            '--no-stub',
            'flash-id',
            timeout=60.0,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output[-800:]
        warns = esptool.forbidden_warnings(output, transport='socket')
        assert warns == [], '\n'.join(warns)
        mac = registers.mac_bytes_for_chip(chip)
        mac_line = ':'.join(f'{b:02x}' for b in mac)
        assert mac_line in output
