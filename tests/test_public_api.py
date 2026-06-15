# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Public API contract tests."""

from __future__ import annotations

import importlib
import inspect

import pytest

from esp32_mock_bootloader import MockBootloader, __version__
from esp32_mock_bootloader import chips, daemon, protocol, server
from esp32_mock_bootloader.testing import constants, esptool, protocol as test_protocol, server as test_server
from esptool.targets import CHIP_DEFS


def test_package_exports():
    assert isinstance(__version__, str)
    assert inspect.isclass(MockBootloader)


def test_documented_submodules_import():
    import esp32_mock_bootloader.testing as testing

    for name in testing.__all__:
        assert hasattr(testing, name)


def test_testing_submodule_all_symbols():
    for module in (constants, esptool, test_protocol, test_server):
        for name in module.__all__:
            assert hasattr(module, name), f'{module.__name__}.{name}'
            assert getattr(module, name) is not None


def test_chips_profiles_match_supported():
    assert set(chips.PROFILES.keys()) == set(chips.SUPPORTED)
    assert set(constants.ESPTOOL_CHIPS) == set(chips.SUPPORTED)


def test_chip_profiles_derived_from_esptool():
    """Profiles are built from esptool ROM classes, not hand-maintained tables."""
    assert chips.PROFILES is chips.get_chip_profiles()
    for chip in chips.SUPPORTED:
        expected = chips.profile_from_rom(chip, CHIP_DEFS[chip])
        assert chips.PROFILES[chip] == expected, chip


def test_mock_bootloader_lifecycle(tmp_path):
    with MockBootloader(chip='esp32', state_dir=tmp_path / 'state') as mock:
        assert mock.port > 0
        assert mock.url.startswith('socket://')
        assert mock.chip == 'esp32'
        sock = mock.connect()
        try:
            test_protocol.send_sync(sock)
        finally:
            sock.close()
    status = daemon.daemon_status(mock.port, tmp_path / 'state')
    assert status['running'] is False


def test_testing_running_server_smoke():
    with test_server.running_server(chip='esp32') as (_proc, port):
        sock = test_server.connect(port)
        try:
            test_protocol.send_sync(sock)
            assert test_protocol.minimal_plain_flash(sock)
        finally:
            sock.close()


def test_protocol_module_has_cmd_sync():
    assert hasattr(protocol, 'CMD_SYNC')
    assert protocol.CMD_SYNC == protocol.CMD['SYNC']


def test_server_advanced_import():
    assert hasattr(server, 'handle_client')
    assert hasattr(server, 'FlashImage')


def test_import_paths_documented_in_api_module():
    api = importlib.import_module('esp32_mock_bootloader.api')
    assert hasattr(api, 'MockBootloader')
