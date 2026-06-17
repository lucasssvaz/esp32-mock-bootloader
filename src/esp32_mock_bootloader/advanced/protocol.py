# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Opt-in advanced protocol access bound to a MockHandle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from esp32_mock_bootloader.client import Client

if TYPE_CHECKING:
    from esp32_mock_bootloader.api import MockHandle


def connect(handle: MockHandle) -> Client:
    """Return a SLIP protocol client bound to the mock's session."""
    session = handle._session  # noqa: SLF001
    if session is None or not session._started:  # noqa: SLF001
        raise RuntimeError('mock bootloader is not started')
    return session.client


__all__ = ['connect']
