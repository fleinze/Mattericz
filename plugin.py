#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Domoticz Matter Plugin
Connects to a python-matter-server via WebSocket using Domoticz.Connection Protocol="None"
(raw TCP) with manual RFC-6455 framing and zlib decompression for permessage-deflate.

<plugin key="Matter" name="Matter (python-matter-server)" author="fleinze" version="0.1.0"
        externallink="https://github.com/fleinze/domoticz-python-matter">
    <description>
        Connects to a python-matter-server WebSocket and imports Matter devices into Domoticz.
        For commissioning of new nodes open the python-matter-server in a webbrowser or via the domoticz custom menu.
    </description>
    <params>
        <param field="Address" label="Matter Server Address" width="300px" required="true"
               default="localhost"/>
        <param field="Port" label="Matter Server Port" width="300px" required="true"
               default="5580"/>
        <param field="Mode6" label="Debug" width="75px">
            <options>
                <option label="None"    value="0" default="true"/>
                <option label="Verbose" value="1"/>
            </options>
        </param>
    </params>
</plugin>
"""

import DomoticzEx as Domoticz
import base64
import hashlib
import os
import secrets
import zlib
import sys, importlib
if 'matter' in sys.modules:  #Force reload of matter module
    del sys.modules['matter']
from matter import MatterBridge

# ---------------------------------------------------------------------------
# RFC-6455 WebSocket helpers
# ---------------------------------------------------------------------------

def _ws_upgrade_request(host: str, port: int, path: str) -> bytes:
    """Build the HTTP/1.1 Upgrade request. Advertises permessage-deflate so
    the server can use compression; we decompress in _ws_decode()."""
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Extensions: permessage-deflate; client_no_context_takeover",
#        "Sec-WebSocket-Extensions: client_no_context_takeover",
        "",
        "",
    ]
    return "\r\n".join(lines).encode()


def _ws_encode(payload: str) -> bytes:
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


class WsDecoder:
    """
    Stateful RFC-6455 frame decoder.
    Handles:
      - Multi-byte length fields (126 / 127)
      - Continuation frames (opcode 0x00) reassembled into the opening frame
      - permessage-deflate decompression (RSV1 bit set on the first fragment)
      - Server-to-client frames are never masked
    Call feed(data: bytes) with every TCP chunk; it yields complete messages.
    """

    def __init__(self, debug: bool = False):
        self._buf       = bytearray()
        self._frags     = bytearray()   # reassembly buffer for continuation frames
        self._compressed = False        # RSV1 was set on the opening fragment
        self._debug     = debug
        self._inflator = zlib.decompressobj(-zlib.MAX_WBITS)

    def feed(self, data: bytes):
        """Feed raw TCP bytes; yields decoded text messages (str)."""
        self._buf += data
        iteration = 0
        while True:
            iteration += 1
            msg = self._try_parse_frame()
            if iteration > 1000:
                Domoticz.Error("[WS] feed: infinite loop detected, breaking")
                break
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

        b0, b1 = buf[0], buf[1]
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

        # Consume frame from buffer
        self._buf = self._buf[offset + pay_len:]

        # --- Dispatch by opcode ---
        if opcode == 0x08:          # Close
            return None             # signal disconnect
        if opcode == 0x09:  # Ping → Pong senden
            return ("ping", bytes(payload))
        if opcode == 0x0A:  # Pong – ignorieren
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
#            Domoticz.Log(f"[WS] decompress: raw len={len(raw)}, first bytes={raw[:10].hex()}, last bytes={raw[-4:].hex()}")
            try:
                raw = self._inflator.decompress(raw+b"\x00\x00\xff\xff")
                raw += self._inflator.flush(zlib.Z_SYNC_FLUSH)
            except zlib.error as exc:
                Domoticz.Error(f"[WS] zlib decompress failed: {exc}")
                return _NEED_MORE

        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            Domoticz.Error(f"[WS] UTF-8 decode failed: {exc}")
            return _NEED_MORE


class _NeedMore:
    pass

_NEED_MORE = _NeedMore()


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class BasePlugin:
    """Main plugin class – lifecycle managed by Domoticz."""

    def __init__(self):
        self.matter      = None
        self.conn        = None
        self.debug       = False
        self._hb_count   = 0
        self._reconnect_interval = 6
        self._host       = "localhost"
        self._port       = 5580
        self._path       = "/ws"
        self._connected  = False
        self._handshake_done = False
        self._tcp_buf    = bytearray()  # buffer for raw TCP data before handshake
        self._decoder    = None         # WsDecoder, created after handshake

    # ------------------------------------------------------------------
    # Domoticz lifecycle callbacks
    # ------------------------------------------------------------------

    def onStart(self):
        Domoticz.Log("Matter plugin starting ...")

        self.debug = Parameters["Mode6"] == "1"
        if self.debug:
            Domoticz.Debugging(1)
            Domoticz.Log("Debug mode enabled")

        self._host = Parameters["Address"].strip() or "localhost"
        self._port = Parameters["Port"].strip() or 5580
        self._path = "/ws"

        self.matter = MatterBridge(devices=Devices, debug=self.debug)
        self._connect()

        html_content = f'<IFRAME SRC="http://{self._host}:{self._port}/" height="600" width="100%"></IFRAME>'
        with open('./www/templates/matter.html', 'w', encoding='utf-8') as f:
            f.write(html_content)

    def onStop(self):
        Domoticz.Log("Matter plugin stopping ...")
        self._connected = False
        if self.conn:
            try:
                self.conn.Disconnect()
            except Exception:
                pass
            self.conn = None
        if os.path.exists('./www/templates/matter.html'):
            os.remove('./www/templates/matter.html')

    def onHeartbeat(self):
        self._hb_count += 1
        if not self._connected:
            if self._hb_count % self._reconnect_interval == 0:
                Domoticz.Log("Not connected - reconnecting ...")
                self._connect()

    # ------------------------------------------------------------------
    # Connection callbacks
    # ------------------------------------------------------------------

    def onConnect(self, Connection, Status, Description):
        if Status != 0:
            Domoticz.Error(f"Connection failed ({Status}): {Description}")
            self._connected = False
            return

        Domoticz.Log(f"TCP connected to {Connection.Address}:{Connection.Port} - sending WS upgrade ...")
        self._handshake_done = False
        self._tcp_buf = bytearray()
        self._decoder = None
        Connection.Send(_ws_upgrade_request(self._host, int(self._port), self._path))

    def onMessage(self, Connection, Data):
        # Domoticz delivers raw TCP bytes with Protocol="None"
        if isinstance(Data, str):
            Data = Data.encode("latin-1")
        if not self._handshake_done:
            self._tcp_buf += Data
            self._try_complete_handshake(Connection)
            return

        # Hand all buffered bytes to the decoder
        for msg in self._decoder.feed(Data):
            if msg is None:
                Domoticz.Log("[WS] Server sent Close frame.")
                self._connected = False
                return
            if isinstance(msg, tuple) and msg[0] == "ping":
               # Pong mit gleichem Payload zurückschicken (unmasked opcode 0x0A)
               pong_payload = msg[1]
               length = len(pong_payload)
               mask = secrets.token_bytes(4)
               header = bytearray([0x8A, 0x80 | length]) + bytearray(mask)
               masked = bytes(b ^ mask[i % 4] for i, b in enumerate(pong_payload))
               self.conn.Send(bytes(header) + masked)
               continue
#            if self.debug:
#                Domoticz.Debug(f"[WS] <- {msg[:300]}")
            self.matter.on_message(msg)

    def onDisconnect(self, Connection):
        Domoticz.Log("Disconnected from Matter server.")
        self._connected = False
        self._handshake_done = False

    def onCommand(self, DeviceID, Unit, Command, Level, Color):
        self.matter.on_command(DeviceID, Unit, Command, Level, Color)

    def onNotification(self, Name, Subject, Text, Status, Priority, Sound, ImageFile):
        pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _try_complete_handshake(self, Connection):
        """Wait until the full HTTP response header is in _tcp_buf."""
        try:
            header_end = self._tcp_buf.index(b"\r\n\r\n")
        except ValueError:
            return  # not yet complete

        header_bytes = self._tcp_buf[:header_end]
        self._tcp_buf = self._tcp_buf[header_end + 4:]  # remainder = WS data

        header_text = header_bytes.decode("utf-8", errors="replace")
        if self.debug:
            Domoticz.Debug(f"[WS] Handshake response:\n{header_text}")

        first_line = header_text.split("\r\n")[0]
        if "101" not in first_line:
            Domoticz.Error(f"[WS] Unexpected handshake response: {first_line}")
            return

        Domoticz.Log("WebSocket handshake complete (raw TCP).")
        self._handshake_done = True
        self._connected = True
        self._decoder = WsDecoder(debug=self.debug)

        # Process any WS bytes that arrived together with the HTTP headers
        if self._tcp_buf:
            for msg in self._decoder.feed(self._tcp_buf):
                if msg is None:
                    self._connected = False
                    return
                self.matter.on_message(msg)
            self._tcp_buf = bytearray()

        self.matter.on_connected(self._send_ws)

    def _connect(self):
        self._connected = False
        self._handshake_done = False
        Domoticz.Log(f"Connecting to {self._host}:{self._port} ...")
        self.conn = Domoticz.Connection(
            Name="MatterWS",
            Transport="TCP/IP",
            Protocol="None",
            Address=self._host,
            Port=str(self._port),
        )
        self.conn.Connect()

    def _send_ws(self, payload: str):
        """Send a JSON string as a masked WebSocket text frame."""
        if not self._connected or self.conn is None:
            Domoticz.Error("[Matter] _send_ws: not connected")
            return
        self.conn.Send(_ws_encode(payload))


# ---------------------------------------------------------------------------
# Module-level Domoticz entry points
# ---------------------------------------------------------------------------
global _plugin
_plugin = BasePlugin()


def onStart():
    global _plugin
    _plugin.onStart()

def onStop():
    global _plugin
    _plugin.onStop()

def onConnect(Connection, Status, Description):
    global _plugin
    _plugin.onConnect(Connection, Status, Description)

def onMessage(Connection, Data):
    global _plugin
    _plugin.onMessage(Connection, Data)

def onCommand(DeviceID, Unit, Command, Level, Color):
    global _plugin
    _plugin.onCommand(DeviceID, Unit, Command, Level, Color)

def onNotification(Name, Subject, Text, Status, Priority, Sound, ImageFile):
    global _plugin
    _plugin.onNotification(Name, Subject, Text, Status, Priority, Sound, ImageFile)

def onDisconnect(Connection):
    global _plugin
    _plugin.onDisconnect(Connection)

def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()
