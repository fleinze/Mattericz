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
import os
import sys

# Force reload of helper modules so that "Update" in Domoticz hardware page
# picks up changes to matter.py and websockets.py without a full restart.
for _mod in ('matter', 'websockets'):
    if _mod in sys.modules:
        del sys.modules[_mod]

from matter import MatterBridge
from ws_framing import WsDecoder, ws_upgrade_request, ws_encode, ws_pong


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class BasePlugin:
    """Main plugin class – lifecycle managed by Domoticz."""

    def __init__(self):
        self.matter          = None
        self.conn            = None
        self.debug           = False
        self._hb_count       = 0
        self._reconnect_interval = 6
        self._host           = "localhost"
        self._port           = 5580
        self._path           = "/ws"
        self._connected      = False
        self._handshake_done = False
        self._tcp_buf        = bytearray()
        self._decoder        = None

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

        html_content = (
            f'<IFRAME SRC="http://{self._host}:{self._port}/" '
            f'height="600" width="100%"></IFRAME>'
        )
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

        Domoticz.Log(
            f"TCP connected to {Connection.Address}:{Connection.Port}"
            " - sending WS upgrade ..."
        )
        self._handshake_done = False
        self._tcp_buf = bytearray()
        self._decoder = None
        Connection.Send(ws_upgrade_request(self._host, int(self._port), self._path))

    def onMessage(self, Connection, Data):
        if isinstance(Data, str):
            Data = Data.encode("latin-1")

        if not self._handshake_done:
            self._tcp_buf += Data
            self._try_complete_handshake(Connection)
            return

        for msg in self._decoder.feed(Data):
            if msg is None:
                Domoticz.Log("[WS] Server sent Close frame.")
                self._connected = False
                return
            if isinstance(msg, tuple) and msg[0] == "ping":
                self.conn.Send(ws_pong(msg[1]))
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
        self._tcp_buf = self._tcp_buf[header_end + 4:]

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
        self.conn.Send(ws_encode(payload))


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
