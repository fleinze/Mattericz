#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ws_framing.py  –  WebSocket helpers for the Domoticz Matter plugin.

  # called from plugin.py Domoticz callbacks:
  conn.on_connect(Status, Description) -> bool   True = handshake sent / ok
  conn.on_message(Data) -> iterable of messages
      each message is one of:
        str             – complete JSON text
        ("ping", bytes) – ping frame; caller must send pong via conn.pong()
        None            – close / disconnect

  conn.pong(payload: bytes)
"""

import base64
import secrets
import zlib

import DomoticzEx as Domoticz

USE_COMPRESSION = True # Request compressed payload from server, set True or False

class WsConnection:

    PROTOCOL = "WS"

    def __init__(self, host: str, port: int, path: str, debug: bool = False):
        self._host    = host
        self._port    = port
        self._path    = path
        self._debug   = debug
        self._conn    = None
        self._inflator = zlib.decompressobj(-zlib.MAX_WBITS)

    def connect(self):
        self._recv_buf = ""
        self._conn = Domoticz.Connection(
            Name="MatterWS",
            Transport="TCP/IP",
            Protocol=self.PROTOCOL,
            Address=self._host,
            Port=str(self._port),
        )
        self._conn.Connect()

    def disconnect(self):
        if self._conn:
            try:
                self._conn.Disconnect()
            except Exception:
                pass

    def send(self, payload: str):
        if not self._conn:
            Domoticz.Error("[WS] send: not connected")
            return
        if self._debug:
            Domoticz.Debug(f"[WS] -> {payload[:200]}")
        self._conn.Send({"Payload": payload, "Mask": secrets.randbits(32)})

    def pong(self, payload: bytes):
        if not self._conn:
            return
        self._conn.Send({
            "Operation": "Pong",
            "Payload": payload.decode("latin-1") if payload else "Pong",
            "Mask": secrets.randbits(32),
        })

    def on_connect(self, Status, Description) -> bool:
        """
        Called from plugin.py onConnect.
        Sends the HTTP Upgrade request.
        Returns True if connection succeeded and upgrade was sent.
        """
        if Status != 0:
            Domoticz.Error(f"[WS] Connection failed ({Status}): {Description}")
            return False
        Domoticz.Log(
            f"[WS] TCP connected to {self._host}:{self._port}"
            " – sending WS upgrade ..."
        )
        self._conn.Send({
            "URL": self._path,
            "Headers": {
                "Host": f"{self._host}:{self._port}",
                "Origin": f"http://{self._host}:{self._port}",
                "Sec-WebSocket-Key": base64.b64encode(secrets.token_bytes(16)).decode(),
                "Sec-WebSocket-Extensions": "permessage-deflate; client_no_context_takeover" if USE_COMPRESSION else "",
            },
        })
        return True

    def on_message(self, Data: dict):
        """
        Called from plugin.py onMessage.
        Yields complete messages (str), ("ping", bytes), or None (close).
        """
        if "Status" in Data:
            if Data["Status"] == "101":
                Domoticz.Log("[WS] Handshake complete (Protocol=WS).")
                return
            else:
                Domoticz.Error(f"[WS] Unexpected HTTP status: {Data.get('Status')}")
            return

        if "Operation" in Data:
            op = Data["Operation"]
            if op == "Ping":
                yield ("ping", b"")
            elif op == "Close":
                Domoticz.Log("[WS] Close frame received.")
                yield None
            # Pong – ignore
            return

        if "Payload" not in Data:
            return

        raw = Data["Payload"]

        if USE_COMPRESSION and isinstance(raw, (bytes, bytearray)): # if payload is compressed, deflate it.
#            Domoticz.Debug("decompressing")
            try:
                raw = self._inflator.decompress(raw + b"\x00\x00\xff\xff")
                raw += self._inflator.flush(zlib.Z_SYNC_FLUSH)
                raw = raw.decode("utf-8")
            except Exception as exc:
                Domoticz.Error(f"[WS] decompress/decode failed: {exc}")
                return
#        else:
#            Domoticz.Debug("uncompressed")
        yield raw
