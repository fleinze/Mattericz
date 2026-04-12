#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
websockets.py  –  RFC-6455 WebSocket helpers for the Domoticz Matter plugin.

Provides:
  _ws_upgrade_request(host, port, path) -> bytes
  _ws_encode(payload: str) -> bytes
  WsDecoder  – stateful frame decoder, feed() yields complete messages
"""

import base64
import secrets
import zlib

import DomoticzEx as Domoticz


# ---------------------------------------------------------------------------
# Sentinel for incomplete frames
# ---------------------------------------------------------------------------

class _NeedMore:
    pass

_NEED_MORE = _NeedMore()


# ---------------------------------------------------------------------------
# Outgoing
# ---------------------------------------------------------------------------

def ws_upgrade_request(host: str, port: int, path: str) -> bytes:
    """Build the HTTP/1.1 Upgrade request. Advertises permessage-deflate so
    the server can use compression; we decompress in WsDecoder."""
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Extensions: permessage-deflate; client_no_context_takeover",
        "",
        "",
    ]
    return "\r\n".join(lines).encode()


def ws_encode(payload: str) -> bytes:
    """Encode a UTF-8 text payload as a single masked WebSocket frame."""
    data = payload.encode("utf-8")
    length = len(data)
    mask = secrets.token_bytes(4)

    header = bytearray()
    header.append(0x81)          # FIN=1, opcode=1 (text)
    if length <= 125:
        header.append(0x80 | length)
    elif length <= 65535:
        header.append(0x80 | 126)
        header += length.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += length.to_bytes(8, "big")
    header += mask

    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return bytes(header) + masked


def ws_pong(payload: bytes) -> bytes:
    """Encode a masked Pong frame with the given payload."""
    length = len(payload)
    mask = secrets.token_bytes(4)
    header = bytearray([0x8A, 0x80 | length]) + bytearray(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


# ---------------------------------------------------------------------------
# Incoming – stateful frame decoder
# ---------------------------------------------------------------------------

class WsDecoder:
    """
    Stateful RFC-6455 frame decoder.
    Handles:
      - Multi-byte length fields (126 / 127)
      - Continuation frames (opcode 0x00) reassembled into the opening frame
      - permessage-deflate decompression (RSV1 bit on the opening fragment)
      - Server-to-client frames are never masked

    Call feed(data: bytes) – it yields:
      str             complete text message
      ("ping", bytes) ping frame – caller should send a pong
      None            close frame – caller should disconnect
    """

    def __init__(self, debug: bool = False):
        self._buf        = bytearray()
        self._frags      = bytearray()
        self._compressed = False
        self._debug      = debug
        self._inflator   = zlib.decompressobj(-zlib.MAX_WBITS)

    def feed(self, data: bytes):
        """Feed raw TCP bytes; yields complete messages."""
        self._buf += data
        while True:
            msg = self._try_parse_frame()
            if msg is None:
                yield None
                break
            if isinstance(msg, _NeedMore):
                break
            yield msg

    # ------------------------------------------------------------------

    def _try_parse_frame(self):
        buf = self._buf
        if len(buf) < 2:
            return _NEED_MORE

        b0, b1  = buf[0], buf[1]
        fin     = bool(b0 & 0x80)
        rsv1    = bool(b0 & 0x40)
        opcode  = b0 & 0x0F
        masked  = bool(b1 & 0x80)
        pay_len = b1 & 0x7F

        offset = 2
        if pay_len == 126:
            if len(buf) < 4:
                return _NEED_MORE
            pay_len = int.from_bytes(buf[2:4], "big")
            offset = 4
        elif pay_len == 127:
            if len(buf) < 10:
                return _NEED_MORE
            pay_len = int.from_bytes(buf[2:10], "big")
            offset = 10

        if masked:
            offset += 4

        if len(buf) < offset + pay_len:
            return _NEED_MORE

        # Extract payload
        if masked:
            mask_bytes = buf[offset - 4:offset]
            payload = bytearray(
                buf[offset + i] ^ mask_bytes[i % 4] for i in range(pay_len)
            )
        else:
            payload = bytearray(buf[offset:offset + pay_len])

        # Consume frame from buffer – always before any return
        self._buf = self._buf[offset + pay_len:]

        # --- Dispatch by opcode ---
        if opcode == 0x08:          # Close
            return None
        if opcode == 0x09:          # Ping
            return ("ping", bytes(payload))
        if opcode == 0x0A:          # Pong – ignore
            return _NEED_MORE

        if opcode == 0x01:          # Text frame (opening / unfragmented)
            self._compressed = rsv1
            self._frags = payload
        elif opcode == 0x00:        # Continuation frame
            self._frags += payload
        else:
            if self._debug:
                Domoticz.Debug(f"[WS] Unknown opcode {opcode:#x}, ignoring")
            return _NEED_MORE

        if not fin:
            return _NEED_MORE       # more fragments coming

        # All fragments collected – decompress if needed
        raw = bytes(self._frags)
        self._frags = bytearray()

        if self._compressed:
            try:
                raw = self._inflator.decompress(raw + b"\x00\x00\xff\xff")
                raw += self._inflator.flush(zlib.Z_SYNC_FLUSH)
            except zlib.error as exc:
                Domoticz.Error(f"[WS] zlib decompress failed: {exc}")
                return _NEED_MORE

        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            Domoticz.Error(f"[WS] UTF-8 decode failed: {exc}")
            return _NEED_MORE
