# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Public API contract tests."""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import struct
import tempfile
from pathlib import Path

import pytest

import esp32_mock_bootloader
from esp32_mock_bootloader import MockHandle, __version__, constants, instances, mock_bootloader, protocol
from esp32_mock_bootloader import chips, daemon, server
from esp32_mock_bootloader.advanced import process, protocol_client, transport
from esp32_mock_bootloader.advanced import protocol as protocol_api
from esp32_mock_bootloader.registry import Registry
from esp32_mock_bootloader.session import running_server
from esptool.targets import CHIP_DEFS
from tests.helpers import esptool


def test_package_exports():
    assert isinstance(__version__, str)
    assert callable(mock_bootloader)
    assert inspect.isclass(MockHandle)
    assert hasattr(instances, 'status')
    assert set(esp32_mock_bootloader.__all__) == {
        'MockHandle', 'instances', 'mock_bootloader', '__version__',
    }


def test_advanced_submodule_import():
    from esp32_mock_bootloader import advanced

    assert hasattr(advanced, 'protocol')
    assert hasattr(advanced, 'transport')
    assert hasattr(advanced, 'process')
    assert hasattr(advanced, 'protocol_client')


def test_public_module_all_symbols():
    for module in (constants, process, protocol_client, transport):
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


def test_mock_bootloader_lifecycle(registry_root: Registry):
    with mock_bootloader(chip='esp32', registry=registry_root, mode='daemon') as mock:
        port = mock.port()
        assert port > 0
        assert mock.url().startswith('socket://')
        assert mock.chip == 'esp32'
    status = daemon.daemon_status(port, registry_root.path)
    assert status['running'] is False


def test_running_server_smoke():
    from esp32_mock_bootloader.advanced import protocol as protocol_api

    with running_server(chip='esp32') as session:
        handle = mock_bootloader(chip='esp32', port=session.port, autostart=False)
        handle._session = session  # noqa: SLF001
        handle._port = session.port  # noqa: SLF001
        client = protocol_api.connect(handle)
        assert client.minimal_plain_flash()


def test_protocol_module_has_cmd_sync():
    assert hasattr(protocol, 'CMD_SYNC')
    assert protocol.CMD_SYNC == protocol.CMD['SYNC']


def test_server_advanced_import():
    assert hasattr(server, 'handle_client')
    assert hasattr(server, 'FlashImage')


def test_import_paths_documented_in_api_module():
    api = importlib.import_module('esp32_mock_bootloader.api')
    assert hasattr(api, 'MockHandle')
    assert hasattr(api, 'mock_bootloader')


def test_version_fallback_when_not_installed(monkeypatch):
    def boom(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError('nope')

    monkeypatch.setattr(importlib.metadata, 'version', boom)
    importlib.reload(esp32_mock_bootloader)
    assert esp32_mock_bootloader.__version__ == '0.1.0'
    importlib.reload(esp32_mock_bootloader)


def test_foreground_auto_stop_on_del():
    mock = mock_bootloader(chip='esp32')
    port = mock.port()
    del mock
    import gc
    gc.collect()
    rows = instances.status(port=port)
    assert not rows.get('running', True)


def test_mock_bootloader_foreground_smoke():
    with mock_bootloader(chip='esp32', mode='foreground') as mock:
        assert mock.port() > 0
        assert mock.url().startswith('socket://')


def test_registry_temp():
    with Registry.temp() as reg:
        assert reg.path.is_dir()


def test_mock_bootloader_url_after_stop(registry_root: Registry):
    mock = mock_bootloader(chip='esp32', registry=registry_root, mode='daemon', autostart=False)
    mock.start()
    port = mock.port()
    url = mock.url()
    mock.stop()
    assert url == daemon.socket_url(port)
    status = instances.status(port, base=registry_root.path)
    assert not status['running']


def test_mock_bootloader_detected_chip(registry_root: Registry):
    mock = mock_bootloader(chip='auto', registry=registry_root, mode='daemon')
    daemon.set_detected_chip(mock.port(), 'esp32c3', base=registry_root.path)
    assert mock.detected_chip == 'esp32c3'
    mock.stop()
    assert mock.detected_chip is None


def test_advanced_protocol_connect_not_started():
    from esp32_mock_bootloader.advanced import protocol as protocol_api

    handle = MockHandle(chip='esp32', autostart=False)
    with pytest.raises(RuntimeError, match='not started'):
        protocol_api.connect(handle)


def test_mock_handle_erase_flash(registry_root: Registry):
    with mock_bootloader(chip='esp32', registry=registry_root, mode='daemon') as mock:
        erased_port = mock.erase_flash()
        assert erased_port == mock.port()


def test_mock_handle_returncode_before_start():
    mock = MockHandle(chip='esp32', mode='foreground', autostart=False)
    assert mock.returncode is None


def test_mock_handle_start_idempotent():
    mock = mock_bootloader(chip='esp32', mode='foreground')
    first_port = mock.port()
    mock.start()
    assert mock.port() == first_port


def test_mock_handle_context_manager_starts_lazily():
    mock = MockHandle(chip='esp32', mode='foreground', autostart=False)
    with mock as running:
        assert running.port() > 0


def test_foreground_exits_on_client_disconnect():
    with mock_bootloader(
        chip='esp32', timeout=30.0, exit_on_disconnect=True, mode='foreground',
    ) as mock:
        client = protocol_api.connect(mock)
        client.sync()
        client.close()
        mock._session.proc.wait(timeout=5)  # noqa: SLF001
        assert mock.returncode == 0


def test_subprocess_server_survives_many_sessions():
    """Regression: PIPE capture deadlocks the child when stderr/stdout fill."""
    for _ in range(12):
        with mock_bootloader(
            chip='esp32', timeout=None, exit_on_disconnect=True, mode='foreground',
        ) as mock:
            client = protocol_api.connect(mock)
            client.sync()
            client.close()
            mock._session.proc.wait(timeout=3)  # noqa: SLF001
            assert mock.returncode == 0


def test_multiple_instances_without_group():
    server_a = mock_bootloader(chip='esp32')
    server_b = mock_bootloader(chip='esp32c3')
    try:
        assert server_a.port() != server_b.port()
        assert protocol_api.connect(server_a).minimal_plain_flash()
        assert protocol_api.connect(server_b).minimal_plain_flash()
    finally:
        server_b.stop()
        server_a.stop()


def test_client_auto_sync_without_explicit_sync():
    with mock_bootloader(chip='esp32', mode='foreground') as mock:
        value = protocol_api.connect(mock).read_reg(0x3FF00000)
        assert value is not None


def test_instances_status_multiple_daemons():
    server_a = mock_bootloader(chip='esp32')
    server_b = mock_bootloader(chip='esp32c3')
    try:
        rows = instances.status()
        assert isinstance(rows, list)
        assert len(rows) >= 2
        ports = {row['port'] for row in rows}
        assert server_a.port() in ports
        assert server_b.port() in ports
    finally:
        server_b.stop()
        server_a.stop()


def test_daemon_esptool_write(registry_root):
    with mock_bootloader(chip='auto', registry=registry_root, mode='daemon') as mock:
        assert mock.url() == f'socket://127.0.0.1:{mock.port()}'
        status = instances.status(mock.port())
        assert status['running']
        with tempfile.TemporaryDirectory() as tmp:
            bin_file = esptool.create_fake_binary(Path(tmp) / 'test.bin', 512)
            wrote_ok, detail = esptool.write_flash_no_stub(
                'esp32', str(bin_file), port=mock.port(),
            )
            assert wrote_ok, detail


def test_erase_flash_single_port_with_multiple_running(registry_root):
    offset = constants.FLASH_APP_OFFSET
    length = 0x100
    server_a = mock_bootloader(chip='esp32', registry=registry_root, mode='daemon')
    server_b = mock_bootloader(chip='esp32', registry=registry_root, mode='daemon')
    try:
        client_b = protocol_api.connect(server_b)
        client_b.sync()
        client_b.send_command(
            protocol.CMD_FLASH_BEGIN,
            struct.pack('<IIII', length, 1, length, offset),
        )
        client_b.send_command(
            protocol.CMD_FLASH_DATA,
            struct.pack('<IIII', length, 0, 0, 0) + (b'\x5A' * length),
        )
        erased = instances.erase_flash(server_a.port())
        assert erased == [server_a.port()]
        client_b.activate_stub()
        data, _digest = client_b.stub_read_flash(offset, length)
        assert data == b'\x5A' * length
    finally:
        server_b.stop()
        server_a.stop()


def test_mock_bootloader_port_before_start():
    mock = mock_bootloader(chip='esp32', autostart=False)
    with pytest.raises(RuntimeError, match='not started'):
        mock.port()
