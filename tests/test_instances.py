# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""instances module — CLI-parity operations and MockHandle delegation."""

from __future__ import annotations

import json

import pytest

from esp32_mock_bootloader import instances, mock_bootloader
from esp32_mock_bootloader import daemon


def test_mock_handle_delegates_to_instances():
    with mock_bootloader(chip='esp32', mode='foreground') as mock:
        assert mock.url() == instances.url(mock.port())
        assert mock.port() == instances.port(mock.port())
        data = mock.status()
        assert data['running']
        assert data['port'] == mock.port()


def test_instances_status_text_format():
    with mock_bootloader(chip='esp32') as mock:
        text = instances.status(format='text')
        assert str(mock.port()) in text
        assert 'running' not in text.lower() or 'URL' in text


def test_instances_status_json_format():
    with mock_bootloader(chip='esp32c3') as mock:
        payload = json.loads(instances.status(format='json'))
        if isinstance(payload, dict):
            assert payload['port'] == mock.port()
        else:
            ports = {row['port'] for row in payload['instances']}
            assert mock.port() in ports


def test_instances_stop_all():
    a = mock_bootloader(chip='esp32')
    b = mock_bootloader(chip='esp32c3')
    port_a = a.port()
    port_b = b.port()
    stopped = instances.stop(port='all')
    assert port_a in stopped
    assert port_b in stopped


def test_instances_erase_flash_requires_running():
    with pytest.raises(RuntimeError, match='no mock bootloader'):
        instances.erase_flash(port='all')
