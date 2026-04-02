# Mattericz
Matter plugin for Domoticz

## Requirements
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

## Threads
Thread sensors should work but are not tested yet.

## Supported Nodes
Currently supported are
* Temperature
* Humidity
* On/Off Relay
* Dimmer
Added but untested
* Voltage
* Current
* Power
* Energy

## Add own sensors
Edit `matter.py`. 
Add a line to `TypeDG` containing
```
(cluster_id, attribute_id):{'DomoType': 'Type', 'Multiplier':1.0}
```
Where DomoType is the text-type of the Domoticz-device and Multiplier is a multiplier that needs to be multiplied to the Matter value before bringing it to domoticz.
If Domoticz expects anything different than `nValue=0` and `sValue=value` you need to add it to `_m2d()`. Commands are handled in `on_command`.

## Contributing
Currently proof of concept state with only temperature and humidity sensors and simple on/off switches supported. You are welcome to fork or contribute.
Currently no commissioning via this plugin supported.
