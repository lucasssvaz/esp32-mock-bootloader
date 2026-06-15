# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Helpers for integrating the mock bootloader in tests and CI.

Import submodules by name — avoid long symbol lists::

    import esp32_mock_bootloader.testing as mock

    with mock.server.running_server(chip="esp32") as (_proc, port):
        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
"""

from esp32_mock_bootloader.testing import constants, esptool, protocol, server

__all__ = ['constants', 'esptool', 'protocol', 'server']
