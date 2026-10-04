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

Missing, invalid, out-of-range, or more than 450-second-old sensor reports trigger
firmware fallback. Freshness uses Home Assistant's `last_reported` timestamp,
or `last_updated` on older versions. The firmware receives the floored user
target (22.5 °C becomes 22 °C), including after restart. Recovery starts with
fresh controller history. Sensors must report at least every 7.5 minutes,
even when their temperature has not changed. For BTHome sensors, the integration
also observes newly decoded temperature samples, including unchanged values
that Home Assistant suppresses at the entity level. Cached temperature fields,
RSSI-only reports, and other measurements do not refresh temperature freshness.
The adapter attaches automatically after the sensor loads, reattaches after
sensor replacement, and uses HA's existing decoder without additional scanning
or encryption credentials. It waits for a new sample after establishing its
initial baseline; restored/cached data does not count as a new receipt.

Other integrations continue to use `last_reported` / `last_updated`. If they
suppress unchanged values, an optional **Last temperature report timestamp**
sensor can be selected in the options. It must provide a timezone-aware timestamp
of the latest received temperature measurement, including unchanged readings.
Do not select a generic device last-seen, battery, RSSI, or connectivity heartbeat.
Invalid/unavailable temperature values still cause fallback even with a recent
timestamp. Future, malformed, unavailable, or old timestamps cannot extend
freshness. Temperature control still evaluates every 10 seconds; the 450-second
limit is an outage allowance, not a response delay. Climate diagnostic attributes
`external_temp_freshness_source` and `external_temp_reason` show which receipt
source is used and why a reading was rejected, and are excluded from recording.

The learned overshoot correction and last five completed duty-cycle samples are
saved in Home Assistant storage after each completed heating cycle. They survive
restarts and are restored once fresh sensor feedback is available, provided the
sensor, target, and tuning settings still match. The heater phase, timers, EMA,
temperature histories, and unfinished coast are always initialized afresh.
Duty cycle remains a diagnostic; heating decisions continue to use live feedback.

During Home Assistant startup, initialization reads the convector's actual
status without writing mode, power, window settings, or temperature correction.
`onOff=off` reports OFF even when the device's stored mode says heating or HA
remembers HEAT. An ON device reports its heating or program mode. Saved-mode
restoration during a running-HA integration reload retains its existing policy.
After HA starts, normal control resumes for an ON heater. A missing room sensor
has a bounded 60-second startup window before firmware fallback; an OFF heater
stays OFF. Local requests are serialized with a 0.5-second settling gap, and
an already active heating mode is reused instead of issuing another `setMode`.
The saved temperature correction is sent after HA starts, even when the heater
is OFF or the room sensor is unavailable. It stays pending through failed writes
and is retried on later polls until acknowledged; this never turns the heater ON.
The same retryable correction restore runs after any communication failure,
once status polling succeeds again. This also covers a brief power cycle that
never reaches the three-failure threshold for showing the heater unavailable.
If status reports a correction different from the saved value, it is restored
even when the power cycle occurred entirely between polls.
An intentional software OFF phase retains `onOff=on` with a 10°C setpoint:
startup reports HEAT intent and preserves that low setpoint while waiting for
the room sensor, then decides ON/OFF from fresh feedback. An unavailable sensor
past the startup window invokes firmware fallback at the ordinary room target.

Entity attributes expose sensor validity and age, filtered temperature,
controller state, firmware fallback, prediction, effective cutoff, learned
overshoot correction, slow average correction, and completed-cycle duty.
Software ON starts with `ceil(target)` as the device thermostat limit;
fresh cloud telemetry can justify bounded headroom as described below.
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
This includes all cloud temperature, firmware cutoff, headroom, and heat-trail
sensors; adding those diagnostics requires no extra Recorder filters. None of
these sensors declares a statistics state class, and their matching climate
attributes are also excluded from recording.
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

## Optional MyTESY cloud heating telemetry

An optional, experimental connection can show the heater's reported heating
activity separately from the software heat request. It uses the MyTESY Android
app's cloud discovery and MQTT protocol; it does not publish heater commands.
This protocol is unofficial. A user has verified the connection on a live heater;
the adaptive control still needs comparison over complete heating cycles.

In the integration's **Configure** options, enable **Use MyTESY cloud heating
telemetry**, sign in to MyTESY, then select the same physical heater. The account
password is used only for discovery and is not saved. The selected device token
and MQTT credentials are saved in Home Assistant's config entry. To select a
different heater or refresh credentials, check **Choose or reconnect cloud device**.
Unchecking the telemetry option stops the subscription without restarting local
temperature control; the saved connection details remain available for re-enabling.

The device page gains **Device reported heating**, **Cloud device temperature**,
**Cloud telemetry connected**, and **Cloud heating report age** diagnostics.
They use the same Recorder exclusion naming pattern documented above and do not
generate long-term statistics. The reported device temperature is telemetry;
the selected external room sensor remains the controller's feedback.

While enabled, fresh reports supply the climate Heating/Idle indicator. Missing
reports, disconnects, and reports older than 180 seconds make the heating
indicator unknown. Local OFF/window inhibition still displays Off. Unknown
telemetry restores the normal ON ceiling and configured prediction horizon.
Cloud connectivity alone does not prove a fresh heating report. Requested duty
cycle remains a record of commands, not element runtime or measured power.

With software control and a valid external sensor, heating transitions now
anchor learning to when the device reports the element stopped. Diagnostics
show the internal cloud temperature's rise rate, its extra rise after stopping,
and the time to its observed peak. Separately, the room's rise after actual
stopping gradually tunes the predictive coast horizon, bounded by the configured
lag time. Interrupted coasts and stale telemetry are discarded.

The controller also estimates firmware cutoff temperature and time to cutoff
from internal cloud temperature trends and observed thermostat stops. When the
room and its predicted coast remain below the heating band, a sustained firmware
veto can increase the ON setpoint in 1°C steps, no faster than every 120 seconds.
After a learned cutoff, its internal rise rate can anticipate another veto.
Cloud temperature is rounded in 0.5°C steps: rate estimation requires at least
two steps over a minute, and cutoff forecasts allow ±0.25°C per reading.
Plateaus or single-step flicker leave the rate/forecast unknown. Three confirmed
firmware stops within five minutes can identify short cycling even when the
rounded temperature does not resolve a slope; the same headroom limits apply.
The firmware setpoint never exceeds room target + 3°C or the device's 30°C limit.
The external sensor still controls OFF; a fresh heating report lets reaching the
room target override the minimum ON timer. No MQTT control commands are sent.

New diagnostic sensors expose the firmware headroom and applied setpoint,
estimated cutoff temperature/time, internal rise rate, internal heat trail, and
learned room coast horizon. These measurements and headroom are session-local;
after restart, stale telemetry, or loss of room feedback, adaptation starts afresh.

This uses a separate secure WebSocket MQTT connection and does not require or
change Home Assistant's own MQTT broker configuration. Reconnects back off to
five minutes. If discovery reports no devices, verify the heater is in MyTESY's
current cloud device list; legacy tesyCloud devices are not supported here.
After installation, enable it and compare Device reported heating with MyTESY
through an observed heating/idle cycle before relying on the new indicator.
See [API investigation](docs/mytesy-api-investigation.md) for protocol evidence
and local endpoint probe results.

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

