# Temperature-control assessment

Software control provides feedback from the selected room sensor, half-degree
targets, predictive cutoff, and bounded learning from post-OFF coast peaks. This
can help when the heater's internal sensor does not represent the occupied part
of the room. It does not establish that software control is more accurate than
the device firmware in every installation.

TESY documents internal-sensor temperature correction and adaptive start.
Adaptive start anticipates heating for a scheduled target time; the manual does
not publish enough detail about ordinary thermostat switching to reproduce it.
See the [CN06 manual](https://tesy.com/web/files/products/128/line_files/User-Manual-TESY-CN06-EA-AS-W.pdf),
English temperature correction and adaptive start sections, page 14.

## Repeatable synthetic checks

The climate tests simulate a room losing heat to a fixed outdoor temperature,
heater output that decays after OFF, and a delayed external temperature sensor.
Both controllers receive the same sensor delay and EMA filter and enforce
60-second minimum phases. The reference uses plain hysteresis, with ON at
21.7 °C and OFF at 22.0 °C. It is not an implementation of Tesy firmware.

The software controller uses a 22.0 °C target, 0.3 °C hysteresis, six-minute
prediction horizon, EMA alpha 0.2, and disabled average correction. Each run
lasts six simulated hours; temperature statistics use the final two hours.

| Heater coast time constant / sensor time constant | Reference peak | Software peak | Reference mean absolute error | Software mean absolute error | Heating starts, reference / software, six hours |
| --- | --- | --- | --- | --- | --- |
| 30 s / 10 s | 22.220 °C | 21.954 °C | 0.161 °C | 0.194 °C | 25 / 45 |
| 90 s / 60 s | 22.433 °C | 22.050 °C | 0.240 °C | 0.176 °C | 15 / 27 |
| 180 s / 120 s | 22.653 °C | 22.071 °C | 0.328 °C | 0.191 °C | 11 / 19 |

These checks show that prediction and adaptation can reduce thermal coast peaks.
They also expose a tradeoff: aggressive prediction increases cycling and can
produce more undershoot in a room with little inertia. The default six-minute
horizon is a starting value, not an experimentally established optimum for all
heaters and rooms. The tests assert bounded settled temperatures and reduced
overshoot, alongside the separate failure, restart, recovery, and timing tests.

The model does not reproduce firmware thermostat hysteresis, internal-sensor
bias, actual relay state, changing weather, sunlight, doors, or heater power
modulation. It cannot demonstrate savings or prove better control than firmware.

## The firmware remains part of the control loop

Software ON raises the firmware setpoint to `ceil(target)`. If the internal sensor
already reaches that value, the firmware may stop the element even while the
external sensor remains below target. Raising the software duty cycle will not
overcome this safety limiter. A large persistent difference between internal and
room sensors needs investigation and appropriate sensor correction.

Software OFF now lowers the setpoint to the supported minimum of 10 °C to keep
the device connection alive. The previous `floor(target) - 1` value could still
allow heating when the internal sensor read colder than the room sensor. The
minimum prevents that during ordinary room-temperature operation, while the
firmware can still heat at very low temperatures. Neither requested duty nor
`hvac_action` is an independently measured power or relay signal.

Firmware-only control continues without Home Assistant or a network connection.
Software control depends on both. If Home Assistant stops during software OFF,
the device retains the 10 °C setpoint until control resumes or someone changes it
at the device. Sensor fallback and startup restoration protect against invalid
feedback while the integration is running; they cannot execute while Home
Assistant itself is stopped.

The new `firmware_limit_estimated` attribute compares the internal reading with
the ON safety setpoint. It is useful evidence of a possible limiter interaction,
but does not replace actual element or power measurements.

## Comparing with firmware in your room

1. Keep the same external sensor, position, and integer target for both runs.
   An integer target avoids comparing software's half-degree target with a
   floored firmware fallback target.
2. Record a settled period with software control disabled, then a comparable
   period with software control enabled. Exclude startup warmup and window events.
   Repeat in both orders where weather and occupancy allow.
3. Use the raw external reading to compare mean error, mean absolute error,
   upper and lower excursions, and time spent within a chosen comfort band.
   Controller-filtered temperature alone can hide brief peaks.
4. Compare switching counts as well as temperature. Use an existing power
   measurement if available; requested duty alone does not measure consumption.
5. Check `firmware_limit_estimated` and sensor differences before attributing
   sustained undershoot to the prediction horizon. Review rising ramps and coast
   peaks before changing lag or smoothing settings, changing one setting at a time.

The software aims to keep the control band below target to avoid persistent
overshoot. Its mean can consequently sit below target. A smaller peak alone is
not sufficient evidence of better comfort or temperature accuracy.
