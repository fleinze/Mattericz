# Mattericz
Matter plugin for Domoticz

*This plugin was written with the assistance of AI*

Licensed under the [MIT License](LICENSE.txt)

## Requirements
**Requires Domoticz >= 17725 (2026 beta)**

Requires a running instance of the Open Home Foundation Matter Server.

https://github.com/matter-js/python-matter-server

As a Docker container it can be set up with with this command
```
docker run -d --name matter-server \
  --restart=unless-stopped \
  --security-opt apparmor=unconfined \ 
  -v $(pwd)/data:/data \
  -v /run/dbus:/run/dbus:ro \ 
  --network=host \
  ghcr.io/matter-js/python-matter-server:stable \
  --storage-path /data --paa-root-cert-dir /data/credentials --bluetooth-adapter 0
```
or with `docker compose`:
```
services:
  matterserver:
     image: ghcr.io/matter-js/python-matter-server:stable
     restart: unless-stopped
     volumes:
       - $(pwd)/data:/data
       - /run/dbus:/run/dbus:ro
     network_mode: host
     security_opt:
       - apparmor=unconfined
     command: >
      --storage-path /data
      --paa-root-cert-dir /data/credentials
      --bluetooth-adapter 0
```

## Threads
Thread sensors work. I use the ESP Thread Border Router https://openthread.io/guides/border-router/espressif-esp32

## Supported Nodes
Currently supported are
* Temperature
* Humidity
* On/Off relay
* Dimmer
* Voltage
* Current
* Power
* Energy
* Push-Button switches (generic switch)
* Boolean state sensor (e.g. water leak sensor)
* BatteryLevel

The following devices are combined:
* Temperatur + Humidity
* Power + Energy

Currently tested with:
* Tasmota32 (Temp, Hum, OnOff, Dimmer)
* Ikea Timmerflotte (Thread Temp/Hum-sensor)
* Ikea Grillplats (Thread electrical measurment plug)
* Ikea Bilresa dual button (creates two Domoticz devices: one for the upper button, one for the lower button)
* Ikea Klippbok water leak sensor
* Ikea Kajplats light bulb

Currently not supported:
* Color Temperature
* RGB Color

## Add own sensors
Edit `matter.py`. 
Add a line to `_SINGLE_TYPES` containing
```
(cluster_id, attribute_id):{'DomoType': 'Type', 'Multiplier':1.0}
```
Where DomoType is either the TypeName oder the numeric type in the format Type;Subtype;Switchtype of the Domoticz-device and Multiplier is a multiplier that needs to be multiplied to the Matter value before sending it to domoticz.
If Domoticz expects anything different than `nValue=0` and `sValue=value` you need to add it to `_update_value()`. Commands are handled in `on_command()`.

## Contributing
Currently experimental, use at your own risk. You are welcome to fork and contribute.
Commissioning is done via the python-matter-server web-ui.
