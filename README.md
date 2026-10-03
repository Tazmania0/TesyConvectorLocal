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

These values are also exposed as enabled-by-default diagnostic entities on the
heater's device page. Sensors include Controller state, Requested duty cycle,
Room temperature error, Internal temperature, Filtered external temperature,
Predicted temperature, Effective OFF threshold, and Learned overshoot correction.
Binary sensors show External temperature valid, Firmware fallback active,
Software heating requested, and Firmware limiting estimated.

After installing a version with the diagnostic entities, restart Home Assistant
and open Settings → Devices & services → Tesy → your heater → Diagnostic.
Numeric diagnostics and controller flags are available when software control is
enabled. Controller state remains visible as `SW_DISABLED` when it is disabled;
duty cycle is unknown until a complete cycle has been measured or restored.
Diagnostic entities only read cached values and do not add device requests.
They do not generate long-term statistics. Their duplicated diagnostic attributes
are excluded from climate history; normal climate temperature and mode history
remain available. Learned controller data still persists independently of Recorder.

To keep the diagnostic entities themselves out of History, merge this into your
Home Assistant `configuration.yaml` and restart Home Assistant:

```yaml
recorder:
  exclude:
    entity_globs:
      - "sensor.*tesy_diagnostic_*"
      - "binary_sensor.*tesy_diagnostic_*"
```

Merge these filters into an existing `recorder` section rather than creating a
second one. Newly registered diagnostic entities use this naming pattern.
Existing or manually renamed diagnostic entities retain their registry IDs:
add those exact IDs under `recorder.exclude.entities` instead, or rename them to
match the pattern. Explicit Recorder include rules can override the exclusion;
check your existing filters. This prevents new recordings and does not delete
existing history. The diagnostics remain available live on the device page.
Home Assistant controls whole-entity recording through
[Recorder filters](https://www.home-assistant.io/integrations/recorder/#configure-filter);
the Diagnostic entity category alone does not exclude an entity from History.

`sw_heat_requested` and `duty_cycle_pct` describe commanded phases, not measured
element activity or electricity use. `internal_temp`, `raw_external_temp`,
`external_temperature_error`, and `firmware_limit_estimated` help distinguish room
undershoot from the built-in thermostat limiting an ON request. The limiter flag
is an estimate based on the internal sensor reaching the safety setpoint.

See [temperature-control assessment](docs/temperature-control.md) for simulation
results, limitations, and how to compare software control with firmware in a room.

## Tested with:
- Tesy Convector CN06AS

## Integration icons

TESY brand images are bundled in `custom_components/tesy_convector_local/brand/`,
including normal and high-resolution icons and logos. Home Assistant 2026.3 or
newer loads these local brand images. Older versions use the central Home
Assistant brands service instead; manifest image-path fields do not enable local
branding. After installing the updated integration, restart Home Assistant and
refresh the browser if the previous icon is cached.

The transparent TESY artwork is reused from the
[Neo2SHYAlien fork](https://github.com/Neo2SHYAlien/TesyConvectorLocal/tree/79d81e4b/custom_components/tesy_convector_local/brand).

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

