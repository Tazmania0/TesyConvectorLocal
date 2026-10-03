# Home Assistant Tesy integration
[![Type](https://img.shields.io/badge/Type-Custom_Component-orange.svg)](https://github.com/TheByteStuff/RemoteSyslog_Service)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)

This custom integration allows you to control your Tesy Convector directly from Home Assistant. It provides seamless control of the convector's heating modes and target temperature.
## Features
- HVAC Modes: Switch between Auto, Heat, and Off modes.
- Target Temperature Adjustment: Easily set your desired temperature.
- Supports External Temperature Sensors: Integrate a separate Home Assistant sensor to track temperature.
- Support for convector temperature correction
- Local API Communication: Utilizes the local API, ensuring fast and secure control without relying on cloud services.
- add "set_opened_window" service to set opened window status
- Smart window open detection:
    * Based on temperature drop rate & recovery
    * Customizable hysteresis delay to avoid toggling
    * Binary_sensor for window open status

## External-temperature software control

In HEAT mode, software control uses one validated, EMA-filtered external sensor
sample per poll. The ON threshold is target minus hysteresis; predictive cutoff
and learned post-OFF coast peaks reduce repeatable overshoot. The learned
correction is bounded and decays on cycles without overshoot. Minimum ON/OFF
durations protect normal switching, while sensor failure transfers control
immediately to the device thermostat.

Missing, invalid, out-of-range, or more than 180-second-old sensor reports trigger
firmware fallback. Freshness uses Home Assistant's `last_reported` timestamp,
or `last_updated` on older versions. The firmware receives the floored user
target (22.5 °C becomes 22 °C), including after restart. Recovery starts with
fresh controller history. Sensors must report at least every three minutes,
even when their temperature has not changed.

Entity attributes expose sensor validity and age, filtered temperature,
controller state, firmware fallback, prediction, effective cutoff, learned
overshoot correction, slow average correction, and completed-cycle duty.
Software ON retains `ceil(target)` as the device thermostat safety limit;
software OFF lowers the setpoint to preserve the device connection.

## Tested with:
- Tesy Convector CN06AS

## Installation

### Via HACS
* Add this repo as a ["Custom repository"](https://hacs.xyz/docs/faq/custom_repositories/) with type "Integration"
* Click "Install" in the new "Tesy" card in HACS.
* Install
* Restart Home Assistant
* Click Add Integration and choose Tesy, follow the configuration flow

### Manual Installation (not recommended)
* Copy the entire `custom_components/tesy_convector_local/` directory to your server's `<config>/custom_components` directory
* Restart Home Assistant

### set_opened_window service usage
    service: tesy_convector_local.set_opened_window
    data:
      entity_id: climate.tesy_convector_cn06as
      status: "on"

