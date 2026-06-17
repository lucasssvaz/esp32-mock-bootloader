# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""Protocol client bound to a Session — no socket parameter in user code."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

from esp32_mock_bootloader import protocol_client as proto
from esp32_mock_bootloader.transport import SerialLink

if TYPE_CHECKING:
    from esp32_mock_bootloader.session import Session

Transport = socket.socket | SerialLink


class Client:
    """SLIP protocol client with lazy transport and auto-reconnect."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._transport: Transport | None = None
        self._synced = False

    def _mark_synced(self) -> None:
        self._synced = True

    def _ensure_synced(self) -> None:
        if not self._synced:
            proto.send_sync(self._ensure_transport())
            self._synced = True

    def _transport_alive(self) -> bool:
        if self._transport is None:
            return False
        try:
            return self._transport.fileno() != -1
        except (OSError, AttributeError):
            return False

    def _ensure_transport(self) -> Transport:
        if self._transport is not None and self._transport_alive():
            return self._transport
        self._close_transport()
        try:
            transport = self._session._open_transport()
        except (ConnectionRefusedError, OSError):
            if not self._session.running:
                raise
            transport = self._session._open_transport()
        self._transport = transport
        self._synced = False
        return transport

    def _close_transport(self) -> None:
        if self._transport is None:
            return
        try:
            self._transport.close()
        except OSError:
            pass
        self._session._untrack_transport(self._transport)
        self._transport = None
        self._synced = False

    def close(self) -> None:
        """Close the active transport (normally automatic on session exit)."""
        self._close_transport()

    def sync(self) -> None:
        proto.send_sync(self._ensure_transport())
        self._mark_synced()

    def send_and_receive(self, packet: bytes, recv_size: int = 4096) -> bytes:
        self._ensure_synced()
        return proto.send_and_receive(self._ensure_transport(), packet, recv_size)

    def send_command(self, cmd: int, data: bytes = b'', checksum: int | None = None) -> bytes:
        packet = proto.make_command(cmd, data, checksum)
        return self.send_and_receive(packet)

    def decode_frames(self, raw: bytes) -> list[bytes]:
        return proto.slip_decode_frames(raw)

    def parse_response(self, frame: bytes) -> tuple[int, int, int, int, bytes]:
        return proto.parse_response(frame)

    def read_reg(self, addr: int) -> int | None:
        self._ensure_synced()
        return proto.read_reg_value(self._ensure_transport(), addr)

    def minimal_plain_flash(self) -> bool:
        result = proto.minimal_plain_flash(self._ensure_transport())
        self._mark_synced()
        return result

    def activate_stub(self) -> None:
        proto.activate_stub(self._ensure_transport())
        self._mark_synced()

    def stub_read_flash(
        self,
        offset: int,
        length: int,
        *,
        packet_size: int | None = None,
        max_in_flight: int = 4,
    ) -> tuple[bytes, bytes]:
        self._ensure_synced()
        return proto.stub_read_flash(
            self._ensure_transport(),
            offset,
            length,
            packet_size=packet_size,
            max_in_flight=max_in_flight,
        )


__all__ = ['Client']
