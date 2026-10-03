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

The learned overshoot correction and last five completed duty-cycle samples are
saved in Home Assistant storage after each completed heating cycle. They survive
restarts and are restored once fresh sensor feedback is available, provided the
sensor, target, and tuning settings still match. The heater phase, timers, EMA,
temperature histories, and unfinished coast are always initialized afresh.
Duty cycle remains a diagnostic; heating decisions continue to use live feedback.

Entity attributes expose sensor validity and age, filtered temperature,
controller state, firmware fallback, prediction, effective cutoff, learned
overshoot correction, slow average correction, and completed-cycle duty.
Software ON retains `ceil(target)` as the device thermostat safety limit;
software OFF uses the supported minimum setpoint of 10 °C to preserve the device
connection without the firmware continuing to heat a normally warm room.

`sw_heat_requested` and `duty_cycle_pct` describe commanded phases, not measured
element activity or electricity use. `internal_temp`, `raw_external_temp`,
`external_temperature_error`, and `firmware_limit_estimated` help distinguish room
undershoot from the built-in thermostat limiting an ON request. The limiter flag
is an estimate based on the internal sensor reaching the safety setpoint.

See [temperature-control assessment](docs/temperature-control.md) for simulation
results, limitations, and how to compare software control with firmware in a room.

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

`entity_id` also accepts a list of Tesy climate entities. The service is shared
across heaters and always applies the normal manual window handling to each
selected entity.

