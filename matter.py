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
CLUSTER_TEMPERATURE = 0x0402   # Temperature Measurement
ATTR_TEMP_MEASURED  = 0x0000   # MeasuredValue (int16, 0.01 °C)

DOMOTICZ_TEMP_TYPE  = "Temperature"

UNIT_TEMPERATURE    = 1    # one Domoticz Unit per DeviceID in this plugin


def _make_device_id(node_id: int, endpoint_id: int, cluster_id: int) -> str:
    """Stable Domoticz DeviceID string (Varchar(25) max)."""
    return f"matter_{node_id}_{endpoint_id}_{cluster_id:04x}"


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
            Domoticz.Debug(f"[Matter] ← {raw[:400]}")

        if "event" in msg:
            self._handle_event(msg)
        elif "message_id" in msg:
            self._handle_response(msg)

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
        cmd_id = msg.get("message_id", "")

        # start_listening response carries the full node dump in result.nodes
        if cmd_id == "start_listening" and isinstance(result, dict):
            nodes = result.get("nodes", [])
            Domoticz.Log(f"[Matter] start_listening: {len(nodes)} node(s) received.")
            for node in nodes:
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

        if cluster_id == CLUSTER_TEMPERATURE and attribute_id == ATTR_TEMP_MEASURED:
            self._update_temperature(node_id, endpoint_id, value)

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

        for attr_path, value in attributes.items():
            parts = attr_path.split("/")
            if len(parts) < 3:
                continue
            try:
                endpoint_id  = int(parts[0])
                cluster_id   = int(parts[1])
                attribute_id = int(parts[2])
            except ValueError:
                continue

            if cluster_id == CLUSTER_TEMPERATURE and attribute_id == ATTR_TEMP_MEASURED:
                self._update_temperature(node_id, endpoint_id, value)

    # ------------------------------------------------------------------
    # Domoticz device management
    # ------------------------------------------------------------------

    def _update_temperature(self, node_id: int, endpoint_id: int, raw_value):
        """
        raw_value is int16 in 0.01 °C steps (e.g. 2150 → 21.50 °C).
        Creates the Domoticz device if not yet present, then updates it.
        """
        if raw_value is None:
            return
        try:
            temp_c = int(raw_value) / 100.0
        except (TypeError, ValueError):
            Domoticz.Error(
                f"[Matter] Cannot convert temperature: {raw_value!r} "
                f"(node {node_id} ep {endpoint_id})"
            )
            return

        device_id = _make_device_id(node_id, endpoint_id, CLUSTER_TEMPERATURE)

        existing_unit = self._find_unit_by_device_id(device_id)
        if existing_unit is None:
            name = f"Matter Temp {node_id}/{endpoint_id}"
            Domoticz.Log(f"[Matter] Creating device '{name}' (DeviceID={device_id})")
            try:
                Domoticz.Unit(
                    Name=name,
                    Unit=UNIT_TEMPERATURE,
                    DeviceID=device_id,
                    TypeName=DOMOTICZ_TEMP_TYPE,
                    Used=1,
                ).Create()
            except Exception as exc:
                Domoticz.Error(f"[Matter] Device creation failed: {exc}")
                return
            existing_unit = UNIT_TEMPERATURE

        try:
            dev = self._devices.get(device_id)
            if dev is None:
                Domoticz.Error(f"[Matter] Device {device_id} not found in Devices.")
                return
            unit_obj = dev.Units.get(existing_unit)
            if unit_obj is None:
                return
            new_svalue = f"{temp_c:.1f}"
            if unit_obj.sValue != new_svalue:
                unit_obj.nValue = 0
                unit_obj.sValue = new_svalue
                unit_obj.Update(Log=True)
                Domoticz.Log(f"[Matter] Temperature {device_id} → {temp_c:.1f} °C")
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
            Domoticz.Debug(f"[Matter] → {raw}")
        self._send_fn(raw)
