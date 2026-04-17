
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Domoticz Matter Plugin

Requires Domoticz >= 17725 (2026.1 beta)

<plugin key="Mattericz" name="Mattericz (python-matter-server)" author="fleinze" version="0.9.0"
        externallink="https://github.com/fleinze/Mattericz">
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
# picks up changes without a full restart.
for _mod in ('matter', 'ws_framing'):
    if _mod in sys.modules:
        del sys.modules[_mod]

from matter import MatterBridge
from ws_framing import WsConnection


class BasePlugin:
    """Main plugin class – lifecycle managed by Domoticz."""

    def __init__(self):
        self.matter      = None
        self._ws         = None      # WsConnection or WsRawConnection
        self.debug       = False
        self._hb_count   = 0
        self._reconnect_interval = 6
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
        if self._ws:
            self._ws.disconnect()
            self._ws = None
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
        ok = self._ws.on_connect(Status, Description)
        if not ok:
            self._connected = False

    def onMessage(self, Connection, Data):
        for msg in self._ws.on_message(Data):
            if msg is None:
                # Close frame
                Domoticz.Log("[WS] Connection closed by server.")
                self._connected = False
                return
            if isinstance(msg, tuple):
                if msg[0] == "ping":
                    self._ws.pong(msg[1])
#                elif msg[0] == "handshake_done":
#                    # Raw TCP mode: handshake just completed
#                    self._connected = True
#                    self.matter.on_connected(self._ws.send)
                # other tuples ignored
                continue
            # Plain str – but for Protocol="WS" the handshake_done signal
            # comes via Status=101 in on_message, which returns early without
            # yielding – so we call on_connected here on first real message
            # only if not yet connected.
            if not self._connected:
                self._connected = True
                self.matter.on_connected(self._ws.send)
            self.matter.on_message(msg)

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
        self._ws = WsConnection(
            self._host, int(self._port), self._path, self.debug
        )
        self._ws.connect()


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
