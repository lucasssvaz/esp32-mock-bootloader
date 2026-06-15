# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Mock ESP32 ROM bootloader for protocol-level upload testing."""

from importlib.metadata import PackageNotFoundError, version

from esp32_mock_bootloader.api import MockBootloader

try:
    __version__ = version("esp32-mock-bootloader")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["MockBootloader", "__version__"]
