#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
matter.py  –  Matter protocol logic for the Domoticz Matter plugin.

This module contains NO networking code. The WebSocket connection is managed
by plugin.py via Domoticz.Connection (Protocol="WS"). This module receives
calls from plugin.py:

  bridge = MatterBridge(devices=Devices, debug=True)
  bridge.on_connected(send_fn)    # called once WS handshake succeeded (Status 101)
  bridge.on_message(raw_str)      # called for every incoming WS Payload

Outgoing messages are sent via the send_fn(str) callback.

Websocket message format (python-matter-server):
  Command   : {"message_id": "<id>", "command": "<cmd>", "args": {...}}
  Response  : {"message_id": "<id>", "result": <data>}  |  error_code field
  Event     : {"event": "<type>", "data": <any>}

Event types of interest:
  attribute_updated  → data = [node_id, "endpoint/cluster/attr", value]
  node_added / node_updated / node_removed

Supported Matter clusters (v0.1):
  0x0402  Temperature Measurement
          Attribute 0x0000  MeasuredValue  (int16, 0.01 °C)
"""

import json

import DomoticzEx as Domoticz

# ---------------------------------------------------------------------------
# Constants – Matter cluster / attribute IDs
# ---------------------------------------------------------------------------

TypeDB = {
(0x0402,0x0000): {'DomoType': 'Temperature',     'Multiplier': 0.01},
(0x0405,0x0000): {'DomoType': 'Humidity',        'Multiplier': 0.01},
(0x0006,0x0000): {'DomoType': 'Switch',          'Multiplier': 1.00},
(0x0008,0x0000): {'DomoType': 'Dimmer',          'Multiplier': 1.00},
(0x0090,0x0004): {'DomoType': 'Voltage',         'Multiplier': 1.00}, #noqa
(0x0090,0x0005): {'DomoType': 'Current (Single)','Multiplier': 1.00}, #noqa
(0x0090,0x0008): {'DomoType': 'Usage',           'Multiplier': 1.00}, #noqa
(0x0091,0x0001): {'DomoType': 'RFXMeter',        'Multiplier': 0.001}, #noqa
}

def _make_device_id(node_id: int, endpoint_id: int, cluster_id: int) -> str:
    """Stable Domoticz DeviceID string (Varchar(25) max)."""
    return f"{node_id}/{endpoint_id}/{cluster_id}"

def _m2d(value, domotype) -> (int, str):
    if domotype == 'Humidity':
        return int(round(float(value))), "0"
    if domotype == 'Switch':
        return int(value), "On" if value == 1 else "Off"
    if domotype == 'Dimmer':
        return 0 if value==0 else 1, str(int(round(float(value*100/255))))
    return 0, str(value)

def _parse_attribute_path(path: str):
    """
    Parse a Matter attribute path in short form "endpoint/cluster/attr"
    (optionally with a trailing list index).
    Returns (endpoint_id, cluster_id, attribute_id) or None.
    """
    try:
        parts = [int(p) for p in path.split("/")]
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
    except (ValueError, AttributeError):
        pass
    return None


# ---------------------------------------------------------------------------
# MatterBridge
# ---------------------------------------------------------------------------

class MatterBridge:
    """
    Translates python-matter-server WebSocket messages into Domoticz device
    updates. All I/O goes through callbacks supplied by plugin.py.
    """

    def __init__(self, devices, debug: bool = False):
        self._devices = devices   # Domoticz Devices dict (DomoticzEx)
        self._debug   = debug
        self._send_fn = None      # injected by on_connected()
        self._msg_id  = 0

    # ------------------------------------------------------------------
    # Called by plugin.py
    # ------------------------------------------------------------------

    def on_connected(self, send_fn):
        """
        Invoked once Status 101 is received (WS handshake done).
        send_fn(payload_str) sends a WS text frame via Domoticz.Connection.
        """
        self._send_fn = send_fn
        Domoticz.Log("[Matter] Connected – sending start_listening")
        self._send_command("start_listening")

    def on_message(self, raw: str):
        """Dispatch one incoming JSON message from the server."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            Domoticz.Error(f"[Matter] Non-JSON message: {raw[:120]}")
            return

        if self._debug:
            Domoticz.Debug(f"[Matter] <- {raw[:400]}")

        if "event" in msg:
            self._handle_event(msg)
        elif "message_id" in msg:
            self._handle_response(msg)

    def on_command(self, DeviceID, Unit, Command, Level, Color):
        """Forward a Domoticz command to the matter device"""
        Domoticz.Log(
            f"on_command - {DeviceID=} {Unit=} "
            f"{Command=} {Level=}, {Color=}"
        )
        parsed = _parse_attribute_path(DeviceID)
        if parsed is None:
            return

        node_id, endpoint_id, cluster_id = parsed
#        Domoticz.Log(self._devices[DeviceID].Units[1].Type)
        command = "device_command"
        if (Command == "On" or Command == "Off") and cluster_id == 0x0006:
            args = {
                "endpoint_id": endpoint_id,
                "node_id": node_id,
                "payload": {},
                "cluster_id": cluster_id,
                "command_name": Command
            }
        elif Command == "Off" and cluster_id == 0x0008:
            args = {
                "endpoint_id": endpoint_id,
                "node_id": node_id,
                "payload": {"level": 0, "transitionTime": 3},
                "cluster_id": cluster_id,
                "command_name": "MoveToLevel"
            }
        elif Command == "Set Level":
            args = {
                "endpoint_id": endpoint_id,
                "node_id": node_id,
                "payload": {"level": int(Level/100*255), "transitionTime": 3},
                "cluster_id": cluster_id,
                "command_name": "MoveToLevel"
            }
        self._send_command(command, args)

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    def _handle_response(self, msg: dict):
        if "error_code" in msg:
            Domoticz.Error(
                f"[Matter] Server error (id={msg.get('message_id')}): "
                f"{msg.get('error_code')} – {msg.get('details', '')}"
            )
            return

        result = msg.get("result")
        message_id = msg.get("message_id", "")

        # start_listening response carries the full node dump in result.nodes
        if message_id == "start_listening" and isinstance(result, list):
            Domoticz.Log(f"[Matter] start_listening: {len(result)} node(s) received.")
            for node in result:
                self._process_node(node)

    def _handle_event(self, msg: dict):
        event = msg.get("event", "")
        data  = msg.get("data")

        if event == "attribute_updated":
            self._handle_attribute_updated(data)
        elif event in ("node_added", "node_updated"):
            if isinstance(data, dict):
                self._process_node(data)
        elif event == "node_removed":
            node_id = data.get("node_id") if isinstance(data, dict) else "?"
            Domoticz.Log(f"[Matter] node_removed: node_id={node_id}")
        else:
            if self._debug:
                Domoticz.Debug(f"[Matter] Unhandled event: {event}")

    # ------------------------------------------------------------------
    # Attribute handling
    # ------------------------------------------------------------------

    def _handle_attribute_updated(self, data):
        """
        data = [node_id, "endpoint_id/cluster_id/attr_id", value]
        """
        if not isinstance(data, (list, tuple)) or len(data) != 3:
            return

        node_id, path, value = data

        parsed = _parse_attribute_path(path)
        if parsed is None:
            return

        endpoint_id, cluster_id, attribute_id = parsed

        if (cluster_id, attribute_id)  in TypeDB:
            self._update_value(node_id, endpoint_id, cluster_id, attribute_id, value)

    def _process_node(self, node: dict):
        """
        Process the attribute map from a node object (initial dump or update).

        Attribute keys use short form: "<endpoint>/<cluster>/<attr>".
        """
        node_id    = node.get("node_id")
        attributes = node.get("attributes") or {}
        if not attributes:
            return

        Domoticz.Log(f"[Matter] Processing node {node_id}: {len(attributes)} attribute(s).")

        # Determine NodeLabel per endpoint:
        # - Cluster 0x0039 (57) = Bridged Device Basic Information, only
        #   present on bridged device endpoints → use that endpoint's own label
        # - Cluster 0x0028 (40) = Basic Information, on ep0 of native devices
        #   → use ep0/40/5
        # We build a cache of ep → label once before the attribute loop.
        ep_labels: dict = {}
        ep_values: list = []
        for attr_path, value in attributes.items():
            parsed = _parse_attribute_path(attr_path)
            if parsed is None:
                continue
            endpoint_id, cluster_id, attribute_id = parsed
            # Bridged Device Basic Information NodeLabel (ep/57/5)
            if cluster_id == 0x0039 and attribute_id == 0x0005 and value:
                ep_labels[endpoint_id] = str(value).strip()
            # Basic Information NodeLabel on ep0 (0/40/5) – fallback
            if cluster_id == 0x0028 and attribute_id == 0x0005 and endpoint_id == 0 and value:
                ep_labels.setdefault("_root", str(value).strip())
            if (cluster_id, attribute_id)  in TypeDB:
                ep_values.append({'endpoint_id':endpoint_id,'cluster_id':cluster_id,'attribute_id':attribute_id,'value':value})
                #Domoticz.Log(f'fleinze: success {TypeDB.get((cluster_id, attribute_id))}')
       # Domoticz.Log(f'{ep_values=}')
        for ep_value in ep_values:
            endpoint_id = ep_value['endpoint_id']
            cluster_id = ep_value['cluster_id']
            attribute_id = ep_value['attribute_id']
            value = ep_value['value']
            label = ep_labels.get(endpoint_id) or ep_labels.get("_root")
            self._update_value(node_id, endpoint_id, cluster_id, attribute_id, value, label)

    # ------------------------------------------------------------------
    # Domoticz device management
    # ------------------------------------------------------------------
    def _update_value(self, node_id: int, endpoint_id: int, cluster_id: int, attribute_id: int, value, label: str=None):
        """ Updates the Domoticz device value. Creates if neccessary """
        if value is None:
            return
        device_id = _make_device_id(node_id, endpoint_id, cluster_id)
        existing_unit = self._find_unit_by_device_id(device_id)
        domotype = TypeDB.get((cluster_id, attribute_id))['DomoType']
        multiplier = TypeDB.get((cluster_id, attribute_id))['Multiplier']
        if existing_unit is None:
            # Use NodeLabel if available, fall back to a generic name.
            name = label if label else f"Matter {node_id}/{endpoint_id}"
            Domoticz.Log(f"[Matter] Creating device '{name}' (DeviceID={device_id})")
            try:
                Domoticz.Unit(
                    Name=name,
                    Unit=1,
                    DeviceID=device_id,
                    TypeName=domotype,
                    Used=1,
                ).Create()
            except Exception as exc:
                Domoticz.Error(f"[Matter] Device creation failed: {exc}")
                return
        try:
            dev = self._devices.get(device_id)
            if dev is None:
                Domoticz.Error(f"[Matter] Device {device_id} not found in Devices.")
                return
            unit_obj = dev.Units.get(existing_unit)
            if unit_obj is None:
                return
            nvalue, svalue = _m2d(value * multiplier, domotype)
            unit_obj.nValue = nvalue
            unit_obj.sValue = svalue
            unit_obj.Update(Log=True)
            Domoticz.Log(f"[Matter] Value {device_id} → {nvalue},{svalue}")
        except Exception as exc:
            Domoticz.Error(f"[Matter] Device update failed: {exc}")


    def _find_unit_by_device_id(self, device_id: str):
        """Return the first unit number for the given DeviceID, or None."""
        dev = self._devices.get(device_id)
        if dev is None:
            return None
        for u in dev.Units:
            return u
        return None

    # ------------------------------------------------------------------
    # WebSocket send
    # ------------------------------------------------------------------

    def _send_command(self, command: str, args: dict = None):
        """Send an RPC command to python-matter-server."""
        if self._send_fn is None:
            Domoticz.Error(f"[Matter] _send_command called before on_connected ({command})")
            return
        self._msg_id += 1
        # Convention: start_listening uses its command name as message_id
        msg_id = command if command == "start_listening" else str(self._msg_id)
        payload = {"message_id": msg_id, "command": command}
        if args:
            payload["args"] = args
        raw = json.dumps(payload)
        if self._debug:
            Domoticz.Debug(f"[Matter] -> {raw}")
        self._send_fn(raw)
