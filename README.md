# sc2mqtt
Skoda Connect 2 MQTT 

This is a connector between Skoda Connect and your smart home software (best connects with Home Assistant, but should work with any MQTT-capable).

## Usage
Call the sc2mqtt.py file directly. It will search for `config.json` in the current directory; if none found (or an invalid one), it will create a `config.json.sample` and exit.

Upon successful start, it will poll Skoda Connect every 60 seconds for a status update on every vehicle detected for the account and post the sensor values over MQTT.

## TODO
- make more stuff configurable
- tidy up code


## Thanks
- original idea by https://github.com/TA2k
