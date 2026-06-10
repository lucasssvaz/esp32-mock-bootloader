# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Transport connection helper tests."""

from __future__ import annotations

import pytest

from conftest import connect_serial_endpoint


def test_connect_serial_endpoint_invalid_socket_url():
    with pytest.raises(ValueError, match='invalid socket endpoint'):
        connect_serial_endpoint('socket://127.0.0.1')


def test_connect_serial_endpoint_unsupported_host():
    with pytest.raises(ValueError, match='unsupported socket host'):
        connect_serial_endpoint('socket://192.168.1.1:1234')
