# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""com0com helper unit tests (fake setupc; no Windows driver required)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from esp32_mock_bootloader.com0com import (
    Com0ComError,
    ComPair,
    com0com_pair,
    find_pair_id,
    find_paired_port,
    find_setupc,
    install_pair,
    keep_com_pair,
    parse_pairs,
    remove_pair,
    run_setupc,
    wait_for_ports,
)
from esp32_mock_bootloader import server

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
FAKE_SETUPC = FIXTURES / 'fake_setupc.py'


@pytest.fixture
def fake_setupc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state = tmp_path / 'fake_setupc_state.json'
    if os.name == 'nt':
        wrapper = tmp_path / 'setupc.cmd'
        wrapper.write_text(f'@"{sys.executable}" "{FAKE_SETUPC}" %*\n', encoding='utf-8')
    else:
        wrapper = tmp_path / 'setupc'
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_SETUPC}" "$@"\n',
            encoding='utf-8',
        )
        wrapper.chmod(0o755)
    monkeypatch.setenv('ESP32_MOCK_SETUPC', str(wrapper))
    monkeypatch.setenv('ESP32_MOCK_FAKE_SETUPC_STATE', str(state))
    monkeypatch.setattr('esp32_mock_bootloader.com0com.wait_for_ports', lambda *_a, **_k: None)
    return wrapper


def test_parse_pairs_and_find_pair_id():
    listing = """
CNCA0 PortName=COM18,EmuBR=yes
CNCB0 PortName=COM19,EmuBR=yes
CNCA1 PortName=COM20,EmuBR=yes
CNCB1 PortName=COM21,EmuBR=yes
"""
    pairs = parse_pairs(listing)
    assert (0, 'COM18', 'COM19') in pairs
    assert find_pair_id(listing, 'COM18', 'COM19') == 0
    assert find_pair_id(listing, 'COM20', 'COM21') == 1
    assert find_pair_id(listing, 'COM99', 'COM98') is None


def test_find_paired_port(fake_setupc: Path):
    install_pair(fake_setupc, 'COM40', 'COM41')
    listing = run_setupc(fake_setupc, 'list')
    assert find_paired_port('COM41') == 'COM40'
    assert find_paired_port('COM40') == 'COM41'
    assert find_paired_port('COM99') is None
    assert find_pair_id(listing, 'COM40', 'COM41') == 0


def test_resolve_serial_pair_auto_peer(fake_setupc: Path):
    install_pair(fake_setupc, 'COM40', 'COM41')
    assert server.resolve_serial_pair(client_port='COM41') == ('COM40', 'COM41')
    assert server.resolve_serial_pair(serial_bind='COM40') == ('COM40', 'COM41')


def test_parse_pairs_real_port_name():
    listing = """
CNCA0 RealPortName=COM30,EmuBR=yes
CNCB0 PortName=COM31,EmuBR=yes
"""
    pairs = parse_pairs(listing)
    assert pairs == [(0, 'COM30', 'COM31')]


def test_parse_pairs_com_placeholder_skipped():
    listing = """
CNCA0 PortName=COM#,EmuBR=yes
CNCB0 PortName=COM32,EmuBR=yes
"""
    pairs = parse_pairs(listing)
    assert pairs == []


def test_find_setupc_override(fake_setupc: Path):
    assert find_setupc() == fake_setupc


def test_find_setupc_override_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    missing = tmp_path / 'nope.exe'
    monkeypatch.setenv('ESP32_MOCK_SETUPC', str(missing))
    with pytest.raises(Com0ComError, match='does not exist'):
        find_setupc()


def test_find_setupc_which(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv('ESP32_MOCK_SETUPC', raising=False)
    fake = tmp_path / 'setupc.cmd'
    fake.write_text('@echo off\n', encoding='utf-8')
    monkeypatch.setattr('esp32_mock_bootloader.com0com.shutil.which', lambda _: str(fake))
    assert find_setupc() == fake


def test_find_setupc_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv('ESP32_MOCK_SETUPC', raising=False)
    monkeypatch.setattr('esp32_mock_bootloader.com0com.shutil.which', lambda _: None)
    candidate = tmp_path / 'setupc.exe'
    candidate.touch()
    monkeypatch.setattr(
        'esp32_mock_bootloader.com0com.SETUPC_CANDIDATES',
        (candidate,),
    )
    assert find_setupc() == candidate


def test_find_setupc_not_found(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv('ESP32_MOCK_SETUPC', raising=False)
    monkeypatch.setattr('esp32_mock_bootloader.com0com.shutil.which', lambda _: None)
    monkeypatch.setattr(Path, 'is_file', lambda self: False, raising=False)
    with pytest.raises(Com0ComError, match='setupc.exe not found'):
        find_setupc()


def test_run_setupc_success(fake_setupc: Path):
    out = run_setupc(fake_setupc, 'list')
    assert 'CNCA' not in out or out.strip() == ''


def test_run_setupc_nonzero_exit(fake_setupc: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*_a, **_k):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='access is denied')

    monkeypatch.setattr('esp32_mock_bootloader.com0com.subprocess.run', boom)
    with pytest.raises(Com0ComError, match='access is denied|setupc failed'):
        run_setupc(fake_setupc, 'list')


def test_run_setupc_os_error(fake_setupc: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*_a, **_k):
        raise OSError('nope')

    monkeypatch.setattr('esp32_mock_bootloader.com0com.subprocess.run', boom)
    with pytest.raises(Com0ComError, match='Failed to run'):
        run_setupc(fake_setupc, 'list')


def test_run_setupc_error_line(fake_setupc: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout='error: install failed\n', stderr='',
        )

    monkeypatch.setattr('esp32_mock_bootloader.com0com.subprocess.run', fake_run)
    with pytest.raises(Com0ComError, match='setupc error'):
        run_setupc(fake_setupc, 'install x')


def test_install_pair_and_remove(fake_setupc: Path):
    pair = install_pair(fake_setupc, 'COM40', 'COM41')
    assert pair == ComPair(pair_id=0, server='COM40', peer='COM41')
    listing = run_setupc(fake_setupc, 'list')
    assert find_pair_id(listing, 'COM40', 'COM41') == 0
    remove_pair(fake_setupc, pair)
    listing = run_setupc(fake_setupc, 'list')
    assert find_pair_id(listing, 'COM40', 'COM41') is None


def test_install_pair_replaces_existing(fake_setupc: Path):
    install_pair(fake_setupc, 'COM50', 'COM51')
    pair = install_pair(fake_setupc, 'COM50', 'COM51')
    assert pair.pair_id == 0


def test_install_pair_not_found_after_install(fake_setupc: Path, monkeypatch: pytest.MonkeyPatch):
    original = subprocess.run

    def selective_run(cmd, **kwargs):
        script = kwargs.get('input', '')
        if 'install' in script and 'list' in script:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='\n', stderr='')
        return original(cmd, **kwargs)

    monkeypatch.setattr('esp32_mock_bootloader.com0com.subprocess.run', selective_run)
    with pytest.raises(Com0ComError, match='could not find'):
        install_pair(fake_setupc, 'COM60', 'COM61')


def test_wait_for_ports_success(monkeypatch: pytest.MonkeyPatch):
    class Port:
        def __init__(self, device: str) -> None:
            self.device = device

    calls = {'n': 0}

    def fake_comports():
        calls['n'] += 1
        if calls['n'] >= 2:
            return [Port('COM70'), Port('COM71')]
        return []

    monkeypatch.setattr('serial.tools.list_ports.comports', fake_comports)
    wait_for_ports('COM70', 'COM71', timeout=2.0)


def test_wait_for_ports_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr('serial.tools.list_ports.comports', lambda: [])
    with pytest.raises(Com0ComError, match='Timed out'):
        wait_for_ports('COM99', timeout=0.5)


def test_com0com_pair_context(fake_setupc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv('ESP32_MOCK_KEEP_COM_PAIR', raising=False)
    ports_seen: list[str] = []

    def fake_wait(*names, timeout=15.0):
        ports_seen.extend(names)

    monkeypatch.setattr('esp32_mock_bootloader.com0com.wait_for_ports', fake_wait)
    with com0com_pair('COM80', 'COM81') as pair:
        assert pair.server == 'COM80'
        assert pair.peer == 'COM81'
    assert ports_seen == ['COM80', 'COM81']
    listing = run_setupc(fake_setupc, 'list')
    assert find_pair_id(listing, 'COM80', 'COM81') is None


def test_com0com_pair_keep_env(fake_setupc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('ESP32_MOCK_KEEP_COM_PAIR', '1')
    with com0com_pair('COM90', 'COM91'):
        pass
    listing = run_setupc(fake_setupc, 'list')
    assert find_pair_id(listing, 'COM90', 'COM91') == 0


def test_com0com_pair_remove_errors_ignored(fake_setupc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv('ESP32_MOCK_KEEP_COM_PAIR', raising=False)

    def boom(*_a, **_k):
        raise Com0ComError('remove failed')

    monkeypatch.setattr('esp32_mock_bootloader.com0com.remove_pair', boom)
    with com0com_pair('COM95', 'COM96'):
        pass


def test_keep_com_pair_env(monkeypatch: pytest.MonkeyPatch):
    assert keep_com_pair() is False
    monkeypatch.setenv('ESP32_MOCK_KEEP_COM_PAIR', 'yes')
    assert keep_com_pair() is True
