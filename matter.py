#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
matter.py  –  Matter protocol logic for the Domoticz Matter plugin.

Add own matter types:
1. Add to _SINGLE_TYPES: (cluster_id, attribute_id) -> {'DomoType': ..., 'Multiplier': ...}
2. Add to _COMBINED_TYPES if two attributes should form one Domoticz device.
3. Add command handling to on_command() if needed.

_SINGLE_TYPES DomoType can be:
  - A Domoticz TypeName string (e.g. 'Temperature', 'Switch', 'Dimmer')
  - A "Type;Subtype;Switchtype" string for numeric type IDs (e.g. '113;0;0')

_COMBINED_TYPES maps a combined DomoType name to the list of (cluster_id, attribute_id)
tuples that must ALL be present (on the same endpoint, except Temp+Hum which
can span endpoints) before the combined device is created.

_SUPPRESSED_BY maps suppressed types, e.g. switch is suppressed if dimmer exists

_TRANSFORM_VALUES describes special value transformation if matter delivers e.g. a struct
or a list instead of ints

Special rules (hard-coded):
  - Dimmer (0x0008/0x0000): the On/Off cluster (0x0006/0x0000) on the same
    endpoint is suppressed – no separate Switch device is created.
    On/Off attribute updates are reflected on the Dimmer device (nValue only).
  - Temp+Hum: Temperature and Humidity may live on different endpoints as long
    as the node has exactly one of each.
  - kWh: power (0x0090/0x0008) + energy (0x0091/0x0001) on the same endpoint.
    The standalone 'Usage' type is replaced by kWh in this case.
"""

import json

import DomoticzEx as Domoticz

# ---------------------------------------------------------------------------
# Type database – single-attribute devices
# ---------------------------------------------------------------------------

_SINGLE_TYPES = {
    (0x0402, 0x0000): {'DomoType': 'Temperature',      'Multiplier': 0.01},
    (0x0405, 0x0000): {'DomoType': 'Humidity',         'Multiplier': 0.01},
    (0x0006, 0x0000): {'DomoType': 'Switch',           'Multiplier': 1.},   # On/Off cluster
    (0x003b, 0x0001): {'DomoType': 'Switch',           'Multiplier': 1.},   # Switch cluster
    (0x0045, 0x0000): {'DomoType': 'Switch',           'Multiplier': 1.},   # Boolean state
    (0x0008, 0x0000): {'DomoType': 'Dimmer',           'Multiplier': 0.392},
    (0x0090, 0x0004): {'DomoType': 'Voltage',          'Multiplier': 0.001},
    (0x0090, 0x0005): {'DomoType': 'Current (Single)', 'Multiplier': 0.001},
    (0x0090, 0x0008): {'DomoType': 'Usage',            'Multiplier': 0.001}, # will probably not exist as single type, according to matter-survey
    (0x0091, 0x0001): {'DomoType': '113;0;0',          'Multiplier': 1.0},   # will probably not exist as single type, according to matter-survey
}

# ---------------------------------------------------------------------------
# Combine database – multi-attribute combined Domoticz devices
#
# Each entry:
#   combined_type_name -> {
#       'DomoType': str,            Domoticz TypeName (or "Type;Sub;Switch")
#       'Options': dict,            optional Domoticz Options
#       'Members': [                ordered list of contributing attributes
#           (cluster_id, attribute_id), ...
#       ],
#       'cross_endpoint': bool,     True = members may span endpoints
#   }
# ---------------------------------------------------------------------------

_COMBINED_TYPES = {
    'Temp+Hum': {
        'DomoType': 'Temp+Hum',
        'Members': [
            (0x0402, 0x0000),   # Temperature
            (0x0405, 0x0000),   # Humidity
        ],
        'cross_endpoint': True,
    },
    'kWh': {
        'DomoType': 'kWh',
        'Options': {'EnergyMeterMode': '1'},
        'Members': [
            (0x0090, 0x0008),   # Power (W)
            (0x0091, 0x0001),   # Energy (Wh)
        ],
        'cross_endpoint': False,
    },
}

# Clusters that are suppressed when a more capable device already covers them.
# Maps (cluster_id, attribute_id) -> set of DomoTypes that absorb it.
_SUPPRESSED_BY = {
    (0x0006, 0x0000): {'DomoType':'Dimmer','Matter':(0x0008,0x0000)},   # On/Off suppressed when Dimmer exists on same ep
}

_TRANSFORM_VALUES = {
    (0x0091, 0x0001): lambda v: v["0"],  # Energy Measurement Struct
}

# Combined type names that absorb individual _SINGLE_TYPES entries.
# Maps (cluster_id, attribute_id) -> combined_type_name
_ABSORBED_BY_COMBINED: dict = {}
for _cname, _cdef in _COMBINED_TYPES.items():
    for _member in _cdef['Members']:
        _ABSORBED_BY_COMBINED[_member] = _cname

# ---------------------------------------------------------------------------
# DeviceID helpers
# ---------------------------------------------------------------------------

def _make_device_id(node_id: int, endpoint_id: int, cluster_id: int, attribute_id: int) -> str:
    """Stable Domoticz DeviceID string (Varchar(25) max)."""
    return f"{node_id}/{endpoint_id}/{cluster_id}/{attribute_id}"


def _make_combined_id(node_id: int, endpoint_id: int, combined_name: str) -> str:
    """DeviceID for a combined device. endpoint_id is the 'primary' endpoint."""
    tag = combined_name.replace('+', '_').replace(' ', '_')
    return f"{node_id}/{endpoint_id}/{tag}"


def _parse_attribute_path(path: str):
    """
    Parse 'endpoint/cluster/attr[/...]'.
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
# Domoticz unit creation helper
# ---------------------------------------------------------------------------

def _create_unit(name: str, device_id: str, domotype: str, options: dict = None):
    """Create a Domoticz Unit. domotype may be a TypeName or 'Type;Sub;Switch'."""
    kwargs = dict(Name=name, Unit=1, DeviceID=device_id, Used=1)
    if options:
        kwargs['Options'] = options
    if domotype[0].isdigit():
        t, s, sw = domotype.split(';')
        kwargs.update(Type=int(t), Subtype=int(s), Switchtype=int(sw))
    else:
        kwargs['TypeName'] = domotype
    try:
        Domoticz.Unit(**kwargs).Create()
        return True
    except Exception as exc:
        Domoticz.Error(f"[Matter] Device creation failed ({device_id}): {exc}")
        return False


# ---------------------------------------------------------------------------
# MatterBridge
# ---------------------------------------------------------------------------

class MatterBridge:
    """
    Translates python-matter-server WebSocket messages into Domoticz device
    updates. All I/O goes through callbacks supplied by plugin.py.
    """

    def __init__(self, devices, debug: bool = False):
        self._devices = devices
        self._debug   = debug
        self._send_fn = None
        self._msg_id  = 0

    # ------------------------------------------------------------------
    # Called by plugin.py
    # ------------------------------------------------------------------

    def on_connected(self, send_fn):
        self._send_fn = send_fn
        Domoticz.Log("[Matter] Connected – sending start_listening")
        self._send_command("start_listening")

    def on_message(self, raw: str):
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
        Domoticz.Log(f"on_command - {DeviceID=} {Unit=} {Command=} {Level=}, {Color=}")
        parsed = _parse_attribute_path(DeviceID)
        if parsed is None:
            return
        node_id, endpoint_id, cluster_id = parsed
        args = None
        if Command in ("On", "Off") and cluster_id == 0x0006:
            args = {"endpoint_id": endpoint_id, "node_id": node_id,
                    "payload": {}, "cluster_id": cluster_id, "command_name": Command}
        elif Command in ("On", "Off") and cluster_id == 0x0008:
            # Dimmer device: On/Off goes to the On/Off cluster (0x0006)
            args = {"endpoint_id": endpoint_id, "node_id": node_id,
                    "payload": {}, "cluster_id": 0x0006, "command_name": Command}
        elif Command == "Set Level":
            args = {"endpoint_id": endpoint_id, "node_id": node_id,
                    "payload": {"level": int(Level / 100 * 255), "transitionTime": 3},
                    "cluster_id": cluster_id, "command_name": "MoveToLevel"}
            if Level > 0:
                self._send_command("device_command", args)
                args = {"endpoint_id": endpoint_id, "node_id": node_id,
                    "payload": {}, "cluster_id": 0x0006, "command_name": "On"}
        if args is not None:
            self._send_command("device_command", args)

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
        result     = msg.get("result")
        message_id = msg.get("message_id", "")
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
        """data = [node_id, "endpoint/cluster/attr", value]"""
        if not isinstance(data, (list, tuple)) or len(data) != 3:
            return
        node_id, path, value = data
        parsed = _parse_attribute_path(path)
        if parsed is None:
            return
        endpoint_id, cluster_id, attribute_id = parsed
        key = (cluster_id, attribute_id)
        if key not in _SINGLE_TYPES:
            return

        if (cluster_id, attribute_id) in _TRANSFORM_VALUES: #handle transforms
            transform = _TRANSFORM_VALUES.get((cluster_id, attribute_id), lambda v: v)
            value = transform(value)


        # --- Check if this attribute belongs to a combined device -------
        cname = _ABSORBED_BY_COMBINED.get(key)
        if cname is not None:
            cdef = _COMBINED_TYPES[cname]
            members_needed = cdef['Members']
            cross_ep = cdef.get('cross_endpoint', False)

            if cross_ep:
                # Primary endpoint is determined by the first member's device_id.
                # We look for an existing combined device for this node.
                cdev_id = self._find_combined_device_id(node_id, cname)
                if cdev_id is None:
                    # Combined device not created yet – fall through to single device.
                    # (It will be created properly on the next _process_node call,
                    #  e.g. via node_updated event. For now skip.)
                    return
                # Read current member values from the existing combined device sValue,
                # then overwrite the one that just changed.
                member_values = self._read_combined_member_values(
                    node_id, cdev_id, cname, members_needed
                )
                # Update the changed member
                member_values[key] = (endpoint_id, value)
                if None in [v for _, v in member_values.values()]:
                    return  # not all members known yet
                members = [
                    (member_values[m][0], m[0], m[1], member_values[m][1])
                    for m in members_needed
                ]
            else:
                # Same-endpoint combined: use endpoint_id from the incoming event
                cdev_id = _make_combined_id(node_id, endpoint_id, cname)
                if self._find_unit_by_device_id(cdev_id) is None:
                    # Not yet created – skip (will be handled by next _process_node)
                    return
                member_values = self._read_combined_member_values(
                    node_id, cdev_id, cname, members_needed
                )
                member_values[key] = (endpoint_id, value)
                if None in [v for _, v in member_values.values()]:
                    return
                members = [
                    (endpoint_id, m[0], m[1], member_values[m][1])
                    for m in members_needed
                ]

            cinfo = {
                'combined_name': cname,
                'domotype':      cdef['DomoType'],
                'options':       cdef.get('Options'),
                'name':          '',   # not used for updates
                'primary_ep':    endpoint_id,
                'members':       members,
            }
            self._update_combined(node_id, cdev_id, cinfo)
            return

        # --- Not a combined member – update single device ----------------
        self._update_value(node_id, endpoint_id, cluster_id, attribute_id, value)

    def _find_combined_device_id(self, node_id: int, cname: str) -> str:
        """
        Search existing Domoticz devices for a combined device of the given
        type belonging to node_id. Returns the DeviceID or None.
        """
        tag = cname.replace('+', '_').replace(' ', '_')
        prefix = f"{node_id}/"
        suffix = f"/{tag}"
        for dev_id in self._devices:
            if str(dev_id).startswith(prefix) and str(dev_id).endswith(suffix):
                return dev_id
        return None

    def _read_combined_member_values(self, node_id: int, cdev_id: str,
                                      cname: str, members_needed: list) -> dict:
        """
        Return a dict of (cluster_id, attribute_id) -> (endpoint_id, value)
        pre-populated from the current sValue of the combined device.
        Values that cannot be parsed are set to (None, None).

        This allows attribute_updated to update only the changed member
        while keeping the other member's last known value.
        """
        result = {m: (None, None) for m in members_needed}

        dev = self._devices.get(cdev_id)
        if dev is None:
            return result
        unit_obj = dev.Units.get(1)
        if unit_obj is None:
            return result

        svalue = unit_obj.sValue  # e.g. "21.5;55;0" or "120.5;1234.0"
        parts = svalue.split(";") if svalue else []

        if cname == 'Temp+Hum' and len(parts) >= 2:
            # parts[0]=temp, parts[1]=hum (parts[2]=status, ignored here)
            cl_t, at_t = members_needed[0]
            cl_h, at_h = members_needed[1]
            try:
                temp_raw = round(float(parts[0]) / _SINGLE_TYPES[(cl_t, at_t)]['Multiplier'])
                hum_raw  = round(float(parts[1]) / _SINGLE_TYPES[(cl_h, at_h)]['Multiplier'])
                # We don't know original endpoints from sValue; use 0 as placeholder.
                # The caller overwrites the changed member's endpoint anyway.
                result[members_needed[0]] = (0, temp_raw)
                result[members_needed[1]] = (0, hum_raw)
            except (ValueError, ZeroDivisionError):
                pass

        elif cname == 'kWh' and len(parts) >= 2:
            cl_p, at_p = members_needed[0]
            cl_e, at_e = members_needed[1]
            try:
                power_raw  = round(float(parts[0]) / _SINGLE_TYPES[(cl_p, at_p)]['Multiplier'])
                energy_raw = round(float(parts[1]) / _SINGLE_TYPES[(cl_e, at_e)]['Multiplier'])
                result[members_needed[0]] = (0, power_raw)
                result[members_needed[1]] = (0, energy_raw)
            except (ValueError, ZeroDivisionError):
                pass

        return result

    # ------------------------------------------------------------------
    # Node processing (initial dump)
    # ------------------------------------------------------------------

    def _process_node(self, node: dict):
        node_id    = node.get("node_id")
        attributes = node.get("attributes") or {}
        if not attributes:
            return
        Domoticz.Log(f"[Matter] Processing node {node_id}: {len(attributes)} attribute(s).")

        # --- Pass 1: collect labels and known attribute values ----------
        ep_labels: dict = {}   # endpoint_id -> label string, "_root" for ep0 fallback
        # known_values: (endpoint_id, cluster_id, attribute_id) -> value
        known_values: dict = {}

        for attr_path, value in attributes.items():
            parsed = _parse_attribute_path(attr_path)
            if parsed is None:
                continue
            endpoint_id, cluster_id, attribute_id = parsed
            if cluster_id == 0x0039 and attribute_id == 0x0005 and value:    #nodelabel for bridged
                ep_labels[endpoint_id] = str(value).strip()
            if cluster_id == 0x0028 and endpoint_id == 0:
                if attribute_id == 0x0005:
                    root_label_5 = str(value).strip() if value is not None else ""
                elif attribute_id == 0x0003:
                    root_label_3 = str(value).strip() if value else None
#            if cluster_id == 0x0028 and attribute_id == 0x0005 and endpoint_id == 0 and value: #nodelable (if not bridged)
#                ep_labels.setdefault("_root", str(value).strip())
            if (cluster_id, attribute_id) in _TRANSFORM_VALUES: #transform values
                transform = _TRANSFORM_VALUES.get((cluster_id, attribute_id), lambda v: v)
                value = transform(value)
            if (cluster_id, attribute_id) in _SINGLE_TYPES:
                known_values[(endpoint_id, cluster_id, attribute_id)] = value

        # Lable-Priority:
        # 1. 0x28,0x5 if not empty
        # 2. else 0x28,0x3
        if root_label_5 is not None:
            if root_label_5 != "":
                ep_labels["_root"] = root_label_5
            elif root_label_3:
                ep_labels["_root"] = root_label_3
        elif root_label_3:
            ep_labels["_root"] = root_label_3

        # --- Pass 2: decide which combined devices to create/update -----
        # combined_map: combined_device_id -> {
        #   'name': str, 'combined_name': str,
        #   'members': [(ep, cl, at, value), ...]
        # }
        combined_map = self._resolve_combined(node_id, known_values, ep_labels)

        # Set of (endpoint_id, cluster_id, attribute_id) already handled by combined devices
        handled_by_combined: set = set()
        for cdev_id, cinfo in combined_map.items():
            for ep, cl, at, val in cinfo['members']:
                handled_by_combined.add((ep, cl, at))
            self._update_combined(node_id, cdev_id, cinfo)

        # --- Pass 3: individual devices for remaining attributes ---------
        for (endpoint_id, cluster_id, attribute_id), value in known_values.items():
            if (endpoint_id, cluster_id, attribute_id) in handled_by_combined:
                continue
            if (cluster_id, attribute_id) in _SUPPRESSED_BY:
                if (endpoint_id, *_SUPPRESSED_BY[(cluster_id, attribute_id)]['Matter']) in known_values:
                    continue
            if (cluster_id, attribute_id) in _TRANSFORM_VALUES: #transform values
                transform = _TRANSFORM_VALUES.get((cluster_id, attribute_id), lambda v: v)
                value = transform(value)
            label = ep_labels.get(endpoint_id) or ep_labels.get("_root")
            self._update_value(node_id, endpoint_id, cluster_id, attribute_id, value, label)

    # ------------------------------------------------------------------
    # Combined device resolution
    # ------------------------------------------------------------------

    def _resolve_combined(self, node_id: int, known_values: dict, ep_labels: dict) -> dict:
        """
        Determine which combined devices to create/update for this node.

        Returns combined_map:
          combined_device_id -> {
              'combined_name': str,
              'domotype': str,
              'options': dict or None,
              'name': str,
              'primary_ep': int,
              'members': [(endpoint_id, cluster_id, attribute_id, value), ...]
          }
        """
        combined_map = {}

        for cname, cdef in _COMBINED_TYPES.items():
            members_needed = cdef['Members']
            cross_ep       = cdef.get('cross_endpoint', False)

            if cross_ep:
                # Members may span endpoints – try to find exactly one of each
                found = {}  # (cluster_id, attribute_id) -> (endpoint_id, value)
                for (ep, cl, at), val in known_values.items():
                    if (cl, at) in members_needed:
                        key = (cl, at)
                        if key in found:
                            found = None  # more than one of this type → ambiguous
                            break
                        found[key] = (ep, val)
                if not found or len(found) != len(members_needed):
                    continue

                # Use the endpoint of the first member as primary
                primary_ep = found[members_needed[0]][0]
                members = [
                    (found[m][0], m[0], m[1], found[m][1])
                    for m in members_needed
                ]
            else:
                # Members must all be on the same endpoint
                # Group known_values by endpoint
                eps_with_all = []
                endpoints = set(ep for (ep, cl, at) in known_values)
                for ep in endpoints:
                    if all((ep, cl, at) in known_values for cl, at in members_needed):
                        eps_with_all.append(ep)
                if not eps_with_all:
                    continue
                for ep in eps_with_all:
                    members = [
                        (ep, cl, at, known_values[(ep, cl, at)])
                        for cl, at in members_needed
                    ]
                    primary_ep = ep
                    cdev_id = _make_combined_id(node_id, primary_ep, cname)
                    label = ep_labels.get(primary_ep) or ep_labels.get("_root")
                    name  = label if label else f"Matter {node_id}/{primary_ep}"
                    combined_map[cdev_id] = {
                        'combined_name': cname,
                        'domotype':      cdef['DomoType'],
                        'options':       cdef.get('Options'),
                        'name':          name,
                        'primary_ep':    primary_ep,
                        'members':       members,
                    }
                continue  # already added per-ep above

            # cross_endpoint path
            cdev_id = _make_combined_id(node_id, primary_ep, cname)
            label = ep_labels.get(primary_ep) or ep_labels.get("_root")
            name  = label if label else f"Matter {node_id}/{primary_ep}"
            combined_map[cdev_id] = {
                'combined_name': cname,
                'domotype':      cdef['DomoType'],
                'options':       cdef.get('Options'),
                'name':          name,
                'primary_ep':    primary_ep,
                'members':       members,
            }

        return combined_map

    # ------------------------------------------------------------------
    # Combined device update / create
    # ------------------------------------------------------------------

    def _update_combined(self, node_id: int, device_id: str, cinfo: dict):
        """Create (if needed) and update a combined Domoticz device."""
        existing_unit = self._find_unit_by_device_id(device_id)
        if existing_unit is None:
            Domoticz.Log(f"[Matter] Creating combined device '{cinfo['name']}' "
                         f"type={cinfo['combined_name']} (DeviceID={device_id})")
            ok = _create_unit(cinfo['name'], device_id, cinfo['domotype'], cinfo.get('options'))
            if not ok:
                return
            existing_unit = 1

        dev = self._devices.get(device_id)
        if dev is None:
            Domoticz.Error(f"[Matter] Combined device {device_id} not found in Devices.")
            return
        unit_obj = dev.Units.get(existing_unit)
        if unit_obj is None:
            return

        cname   = cinfo['combined_name']
        members = cinfo['members']  # [(ep, cl, at, value), ...]

        if cname == 'Temp+Hum':
            # members[0] = Temperature, members[1] = Humidity
            _, cl_t, at_t, val_t = members[0]
            _, cl_h, at_h, val_h = members[1]
            temp_c  = round(val_t * _SINGLE_TYPES[(cl_t, at_t)]['Multiplier'], 1)
            hum_pct = int(round(val_h * _SINGLE_TYPES[(cl_h, at_h)]['Multiplier']))
            # Humidity status heuristic
            if hum_pct < 30:
                hum_status = 2   # Dry
            elif hum_pct > 70:
                hum_status = 3   # Wet
            elif 45 <= hum_pct <= 65:
                hum_status = 1   # Comfort
            else:
                hum_status = 0   # Normal
            nvalue = 0
            svalue = f"{temp_c};{hum_pct};{hum_status}"

        elif cname == 'kWh':
            # members[0] = Power (W), members[1] = Energy counter
            _, cl_p, at_p, val_p = members[0]
            _, cl_e, at_e, val_e = members[1]
            power_w  = round(val_p * _SINGLE_TYPES[(cl_p, at_p)]['Multiplier'], 3)
            energy_wh = round(val_e * _SINGLE_TYPES[(cl_e, at_e)]['Multiplier'], 3)
            nvalue = 0
            svalue = f"{power_w};{energy_wh}"

        else:
            Domoticz.Error(f"[Matter] Unknown combined type: {cname}")
            return

        unit_obj.nValue = nvalue
        unit_obj.sValue = svalue
        unit_obj.Update(Log=True)
        Domoticz.Log(f"[Matter] Combined {device_id} -> {nvalue},{svalue}")

    # ------------------------------------------------------------------
    # Individual device update / create
    # ------------------------------------------------------------------

    def _update_value(self, node_id: int, endpoint_id: int, cluster_id: int,
                      attribute_id: int, value, label: str = None):
        """Create (if needed) and update a single-attribute Domoticz device."""
        if value is None:
            return

        key = (cluster_id, attribute_id)
        if key not in _SINGLE_TYPES:
            return

        domotype   = _SINGLE_TYPES[key]['DomoType']
        multiplier = _SINGLE_TYPES[key]['Multiplier']

        # --- Suppression: On/Off when Dimmer exists on same endpoint ----
        if key in _SUPPRESSED_BY:
            absorbing_types = _SUPPRESSED_BY[key]['DomoType']
            # Check if a Dimmer device exists on this endpoint
            dimmer_id = _make_device_id(node_id, endpoint_id, 0x0008, 0x0000)
            if 'Dimmer' in absorbing_types and self._devices.get(dimmer_id) is not None:
                # Reflect On/Off state on the Dimmer device
                dimmer_dev = self._devices.get(dimmer_id)
                unit_obj = dimmer_dev.Units.get(1) if dimmer_dev else None
                if unit_obj is not None:
                    unit_obj.nValue = int(value)
                    # Preserve existing sValue (level)
                    unit_obj.Update(Log=True)
                    Domoticz.Log(f"[Matter] On/Off -> Dimmer {dimmer_id} nValue={int(value)}")
                return

        device_id     = _make_device_id(node_id, endpoint_id, cluster_id, attribute_id)
        existing_unit = self._find_unit_by_device_id(device_id)

        if existing_unit is None:
            name = label if label else f"Matter {node_id}/{endpoint_id}"
            Domoticz.Log(f"[Matter] Creating device '{name}' (DeviceID={device_id})")
            options = None
            ok = _create_unit(name, device_id, domotype, options)
            if not ok:
                return
            existing_unit = 1

        dev = self._devices.get(device_id)
        if dev is None:
            Domoticz.Error(f"[Matter] Device {device_id} not found in Devices.")
            return
        unit_obj = dev.Units.get(existing_unit)
        if unit_obj is None:
            return

        # --- Convert value to nValue / sValue ---------------------------
        if domotype == 'Humidity':
            nvalue = int(round(float(value * multiplier)))
            svalue = "0"
        elif domotype == 'Switch':
            nvalue = int(value)
            svalue = "On" if value == 1 else "Off"
        elif domotype == 'Dimmer':
            nvalue = 0 if value == 0 else 1
            svalue = str(int(round(float(value * multiplier))))
        elif domotype == '113;0;0':
            nvalue = 0
            svalue = str(round(value["0"] * multiplier, 3))
        else:
            nvalue = 0
            svalue = str(round(value * multiplier, 3))

        unit_obj.nValue = nvalue
        unit_obj.sValue = svalue
        unit_obj.Update(Log=True)
        Domoticz.Log(f"[Matter] Value {device_id} -> {nvalue},{svalue}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_unit_by_device_id(self, device_id: str):
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
        if self._send_fn is None:
            Domoticz.Error(f"[Matter] _send_command called before on_connected ({command})")
            return
        self._msg_id += 1
        msg_id = command if command == "start_listening" else str(self._msg_id)
        payload = {"message_id": msg_id, "command": command}
        if args:
            payload["args"] = args
        raw = json.dumps(payload)
        if self._debug:
            Domoticz.Debug(f"[Matter] -> {raw}")
        self._send_fn(raw)
