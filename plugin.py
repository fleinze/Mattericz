#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Domoticz Matter Plugin
Connects to a python-matter-server via WebSocket using Domoticz.Connection Protocol="WS".

<plugin key="Matter" name="Matter (python-matter-server)" author="fleinze" version="0.1.0"
        externallink="https://github.com/fleinze/domoticz-python-matter">
    <description>
        <h2>Matter Plugin</h2>
        Connects to a python-matter-server WebSocket and imports Matter devices into Domoticz.
        Currently supports: Temperature sensors (Matter cluster 0x0402).
    </description>
    <params>
        <param field="Address" label="Matter Server URL" width="300px" required="true"
               default="ws://localhost:5580/ws"/>
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
import matter as MatterBridge
import base64
import secrets
import urllib.parse


class BasePlugin:
    """Main plugin class – lifecycle managed by Domoticz."""

    def __init__(self):
        self.matter      = None
        self.conn        = None
        self.debug       = False
        self._hb_count   = 0
        self._reconnect_interval = 6   # heartbeats × 10 s = 60 s
        self._host       = "localhost"
        self._port       = 5580
        self._path       = "/ws"
        self._connected  = False

    # ------------------------------------------------------------------
    # Domoticz lifecycle callbacks
    # ------------------------------------------------------------------

    def onStart(self):
        Domoticz.Log("Matter plugin starting ...")

        self.debug = Parameters["Mode6"] == "1"
        if self.debug:
            Domoticz.Debugging(1)
            Domoticz.Log("Debug mode enabled")

        url = Parameters["Address"].strip()
        if not url:
            Domoticz.Error("Matter Server URL not configured - aborting start.")
            return

        parsed = urllib.parse.urlparse(url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 5580
        self._path = parsed.path or "/ws"

        self.matter = MatterBridge.MatterBridge(devices=Devices, debug=self.debug)

        self._connect()

    def onStop(self):
        Domoticz.Log("Matter plugin stopping ...")
        self._connected = False
        if self.conn:
            try:
                self.conn.Disconnect()
            except Exception:
                pass
            self.conn = None

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

        # With Protocol="WS" Domoticz handles WS framing; we send the HTTP
        # Upgrade as a dict.
        # IMPORTANT: set Sec-WebSocket-Extensions to an empty string to
        # explicitly opt out of permessage-deflate.  If the server negotiates
        # compression, Domoticz does not decompress the payload - frames would
        # arrive as raw bytes and cannot be JSON-parsed.
        Connection.Send({
            "URL": self._path,
            "Headers": {
                "Host": f"{self._host}:{self._port}",
                "Origin": f"http://{self._host}:{self._port}",
                "Sec-WebSocket-Key": base64.b64encode(secrets.token_bytes(16)).decode(),
                "Sec-WebSocket-Extensions": "",
            },
        })

    def onMessage(self, Connection, Data):
        if self.debug:
            Domoticz.Debug(f"onMessage: {str(Data)[:300]}")

        # --- WebSocket upgrade response ---------------------------------
        if "Status" in Data:
            if Data["Status"] == "101":
                Domoticz.Log("WebSocket handshake complete.")
                self._connected = True
                self.matter.on_connected(self._send_ws)
            else:
                Domoticz.Error(f"Unexpected HTTP status: {Data.get('Status')} {Data.get('Description','')}")
            return

        # --- WebSocket control frames -----------------------------------
        if "Operation" in Data:
            op = Data["Operation"]
            if op == "Ping":
                if self.debug:
                    Domoticz.Debug("Ping received - sending Pong")
                Connection.Send({"Operation": "Pong", "Payload": "Pong", "Mask": secrets.randbits(32)})
            elif op == "Pong":
                if self.debug:
                    Domoticz.Debug("Pong received")
            elif op == "Close":
                Domoticz.Log("WS Close frame received.")
                self._connected = False
            return

        # --- Normal payload --------------------------------------------
        if "Payload" in Data:
            raw = Data["Payload"]

            # Guard: if the server still negotiated compression despite our
            # empty Sec-WebSocket-Extensions header, the payload arrives as
            # bytes (compressed).  Log an actionable error and skip.
            if isinstance(raw, (bytes, bytearray)):
                Domoticz.Error(
                    "[Matter] Binary/compressed payload received - "
                    "the server is still using permessage-deflate. "
                    f"First bytes: {raw[:40]!r}"
                )
                return

#            if self.debug:
#                Domoticz.Debug(f"WS <- {raw[:300]}")
            self.matter.on_message(raw)

    def onDisconnect(self, Connection):
        Domoticz.Log("Disconnected from Matter server.")
        self._connected = False

    def onCommand(self, DeviceID, Unit, Command, Level, Color):
        self.matter.on_command(DeviceID, Unit, Command, Level, Color)

    def onNotification(self, Name, Subject, Text, Status, Priority, Sound, ImageFile):
        pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self):
        self._connected = False
        Domoticz.Log(f"Connecting to {self._host}:{self._port} ...")
        self.conn = Domoticz.Connection(
            Name="MatterWS",
            Transport="TCP/IP",
            Protocol="WS",
            Address=self._host,
            Port=str(self._port),
        )
        self.conn.Connect()

    def _send_ws(self, payload: str):
        """Send a JSON string as a WebSocket text frame (called by matter.py)."""
        if not self._connected or self.conn is None:
            Domoticz.Error("[Matter] _send_ws: not connected")
            return
        if self.debug:
            Domoticz.Debug(f"WS -> {payload[:300]}")
        self.conn.Send({"Payload": payload, "Mask": secrets.randbits(32)})


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
