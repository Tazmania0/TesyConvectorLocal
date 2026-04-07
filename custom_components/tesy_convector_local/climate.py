"""Climate platform for Tesy Convector Local integration — v0.3.

Changes vs v0.2:
- Half-degree (0.5 °C) target temperature steps.
- Software on/off controller (CONF_SW_CONTROL_ENABLED):
    * Reads current temperature from the external sensor (required when sw control is on).
    * Implements a bang-bang controller with a configurable dead-band / hysteresis.
    * Enforces minimum ON and minimum OFF phase durations (default 60 s each) to
      reduce wear on the heating element's control relay.
    * When sw control is active the integration drives the device on/off itself;
      the device's own thermostat is bypassed (setTemp is set to max so the device
      always heats when turned on).
    * SW control is suspended during window-open events (device is already forced OFF).
- Window detection logic unchanged from v0.2 but now also works correctly when
  sw_control_enabled=True (external sensor is required for both features anyway).

Fixes carried forward from v0.2 (see original header for full list).
"""

import asyncio
import math
from datetime import timedelta
import logging
from statistics import mean

import voluptuous as vol

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_LAST_HVAC_MODE,
    CONF_LAST_SETPOINT,
    CONF_SW_CONTROL_ENABLED,
    CONF_SW_HYSTERESIS,
    CONF_SW_I_GAIN,
    CONF_SW_EMA_ALPHA,
    CONF_SW_LAG_TIME,
    CONF_SW_MIN_OFF_SEC,
    CONF_SW_MIN_ON_SEC,
    CONF_TEMP_FALL_RATE,
    CONF_TEMP_RECOVERY_ABS,
    CONF_TEMP_RECOVERY_RATE,
    CONF_TEMPERATURE_CORRECTION,
    CONF_TEMPERATURE_ENTITY,
    CONF_USE_EXTERNAL_TEMP,
    CONF_WINDOW_OPEN_ENABLED,
    DEFAULT_SW_HYSTERESIS,
    DEFAULT_SW_I_GAIN,
    DEFAULT_SW_EMA_ALPHA,
    DEFAULT_SW_LAG_TIME,
    DEFAULT_SW_MIN_OFF_SEC,
    SW_I_WINDOW_SEC,
    DEFAULT_SW_MIN_ON_SEC,
    DEFAULT_TEMP_FALL_RATE,
    DEFAULT_TEMP_RECOVERY_ABS,
    DEFAULT_TEMP_RECOVERY_RATE,
    DOMAIN,
    WINDOW_OPEN_TIMEOUT_SEC,
    WINDOW_RECOVERY_HYSTERESIS_SEC,
)
from .tesy_convector import TesyConvector

_LOGGER = logging.getLogger(__name__)

# Temperature sent to device when sw-control drives it ON so the device's own
# thermostat does not cut it off prematurely.
_SW_CONTROL_MAX_SETTEMP = 30

SET_OPENED_WINDOW_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("status"): vol.In(["on", "off"]),
    }
)


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set up the Tesy Convector climate entity."""
    convector = hass.data[DOMAIN][config_entry.entry_id]["device"]
    entity = TesyConvectorClimate(convector, config_entry)
    hass.data[DOMAIN][config_entry.entry_id]["climate_entity"] = entity
    async_add_entities([entity])

    schema, handler = _create_window_service_handler(hass, config_entry.entry_id)
    hass.services.async_register(DOMAIN, "set_opened_window", handler, schema=schema)


def _create_window_service_handler(hass: HomeAssistant, entry_id: str):
    async def handle(call):
        entity_id = call.data["entity_id"]
        status = call.data["status"]
        entity = hass.data[DOMAIN][entry_id].get("climate_entity")
        if not entity:
            _LOGGER.error("Climate entity not found for entry_id %s", entry_id)
            return
        if entity.entity_id != entity_id:
            _LOGGER.warning(
                "set_opened_window: entity ID mismatch, ignoring call to %s", entity_id
            )
            return
        await entity.async_handle_manual_window_status(status)

    return SET_OPENED_WINDOW_SCHEMA, handle


class TesyConvectorClimate(ClimateEntity):
    """Representation of a Tesy Convector as a ClimateEntity."""

    def __init__(self, convector: TesyConvector, config_entry) -> None:
        super().__init__()
        self._device = convector
        self.convector = convector
        self._config_entry = config_entry
        self._remove_update_listener = None

        self._attr_name = f"Tesy Convector {convector.model} ({convector.ip_address})"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            manufacturer="Tesy",
            model=convector.model,
            name=f"Tesy Convector {convector.model} ({convector.ip_address})",
        )
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
        self._attr_min_temp = 10
        self._attr_max_temp = 30
        self._attr_target_temperature_step = 0.5   # v0.3 half-degree steps
        self._attr_unique_id = f"{convector.model}_{convector.ip_address}"

        self._hvac_mode = HVACMode.OFF
        self._device_is_heating: bool = False  # actual heating activity, every poll
        self._attr_hvac_action = HVACAction.OFF
        self._current_temp: float | None = None
        self._target_temp: float | None = None
        # EMA filter state for external temperature sensor.
        # Eliminates high-frequency noise (±0.02–0.05°C) before the value
        # is used by the sw-controller, window detection, or ramp estimator.
        # Initialised to None; first reading seeds it directly (no warmup bias).
        self._ema_temp: float | None = None
        self._bad_response_logged = False

        # Window detection state
        self._temp_history: list[tuple[float, float]] = []
        self._window_opened = False
        self._prev_hvac_mode = None
        self._recovery_started_at = None
        self._window_change_time = None
        self._min_temp_during_open = None
        self._manual_window_override: bool = False

        # External sensor availability tracking
        self._ext_sensor_unavailable_logged: bool = False

        # Boot inhibit: window detection is suppressed until enough temperature
        # history has accumulated to make the trend estimate reliable.
        # Cleared automatically once min_span_sec of data is available.
        self._window_detection_ready: bool = False

        # v0.3 — software on/off controller state
        # True  = device commanded ON by sw-controller
        # False = device commanded OFF by sw-controller
        # None  = sw-control inactive or state unknown (will re-evaluate)
        self._sw_device_on: bool | None = None
        self._sw_last_on_time: float | None = None   # loop.time() of last ON command
        self._sw_last_off_time: float | None = None  # loop.time() of last OFF command
        # Separate temperature history for the sw-controller predictive cutoff.
        # Kept independent of _temp_history (used for window detection) so the
        # two features do not interfere with each other.
        self._sw_temp_history: list[tuple[float, float]] = []

        # Self-tuning overshoot correction.
        # Tracks the peak temperature reached after each heater OFF command.
        # If the peak exceeds the upper threshold (overshoot), the correction
        # grows and shifts the effective upper threshold downward so the heater
        # turns off earlier next cycle.  Decays toward 0 when not overshooting.
        # Units: °C (always >= 0, applied as: effective_upper = upper - _sw_overshoot_correction)
        self._sw_overshoot_correction: float = 0.0
        self._sw_peak_temp: float | None = None   # peak seen during current OFF coast
        # Long-term history for integral correction — spans full SW_I_WINDOW_SEC
        # and is NOT cleared on ON/OFF transitions (unlike _sw_temp_history).
        self._sw_long_history: list[tuple[float, float]] = []
        # Duty cycle tracking: stores the last N complete ON/OFF cycles.
        # Each entry is (on_duration_sec, off_duration_sec).
        # Duty = avg(on / (on+off)) over last SW_DUTY_CYCLES cycles.
        self._sw_duty_cycles: list[tuple[float, float]] = []
        self._sw_duty_pct: int | None = None   # last computed duty %

        # Communication failure tracking — retry gate
        # When the device is unreachable, _comm_failed_at records the loop.time()
        # of the first failure.  Polls are skipped (stale state preserved) until
        # 30 seconds have elapsed, then one retry attempt is made.
        self._comm_failed_at: float | None = None
        self._comm_retry_interval: float = 30.0  # seconds between retry attempts

        # Entity availability — False while device is unreachable.
        # HA blocks commands and automations when unavailable.
        self._attr_available: bool = True

        # Window re-detection inhibit — set when user manually clears a window
        # event (sets HEAT/AUTO while window open). Suppresses re-detection
        # for a fixed period so a still-falling temperature doesn't immediately
        # re-trigger the same window-open event the user just dismissed.
        self._window_inhibit_until: float | None = None
        self._window_inhibit_sec: float = 600.0  # 10 minutes

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_options(self) -> dict:
        return self._config_entry.options if self._config_entry else {}

    async def _apply_stored_temperature_correction(self) -> None:
        """Apply configured temperature correction to the device, if any."""
        correction = self._get_options().get(
            CONF_TEMPERATURE_CORRECTION,
            self._config_entry.data.get(CONF_TEMPERATURE_CORRECTION) if self._config_entry else None,
        )
        if correction is None:
            return
        try:
            await self.convector.set_temperature_correction(int(correction))
            _LOGGER.debug(
                "Applied stored temperature correction %s°C after reconnect",
                correction,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to re-apply temperature correction after reconnect: %s",
                exc,
            )

    def _persist_setpoint(self, temp: float) -> None:
        """Save the Heat setpoint to options so it survives reboots."""
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            options={**self._config_entry.options, CONF_LAST_SETPOINT: temp},
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self):
        self._remove_update_listener = async_track_time_interval(
            self.hass, self.async_update, timedelta(seconds=10)
        )

        # ── Boot sequence ────────────────────────────────────────────────
        # 1. Always clear window-open state on the device.  If HA crashed
        #    or restarted while a window event was active, the device may
        #    still have setOpenedWindow=on which would block heating.
        #    Also resets all in-memory window detection state so the
        #    temperature history starts fresh and cannot cause a false
        #    window-open detection from stale pre-boot readings.
        try:
            await self.convector.set_opened_window("off")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Boot: could not clear window state on device: %s", exc)

        # Reset every piece of window detection state explicitly
        self._window_opened = False
        self._prev_hvac_mode = None
        self._window_change_time = None
        self._min_temp_during_open = None
        self._manual_window_override = False
        self._temp_history = []           # must be empty so trend estimate
        self._window_detection_ready = False  # cannot fire until history builds

        # 2. Restore persisted Heat setpoint before restoring mode, so
        #    async_set_hvac_mode pushes the right temperature — not 30°C.
        saved_setpoint = self._get_options().get(CONF_LAST_SETPOINT)
        if saved_setpoint is not None:
            try:
                self._target_temp = float(saved_setpoint)
                _LOGGER.debug("Boot: restored Heat setpoint %.1f°C", self._target_temp)
            except (TypeError, ValueError):
                _LOGGER.warning("Boot: invalid saved setpoint %r — ignored", saved_setpoint)

        # 3. Restore persisted HVAC mode.
        # For HEAT + sw-control: set _hvac_mode directly without calling
        # async_set_hvac_mode, which would unconditionally turn the device
        # ON via set_mode('heating'). Instead let the first poll's
        # sw-controller evaluate the current temperature and decide.
        # For OFF and AUTO: call async_set_hvac_mode normally so the
        # device is put in the right state immediately.
        saved_mode = self._get_options().get(CONF_LAST_HVAC_MODE)
        if saved_mode and saved_mode in [m.value for m in [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]]:
            restored_mode = HVACMode(saved_mode)
            options = self._get_options()
            sw_control = options.get(CONF_SW_CONTROL_ENABLED, False)
            if restored_mode == HVACMode.HEAT and sw_control:
                # sw-controller owns the device — just restore the mode flag
                # and let the first poll decide ON or OFF based on temperature
                self._hvac_mode = HVACMode.HEAT
                self._sw_device_on = None  # force re-evaluation
                self._update_hvac_action()
                _LOGGER.debug("Boot: restored HEAT+sw-control — deferring ON/OFF to first poll")
            else:
                self._hvac_mode = restored_mode
                await self.async_set_hvac_mode(restored_mode)

    async def async_will_remove_from_hass(self):
        if self._remove_update_listener:
            self._remove_update_listener()
            self._remove_update_listener = None

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def async_update(self, *args):
        """Fetch device state, run window detection, run sw-controller."""
        options = self._get_options()
        window_open_enabled = options.get(CONF_WINDOW_OPEN_ENABLED, False)
        temp_fall_rate = options.get(CONF_TEMP_FALL_RATE, DEFAULT_TEMP_FALL_RATE)
        temp_recovery_rate = options.get(CONF_TEMP_RECOVERY_RATE, DEFAULT_TEMP_RECOVERY_RATE)
        temp_recovery_abs = options.get(CONF_TEMP_RECOVERY_ABS, DEFAULT_TEMP_RECOVERY_ABS)
        use_external_temp = options.get(CONF_USE_EXTERNAL_TEMP, False)
        temperature_entity = options.get(CONF_TEMPERATURE_ENTITY) if use_external_temp else None

        sw_control = options.get(CONF_SW_CONTROL_ENABLED, False)
        sw_min_on = float(options.get(CONF_SW_MIN_ON_SEC, DEFAULT_SW_MIN_ON_SEC))
        sw_min_off = float(options.get(CONF_SW_MIN_OFF_SEC, DEFAULT_SW_MIN_OFF_SEC))
        sw_hysteresis = float(options.get(CONF_SW_HYSTERESIS, DEFAULT_SW_HYSTERESIS))
        sw_lag_time  = float(options.get(CONF_SW_LAG_TIME,  DEFAULT_SW_LAG_TIME))
        sw_ema_alpha = float(options.get(CONF_SW_EMA_ALPHA, DEFAULT_SW_EMA_ALPHA))
        sw_i_gain    = float(options.get(CONF_SW_I_GAIN,    DEFAULT_SW_I_GAIN))

        # -- Retry gate -------------------------------------------------------
        # If the last poll failed, skip this poll unless 30s have elapsed.
        # This avoids flooding the device while it is unreachable and keeps
        # the stale state intact so the widget and sw-controller stay stable.
        now_mono = self.hass.loop.time()
        if self._comm_failed_at is not None:
            elapsed = now_mono - self._comm_failed_at
            if elapsed < self._comm_retry_interval:
                _LOGGER.debug(
                    "Tesy Convector: skipping poll — retrying in %.0fs",
                    self._comm_retry_interval - elapsed,
                )
                return
            _LOGGER.debug("Tesy Convector: retry attempt after %.0fs", elapsed)

        status = await self.convector.get_status()
        _LOGGER.debug("Tesy Convector status: %s", status)

        if (
            "payload" in status
            and "onOff" in status["payload"]
            and "status" in status["payload"]["onOff"]["payload"]
        ):
            if self._bad_response_logged:
                _LOGGER.info("Tesy Convector response structure recovered")
                self._bad_response_logged = False
            self._comm_failed_at = None  # clear retry gate on success
            if not self._attr_available:
                _LOGGER.info("Tesy Convector is back online — restoring availability")
                self._attr_available = True
                await self._apply_stored_temperature_correction()
                self.async_write_ha_state()

            # When sw-control is active the device is frequently turned off
            # by the controller itself (end of an ON phase).  The device
            # reporting onOff=off does NOT mean the user switched to OFF —
            # it means the sw-controller ended a heating cycle.  Overwriting
            # _hvac_mode from the device's onOff status in that situation
            # causes the mode guard in _run_sw_controller to bail out and
            # the controller never turns the device back on.
            #
            # Rule: only trust the device's onOff/mode status when sw-control
            # is NOT active.  When sw-control is active, _hvac_mode is owned
            # by HA and must only change through explicit user actions.
            device_on = status["payload"]["onOff"]["payload"]["status"] == "on"

            if not sw_control:
                if device_on:
                    current_mode = status["payload"]["setMode"]["payload"]["name"]
                    self._hvac_mode = HVACMode.AUTO if current_mode == "program" else HVACMode.HEAT
                else:
                    self._hvac_mode = HVACMode.OFF
            # sw-control active: _hvac_mode is already correct (set by user
            # via async_set_hvac_mode).  Do not touch it.

            # _device_is_heating: actual element activity for hvac_action.
            #
            # AUTO/program: device_on=True just means the device is powered —
            # it could be in an out-of-duty time slot with the element off.
            # The only reliable proxy is: slot setpoint > internal temp
            # (device still needs to heat to reach its current slot target).
            #
            # HEAT (no sw-control): element runs whenever device is on and
            # below setpoint — same proxy works here too.
            #
            # sw-control: _sw_device_on is used instead (per-cycle precision).
            # device_on is always True in sw-control OFF phase (setpoint=floor-1)
            # so skip the device_on check — _update_hvac_action uses _sw_device_on.
            if sw_control and self._hvac_mode == HVACMode.HEAT:
                pass
            elif not device_on:
                self._device_is_heating = False
            elif self._hvac_mode == HVACMode.AUTO or (
                self._hvac_mode == HVACMode.HEAT and not sw_control
            ):
                slot_setpoint = status["payload"]["setTemp"]["payload"]["temp"]
                internal_temp = self._extract_internal_temp(status)
                if internal_temp is not None:
                    # Element is active when internal temp is below slot target
                    self._device_is_heating = internal_temp < slot_setpoint
                else:
                    # No internal temp — fall back to device_on as best guess
                    self._device_is_heating = device_on

            # _target_temp is the HEAT-mode setpoint shown on the temperature
            # slider in all modes.  Rules:
            #
            #  • First poll (_target_temp is None): always seed from device so
            #    the widget is never blank regardless of current mode.
            #
            #  • HEAT mode, sw-control OFF: read from device every poll so any
            #    change made directly on the device panel is reflected in HA.
            #
            #  • HEAT mode, sw-control ON: keep the locally-cached precise 0.5
            #    value — device holds a safety-cap integer, not the real target.
            #
            #  • AUTO mode: keep the cached Heat setpoint — the device's
            #    reported setTemp reflects the active schedule slot which is
            #    NOT the Heat setpoint and must not overwrite the slider value.
            device_settemp = status["payload"]["setTemp"]["payload"]["temp"]
            if self._target_temp is None:
                # First poll — seed from device and persist for next boot.
                self._target_temp = device_settemp
                self._persist_setpoint(device_settemp)
            elif self._hvac_mode == HVACMode.HEAT and not sw_control:
                if self._target_temp != device_settemp:
                    self._target_temp = device_settemp
                    self._persist_setpoint(device_settemp)

            # -- External change detection (sw-control only) ------------------
            # Device is not a slave — keyboard or manufacturer app can change
            # mode and setpoint at any time. Read actual state and adapt.
            if sw_control and self._hvac_mode == HVACMode.HEAT and not self._window_opened:
                device_mode = status["payload"]["setMode"]["payload"]["name"]
                device_setpoint = status["payload"]["setTemp"]["payload"]["temp"]

                # Mode changed externally — respect it, stop sw-controller.
                if not device_on:
                    _LOGGER.info(
                        "SW ctrl: device turned OFF externally — stopping controller"
                    )
                    self._hvac_mode = HVACMode.OFF
                    self._sw_device_on = None
                    self._update_hvac_action()
                    self.async_write_ha_state()
                elif device_mode == "program":
                    _LOGGER.info(
                        "SW ctrl: device switched to AUTO externally — stopping controller"
                    )
                    self._hvac_mode = HVACMode.AUTO
                    self._sw_device_on = None
                    self._update_hvac_action()
                    self.async_write_ha_state()
                else:
                    # Setpoint changed externally — update target and let
                    # sw-controller adapt on next cycle using new value.
                    # Ignore sw-controller's own setpoint writes (ceil / floor-1).
                    if self._target_temp is not None:
                        sw_on_sp = math.ceil(self._target_temp)
                        sw_off_sp = math.floor(self._target_temp) - 1
                        is_sw_setpoint = device_setpoint in (sw_on_sp, sw_off_sp)
                    else:
                        is_sw_setpoint = False
                    if not is_sw_setpoint and device_setpoint != self._target_temp:
                        _LOGGER.info(
                            "SW ctrl: setpoint changed externally %s -> %s — adapting",
                            self._target_temp, device_setpoint,
                        )
                        self._target_temp = float(device_setpoint)
                        self._persist_setpoint(float(device_setpoint))
                        self._sw_device_on = None  # re-evaluate on next poll
                        self.async_write_ha_state()

            await self._handle_window_detection(
                window_open_enabled=window_open_enabled,
                temp_fall_rate=temp_fall_rate,
                temp_recovery_rate=temp_recovery_rate,
                temp_recovery_abs=temp_recovery_abs,
                use_external_temp=use_external_temp,
                temperature_entity=temperature_entity,
                status=status,
            )

            # v0.3: run sw-controller after temperature is updated,
            # suspended while window is open (device already forced OFF)
            if sw_control and not self._window_opened:
                await self._run_sw_controller(
                    temperature_entity=temperature_entity,
                    min_on_sec=sw_min_on,
                    min_off_sec=sw_min_off,
                    hysteresis=sw_hysteresis,
                    lag_time_min=sw_lag_time,
                    i_gain=sw_i_gain,
                    ema_alpha=sw_ema_alpha,
                )
            self._update_hvac_action()
        else:
            if "error" in status:
                # Transient comm failure — record time for retry gate.
                if self._comm_failed_at is None:
                    self._comm_failed_at = now_mono
                    _LOGGER.warning(
                        "Tesy Convector unreachable — marking unavailable, "
                        "retrying in %.0fs: %s",
                        self._comm_retry_interval, status.get("error"),
                    )
                    # Mark entity unavailable — blocks automations and commands.
                    self._attr_available = False
                    # Unknown device state — reset sw-controller.
                    self._sw_device_on = None
                    self._update_hvac_action()
                    self.async_write_ha_state()
                else:
                    # Ongoing outage — update timestamp for next 30s window
                    self._comm_failed_at = now_mono
            elif not self._bad_response_logged:
                _LOGGER.error(
                    "Unexpected response structure from Tesy Convector: %s", status
                )
                self._bad_response_logged = True
            # Do NOT reset _hvac_mode or _target_temp on failure.
            # Stale values are more useful than forcing OFF/None which
            # would disrupt the sw-controller and confuse the widget.

    # ------------------------------------------------------------------
    # v0.3 — Software on/off controller
    # ------------------------------------------------------------------

    async def _run_sw_controller(
        self,
        temperature_entity: str | None,
        min_on_sec: float,
        min_off_sec: float,
        hysteresis: float,
        lag_time_min: float,
        i_gain: float,
        ema_alpha: float = 0.3,
    ) -> None:
        """Bang-bang controller with predictive cutoff and integral drift correction.

        Core thresholds
        ---------------
          lower = setpoint - hysteresis     -> turn ON  at or below this
          upper = setpoint                    -> turn OFF at or above this (= setpoint)

        Predictive early-OFF (during ON phase)
        ---------------------------------------
        Measures the current rising ramp rate (deg C/min) from recent history
        and projects forward by lag_time_min minutes. If the predicted temp
        will exceed upper, the heater turns off early to compensate for
        thermal lag between the heater and the distant sensor.

          predicted = current + ramp_rate x lag_time_min
          if predicted >= upper  ->  turn OFF early

        Integral drift correction (I-term)
        -----------------------------------
        A slow steady overshoot (e.g. 0.1 deg C / 10 min while device is OFF)
        is caused by heat diffusing from warm surfaces to the sensor long after
        the heater stops. The ramp during OFF is nearly flat so the predictive
        cutoff cannot help.

        The I-term tracks the average temperature error over the last
        SW_I_WINDOW_SEC (30 min) and shifts both thresholds down by
        (avg_error x i_gain), capped at +/- hysteresis to prevent runaway.

          avg_error    = mean(readings over window) - setpoint
          i_correction = avg_error x i_gain   [capped at +/-hysteresis]
          effective_upper = upper - i_correction

        The lower threshold is intentionally NOT shifted. Undershooting
        costs nothing (room is just slightly cool for a few minutes).
        Overshooting costs money (paid for heat above the setpoint).
        Asymmetric correction maximises efficiency.

        With a 0.6 deg C systematic overshoot and i_gain=0.5:
          i_correction = 0.3 deg C
          effective_upper shifts 0.3 deg C lower
          heater turns OFF earlier, average temp converges to setpoint.

        i_gain=0  -> integral correction disabled.
        i_gain=1  -> full average error applied as correction.
        """
        if self._hvac_mode not in (HVACMode.HEAT,):
            return

        if self._target_temp is None:
            return

        current_temp = self._get_external_temp(temperature_entity, ema_alpha=ema_alpha)
        if current_temp is None:
            _LOGGER.debug("SW controller: no current temperature -- skipping")
            return

        now = self.hass.loop.time()

        # -- History maintenance -----------------------------------------------
        # Short history: clears on ON/OFF transitions -- used for ramp rate only
        self._sw_temp_history.append((now, current_temp))
        self._sw_temp_history = [
            (t, v) for t, v in self._sw_temp_history if now - t <= 600
        ]
        # Long history: never cleared on transitions -- used for integral average
        self._sw_long_history.append((now, current_temp))
        self._sw_long_history = [
            (t, v) for t, v in self._sw_long_history if now - t <= SW_I_WINDOW_SEC
        ]

        setpoint = self._target_temp
        # Dead-band is entirely below setpoint.
        # Setpoint is the upper boundary — never intentionally heat above it.
        # hysteresis = how far below setpoint the room must drop before firing.
        #
        #   lower = setpoint - hysteresis   -> ON  at or below this
        #   upper = setpoint                -> OFF at or above this
        #
        # Example: setpoint=22.0, hysteresis=0.2
        #   ON  at or below 21.8 C
        #   OFF at or above 22.0 C
        upper = setpoint
        lower = setpoint - hysteresis

        # -- Integral correction -----------------------------------------------
        i_correction = 0.0
        if i_gain > 0 and len(self._sw_long_history) >= 6:
            # Need at least 6 readings (~1 min) before trusting the average
            avg_temp = sum(v for _, v in self._sw_long_history) / len(self._sw_long_history)
            avg_error = avg_temp - setpoint   # positive = running hot
            raw_correction = avg_error * i_gain
            # Cap at +/-hysteresis to prevent runaway
            i_correction = max(-hysteresis, min(hysteresis, raw_correction))
            if abs(i_correction) > 0.01:
                _LOGGER.debug(
                    "SW ctrl I-term: avg=%.2f C  error=%.3f C  correction=%.3f C  "
                    "(gain=%.1f  n=%d readings)",
                    avg_temp, avg_error, i_correction, i_gain, len(self._sw_long_history),
                )

        # Asymmetric: only upper shifts. Lower is left at nominal.
        # Undershoot costs nothing; overshoot costs money.
        effective_upper = upper - i_correction
        # effective_lower is just lower — no shift needed

        # -- Ramp rate (predictive early-OFF while ON) -------------------------
        ramp_rate = 0.0
        if self._sw_device_on:
            # Use a short min_span_sec (20 s = 2 polls) for the sw-controller
            # ramp estimator. _sw_temp_history is cleared on every ON/OFF
            # transition so it is always clean — no risk of stale boot noise.
            # The default 60 s used by window detection is too long here and
            # makes the predictive cutoff blind for the first 6 polls of every
            # ON phase, exactly when the ramp matters most.
            ramp_rate = self.estimate_temp_trend(self._sw_temp_history, min_span_sec=20.0) or 0.0
            ramp_rate = max(0.0, ramp_rate)   # only rising ramp is relevant

        # -- Desired state -----------------------------------------------------
        #
        # Upper cutoff logic (while ON):
        #   1. current_temp >= setpoint          — at or above target: always OFF
        #                                           hysteresis does NOT apply here;
        #                                           heating above setpoint costs money
        #   2. predicted_temp >= effective_upper — predictive early-OFF for lag
        #   3. current_temp >= effective_upper   — standard threshold (fallback)
        #
        # Lower cutoff logic (while OFF):
        #   current_temp <= lower                — full hysteresis dead-band applies;
        #                                           undershoot costs nothing
        if self._sw_device_on is None:
            # First evaluation after boot/mode-switch — use the same lower
            # threshold as the normal OFF→ON transition so we don't fire an
            # unnecessary ON pulse when temperature is already within the
            # dead-band (e.g. 21.8°C with setpoint 22.0°C, hysteresis 0.2°C
            # → lower=21.9°C → correctly stays OFF).
            desired_on = current_temp <= lower

        elif self._sw_device_on:
            # Rule 1: at or above effective_upper (setpoint - i_correction)
            at_upper = current_temp >= effective_upper
            # Rule 2: predictive — will coast reach effective_upper within lag window?
            predicted_temp = current_temp + ramp_rate * lag_time_min
            predicted_overshoot = predicted_temp >= effective_upper

            if at_upper or predicted_overshoot:
                desired_on = False
                if predicted_overshoot and not at_upper:
                    _LOGGER.debug(
                        "SW ctrl: predictive early-OFF -- "
                        "current=%.2f C ramp=%.3f C/min lag=%.1fmin "
                        "-> predicted=%.2f C >= eff_upper=%.2f C (i_corr=%.3f C)",
                        current_temp, ramp_rate, lag_time_min,
                        predicted_temp, effective_upper, i_correction,
                    )
                else:
                    _LOGGER.debug(
                        "SW ctrl: OFF at upper=%.2f C -- current=%.2f C  i_corr=%.3f C",
                        effective_upper, current_temp, i_correction,
                    )
            else:
                desired_on = True

        else:
            desired_on = current_temp <= lower

        # -- Minimum phase guards ----------------------------------------------
        if desired_on and not self._sw_device_on:
            if self._sw_last_off_time is not None:
                elapsed = now - self._sw_last_off_time
                if elapsed < min_off_sec:
                    _LOGGER.debug(
                        "SW ctrl: ON suppressed -- min OFF not met (%.0f/%.0f s)",
                        elapsed, min_off_sec,
                    )
                    return

        if not desired_on and self._sw_device_on:
            if self._sw_last_on_time is not None:
                elapsed = now - self._sw_last_on_time
                if elapsed < min_on_sec:
                    _LOGGER.debug(
                        "SW ctrl: OFF suppressed -- min ON not met (%.0f/%.0f s)",
                        elapsed, min_on_sec,
                    )
                    return

        # -- Transitions -------------------------------------------------------
        # Handle None→OFF: first poll after mode switch with temp above setpoint.
        # Neither normal branch fires (None is falsy for ON, not truthy for OFF).
        # Explicitly set floor-1 so device display matches expected idle state.
        if not desired_on and self._sw_device_on is None:
            sw_off_setpoint = math.floor(setpoint) - 1
            _LOGGER.debug(
                "SW ctrl: init idle — temp %.2f C above setpoint %.1f C, "
                "setting floor setpoint %d C",
                current_temp, setpoint, sw_off_setpoint,
            )
            await self.convector.set_temperature(sw_off_setpoint)
            self._sw_device_on = False
            self._update_hvac_action()
            self.async_write_ha_state()
            return

        if desired_on and not self._sw_device_on:
            _LOGGER.info(
                "SW ctrl ON:  %.2f C <= %.2f C eff_lower  (set=%.1f C  i_corr=%.3f C)",
                current_temp, lower, setpoint, i_correction,
            )
            # Safety setpoint must be strictly above current_temp so the
            # device firmware fires immediately (device heats when temp < setpoint).
            # With setpoint=floor-1 in the OFF phase, ceil(setpoint) alone can
            # equal current_temp — device won't heat. Use max(ceil, ceil(current)+1)
            # to guarantee heating fires while keeping safety cap reasonable.
            # Device is already in heating mode (set once in async_set_hvac_mode).
            # Just raise setpoint above current_temp — firmware heats immediately.
            safety_setpoint = math.ceil(setpoint)
            await self.convector.set_temperature(safety_setpoint)
            await asyncio.sleep(0.1)
            self._sw_device_on = True
            self._sw_last_on_time = now
            self._sw_temp_history = [(now, current_temp)]
            # Complete the last cycle by filling in OFF duration
            if self._sw_duty_cycles and self._sw_last_off_time is not None:
                on_dur, _ = self._sw_duty_cycles[-1]
                off_dur = now - self._sw_last_off_time
                self._sw_duty_cycles[-1] = (on_dur, off_dur)
                # Recompute average duty over complete cycles
                complete = [(on, off) for on, off in self._sw_duty_cycles if off > 0]
                if complete:
                    self._sw_duty_pct = round(
                        100 * sum(on / (on + off) for on, off in complete) / len(complete)
                    )
            self._update_hvac_action()
            self.async_write_ha_state()  # immediate widget update

        elif not desired_on and self._sw_device_on:
            predicted_temp = current_temp + ramp_rate * lag_time_min
            _LOGGER.info(
                "SW ctrl OFF: %.2f C  ramp=%.3f C/min  predicted=%.2f C  "
                "eff_upper=%.2f C  i_corr=%.3f C  (set=%.1f C)",
                current_temp, ramp_rate, predicted_temp,
                effective_upper, i_correction, setpoint,
            )
            # set_temperature(floor-1) instead of turn_off() —
            # onOff=off resets the device TCP stack ('Connection reset by peer').
            # floor(setpoint)-1 keeps device on but below any useful setpoint.
            sw_off_setpoint = math.floor(setpoint) - 1
            await self.convector.set_temperature(sw_off_setpoint)
            await asyncio.sleep(0.1)
            self._sw_device_on = False
            self._sw_last_off_time = now
            self._sw_peak_temp = current_temp   # start tracking coast peak from now
            self._sw_temp_history = []
            # Store ON duration — OFF duration added when next ON fires
            if self._sw_last_on_time is not None:
                on_dur = now - self._sw_last_on_time
                self._sw_duty_cycles.append((on_dur, 0.0))  # off_dur filled later
                self._sw_duty_cycles = self._sw_duty_cycles[-5:]  # keep last 5
            self._update_hvac_action()
            self.async_write_ha_state()  # immediate widget update

    async def _restore_firmware_control(self) -> None:
        """Hand temperature control back to device firmware.

        Called when the external sensor becomes unavailable while sw-control
        is active.  Sends the user's desired setpoint (as a rounded integer)
        back to the device so the built-in thermostat takes over cleanly.
        """
        _LOGGER.warning(
            "SW controller: external sensor lost — restoring device firmware control "
            "with setpoint %s°C",
            self._target_temp,
        )
        if self._target_temp is not None:
            # Floor: better to be 0.5°C under target than over when falling back
            device_setpoint = int(self._target_temp)  # floor for positive temps
            await self.convector.set_temperature(device_setpoint)
            await asyncio.sleep(0.1)
        # Ensure device is ON in heating mode so firmware thermostat can operate
        if self._hvac_mode == HVACMode.HEAT:
            await self.convector.set_mode("heating")
            await asyncio.sleep(0.1)
        # Reset sw-controller state — it must re-evaluate when sensor returns
        self._sw_device_on = None

    def _get_external_temp(
        self,
        temperature_entity: str | None,
        ema_alpha: float = DEFAULT_SW_EMA_ALPHA,
    ) -> float | None:
        """Return EMA-filtered external temperature.

        filtered = alpha * raw + (1 - alpha) * previous
        alpha=1.0 → raw passthrough.  alpha=0.2 → heavy smoothing.
        First valid reading seeds the EMA with no initial lag.
        Shares _ema_temp with window detection so all consumers
        see the same noise-free signal.
        """
        if not temperature_entity:
            return None
        temp_state = self.hass.states.get(temperature_entity)
        if temp_state is None or temp_state.state in ("unavailable", "unknown"):
            return None
        try:
            raw = float(temp_state.state)
            if not -40.0 <= raw <= 80.0:
                return None
        except ValueError:
            return None

        if self._ema_temp is None:
            self._ema_temp = raw  # seed — no lag on first reading
        else:
            self._ema_temp = ema_alpha * raw + (1.0 - ema_alpha) * self._ema_temp
        return self._ema_temp

    # ------------------------------------------------------------------
    # Window detection helpers
    # ------------------------------------------------------------------

    def estimate_temp_trend(self, history: list[tuple[float, float]], min_span_sec: float = 60.0) -> float | None:
        """Estimate temperature trend (°C/min) via linear regression on recent 120s.

        Returns None until at least min_span_sec of history has accumulated so
        that boot-time sensor jitter over just 1-2 readings cannot trigger a
        false window-open detection.
        """
        if len(history) < 3:
            return None
        now = history[-1][0]
        # Require a minimum time span across the available history
        history_span = now - history[0][0]
        if history_span < min_span_sec:
            return None
        recent = [(t, temp) for t, temp in history if now - t <= 120]
        if len(recent) < 3:
            return None

        t0 = recent[0][0]
        times = [t - t0 for t, _ in recent]
        temps = [temp for _, temp in recent]

        avg_t = mean(times)
        avg_temp = mean(temps)

        numerator = sum(
            (t - avg_t) * (temp - avg_temp)
            for t, temp in zip(times, temps, strict=True)
        )
        denominator = sum((t - avg_t) ** 2 for t in times)
        if denominator == 0:
            return 0.0
        return (numerator / denominator) * 60

    async def _handle_window_detection(
        self,
        window_open_enabled: bool,
        temp_fall_rate: float,
        temp_recovery_rate: float,
        temp_recovery_abs: float,
        use_external_temp: bool,
        temperature_entity: str | None,
        status: dict,
        ema_alpha: float = DEFAULT_SW_EMA_ALPHA,
    ) -> None:
        """Update current temperature and run window open/close detection."""
        if self._manual_window_override:
            _LOGGER.debug("Window detection skipped: manual override active")
            self._update_current_temp_from_entity_or_status(
                temperature_entity, use_external_temp, status
            )
            return

        now = self.hass.loop.time()

        if temperature_entity and use_external_temp:
            temp_state = self.hass.states.get(temperature_entity)
            temp = None

            if temp_state and temp_state.state not in ("unavailable", "unknown"):
                try:
                    temp_val = float(temp_state.state)
                    if -40.0 <= temp_val <= 80.0:
                        # Apply EMA — same filter the sw-controller uses
                        if self._ema_temp is None:
                            self._ema_temp = temp_val
                        else:
                            self._ema_temp = ema_alpha * temp_val + (1.0 - ema_alpha) * self._ema_temp
                        temp = self._ema_temp
                except ValueError:
                    _LOGGER.warning(
                        "Invalid temperature from %s: %s",
                        temperature_entity,
                        temp_state.state,
                    )
                    if self._sw_device_on is not None:
                        await self._restore_firmware_control()
                    if self._window_opened:
                        await self._close_window(now)
                    self._current_temp = self._extract_internal_temp(status)
                    self._temp_history = []
                    return

            # --- External sensor unavailable or out-of-range ---
            if temp is None:
                if not self._ext_sensor_unavailable_logged:
                    _LOGGER.warning(
                        "External temperature entity '%s' is unavailable or invalid — "
                        "falling back to device firmware control.",
                        temperature_entity,
                    )
                    self._ext_sensor_unavailable_logged = True

                # Restore device firmware control if sw-controller was active
                if self._sw_device_on is not None:
                    await self._restore_firmware_control()

                # Close any open window so device is not stuck OFF
                if self._window_opened:
                    await self._close_window(now)

                # Reset EMA so it re-seeds cleanly when sensor returns
                self._ema_temp = None
                # Show internal sensor temp if available
                self._current_temp = self._extract_internal_temp(status)
                self._temp_history = []
                self._ema_temp = None  # reset EMA — stale value discarded
                return

            # Sensor back online — clear the warning flag
            if self._ext_sensor_unavailable_logged:
                _LOGGER.info(
                    "External temperature entity '%s' is available again.",
                    temperature_entity,
                )
                self._ext_sensor_unavailable_logged = False

            # _ema_temp was already updated in the temp_val block above.
            # Use it as the filtered current temperature.
            filtered_temp = self._ema_temp if self._ema_temp is not None else temp
            self._current_temp = filtered_temp

            if temp is not None and window_open_enabled:
                self._temp_history.append((now, filtered_temp))
                self._temp_history = [x for x in self._temp_history if now - x[0] <= 180]

                trend = self.estimate_temp_trend(self._temp_history)

                # Mark detection as ready once we have a valid trend estimate
                # (i.e. enough history has accumulated since boot/restart).
                if trend is not None and not self._window_detection_ready:
                    self._window_detection_ready = True
                    _LOGGER.info(
                        "Window detection: sufficient history accumulated — detection active"
                    )

                if trend is None or not self._window_detection_ready:
                    return

                # Check re-detection inhibit — user may have dismissed the
                # window event while temperature was still falling.
                if (
                    self._window_inhibit_until is not None
                    and now < self._window_inhibit_until
                ):
                    remaining = self._window_inhibit_until - now
                    _LOGGER.debug(
                        "Window detection inhibited — %.0fs remaining", remaining
                    )
                    return

                if not self._window_opened and trend < -temp_fall_rate:
                    self._window_opened = True
                    self._prev_hvac_mode = self._hvac_mode
                    self._window_change_time = now
                    self._min_temp_during_open = temp
                    self._temp_history = []
                    self._window_detection_ready = False
                    await self.convector.set_opened_window("on")
                    await self.async_set_hvac_mode(HVACMode.OFF, _from_window_detection=True)
                    _LOGGER.info("Window open detected. Rate = %.2f °C/min", trend)

                elif self._window_opened:
                    if self._window_change_time is None:
                        self._window_change_time = now
                    if self._min_temp_during_open is None or temp < self._min_temp_during_open:
                        self._min_temp_during_open = temp

                    time_open = now - self._window_change_time
                    dtemp = (
                        temp - self._min_temp_during_open
                        if self._min_temp_during_open is not None
                        else 0
                    )
                    recovery_ok = (
                        time_open > WINDOW_RECOVERY_HYSTERESIS_SEC
                        and (trend > temp_recovery_rate or dtemp > temp_recovery_abs)
                    )
                    timeout = time_open > WINDOW_OPEN_TIMEOUT_SEC

                    if recovery_ok or timeout:
                        if timeout:
                            _LOGGER.warning("Window open timeout — restoring HVAC mode")
                        else:
                            _LOGGER.info(
                                "Window closed. Recovery rate=%.2f °C/min, delta=%.2f °C",
                                trend, dtemp,
                            )
                        await self._close_window(now)
        else:
            self._current_temp = self._extract_internal_temp(status)

    def _extract_internal_temp(self, status: dict) -> float | None:
        """Extract current temperature from device status payload (tempAir key)."""
        try:
            return status["payload"]["tempAir"]["payload"]["temp"]
        except (KeyError, TypeError):
            return None

    def _update_current_temp_from_entity_or_status(
        self,
        temperature_entity: str | None,
        use_external_temp: bool,
        status: dict,
    ) -> None:
        if temperature_entity and use_external_temp:
            temp_state = self.hass.states.get(temperature_entity)
            if temp_state and temp_state.state not in ("unavailable", "unknown"):
                try:
                    self._current_temp = float(temp_state.state)
                    return
                except ValueError:
                    pass
        self._current_temp = self._extract_internal_temp(status)

    async def _close_window(self, now: float, user_initiated: bool = False) -> None:
        """Close window state, restore HVAC mode, reset detection state.

        user_initiated=True when the user explicitly sets HEAT/AUTO while window
        is open. Sets a re-detection inhibit period so the still-falling temperature
        doesn't immediately re-trigger the same window-open event.
        """
        self._window_opened = False
        self._window_change_time = None
        self._min_temp_during_open = None
        self._temp_history = []
        self._manual_window_override = False
        # Must reset _window_detection_ready so detection waits for fresh
        # history to accumulate before it can fire again. Without this,
        # _ready=True with an empty history causes noisy trend estimates
        # that can immediately re-trigger a false window-open detection.
        self._window_detection_ready = False
        if user_initiated:
            # User explicitly dismissed the window event — suppress re-detection
            # for inhibit period so still-falling temp doesn't retrigger.
            self._window_inhibit_until = now + self._window_inhibit_sec
            _LOGGER.debug(
                "Window detection inhibited for %.0fs (user dismissed)",
                self._window_inhibit_sec,
            )
        else:
            self._window_inhibit_until = None

        await self.convector.set_opened_window("off")
        if self._prev_hvac_mode and self._prev_hvac_mode != HVACMode.OFF:
            await self.async_set_hvac_mode(self._prev_hvac_mode, _from_window_detection=True)
            self._prev_hvac_mode = None

        # Reset sw-controller so it re-evaluates cleanly after window closes
        self._sw_device_on = None
        self._update_hvac_action()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def hvac_mode(self):
        return self._hvac_mode

    @property
    def current_temperature(self):
        return self._current_temp

    @property
    def target_temperature(self):
        return self._target_temp

    def _update_hvac_action(self) -> None:
        """Recompute and store _attr_hvac_action at every state change.

        Called explicitly rather than computed lazily in a property so the
        HA entity state always reflects the correct action immediately.
        Only valid HVACAction enum values are used so the frontend renders
        its own translated strings rather than raw arbitrary text.
        """
        if self._window_opened:
            self._attr_hvac_action = HVACAction.OFF
            return
        if self._hvac_mode == HVACMode.OFF:
            self._attr_hvac_action = HVACAction.OFF
            return

        options = self._get_options()
        sw_control = options.get(CONF_SW_CONTROL_ENABLED, False)

        if self._hvac_mode == HVACMode.HEAT and sw_control:
            # Append duty cycle % to action string when available
            duty = self._sw_duty_pct
            if duty is not None:
                self._attr_hvac_action = (  # type: ignore[assignment]
                    f"heating ({duty}%)" if self._sw_device_on is True
                    else f"idle ({duty}%)"
                )
            else:
                self._attr_hvac_action = (
                    HVACAction.HEATING if self._sw_device_on is True else HVACAction.IDLE
                )
            return

        if self._hvac_mode == HVACMode.AUTO:
            # HVACAction has no AUTO value. IDLE is the honest choice —
            # the mode selector already shows "Auto" so the user knows
            # the device is running its own schedule.
            self._attr_hvac_action = HVACAction.IDLE
            return

        # Plain HEAT (no sw-control) — use element-activity flag from last poll
        self._attr_hvac_action = (
            HVACAction.HEATING if self._device_is_heating else HVACAction.IDLE
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Expose duty cycle and controller diagnostics as entity attributes."""
        attrs: dict = {}
        options = self._get_options()
        if not options.get(CONF_SW_CONTROL_ENABLED, False):
            return attrs
        if self._sw_duty_pct is not None:
            attrs["duty_cycle_pct"] = self._sw_duty_pct
        if self._sw_duty_cycles:
            complete = [(on, off) for on, off in self._sw_duty_cycles if off > 0]
            attrs["duty_cycles_sampled"] = len(complete)
        return attrs

    @property
    def hvac_action(self) -> HVACAction:
        """Return the current HVAC action (stored in _attr_hvac_action)."""
        return self._attr_hvac_action

    @property
    def is_window_opened(self) -> bool:
        return self._window_opened

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode, _from_window_detection: bool = False):
        """Set HVAC mode and persist for reboot recovery.

        _from_window_detection=True marks internal calls made by the window
        detection logic (open → force OFF, close → restore mode).  User-
        initiated calls always have _from_window_detection=False.

        User setting HEAT or AUTO while window is open = explicit intent to
        heat now.  Clear window state immediately and restore normal operation.
        Setting OFF while window is open is left unchanged — window detection
        stays active.
        """
        # User explicitly requests heating while window is open — clear window.
        # Set _prev_hvac_mode=None first so _close_window skips its own
        # async_set_hvac_mode restore call (we are already setting the mode).
        if (
            self._window_opened
            and not _from_window_detection
            and hvac_mode in (HVACMode.HEAT, HVACMode.AUTO)
        ):
            _LOGGER.info(
                "Window open — user activated %s — clearing window state", hvac_mode
            )
            self._prev_hvac_mode = None  # prevent _close_window from re-restoring
            now = self.hass.loop.time()
            await self._close_window(now, user_initiated=True)
            # _close_window already cleared window flags and sent set_opened_window(off).
            # Fall through to set the requested mode normally below.

        if hvac_mode == HVACMode.HEAT:
            options = self._get_options()
            sw_control = options.get(CONF_SW_CONTROL_ENABLED, False)
            if sw_control:
                # Set device to heating mode — sw-controller plays the setpoint
                # up/down to control the element, so the device must stay in
                # heating mode permanently. set_device_on=None forces the
                # controller to re-evaluate temperature on next poll.
                await self.convector.set_mode("heating")
                self._sw_device_on = None  # force re-evaluation on next poll
            else:
                # Plain HEAT — device firmware thermostat controls the element.
                await self.convector.set_mode("heating")
                # Restore real setpoint if sw-control left a safety-cap value.
                if self._sw_device_on is not None and self._target_temp is not None:
                    await self.convector.set_temperature(int(self._target_temp))
                    await asyncio.sleep(0.1)
                self._sw_device_on = None
        elif hvac_mode == HVACMode.OFF:
            await self.convector.turn_off()
            self._sw_device_on = None
        elif hvac_mode == HVACMode.AUTO:
            # Switching to AUTO — device manages its own schedule.
            self._sw_device_on = None
            await self.convector.set_mode("program")
        await asyncio.sleep(0.1)

        # If window is still open (user set OFF while window open) track the
        # mode so _close_window restores it when temperature recovers.
        if self._window_opened and not _from_window_detection:
            self._prev_hvac_mode = hvac_mode
            _LOGGER.debug(
                "Window open — user set mode to %s; will restore on window close",
                hvac_mode,
            )

        # Persist for reboot recovery. Skip only the internal window-open
        # forced-OFF call so we don't overwrite the user's intended mode.
        if not _from_window_detection or hvac_mode != HVACMode.OFF:
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                options={**self._config_entry.options, CONF_LAST_HVAC_MODE: hvac_mode.value},
            )
        self._hvac_mode = hvac_mode
        self._update_hvac_action()
        self.async_write_ha_state()  # push to widget immediately

    async def async_set_temperature(self, **kwargs):
        """Set target temperature — behaviour depends on mode and sw-control.

        AUTO mode (sw-control on or off):
            The temperature slider sets the HEAT mode setpoint, stored locally
            in HA.  The device's Auto schedule is NOT touched.  The value is
            ready immediately when the user switches back to Heat.

        HEAT mode, sw-control OFF:
            Setpoint is floored to integer and sent to the device thermostat.
            Floor chosen so the device never overshoots (20.5 -> 20).

        HEAT mode, sw-control ON:
            Precise 0.5-step value stored locally.  The device receives only a
            safe-cap integer so its own thermostat never interferes.  The
            sw-controller loop drives on/off against the external sensor.

        Device offset correction is integer-only (-4..+4); fractional offsets
        via correction are not possible — sw-control provides 0.5 precision.
        """
        temp = kwargs.get("temperature")
        if temp is None:
            return

        # Snap to nearest 0.5 °C
        temp = round(temp * 2) / 2

        options = self._get_options()
        sw_control = options.get(CONF_SW_CONTROL_ENABLED, False)

        # ── AUTO mode ────────────────────────────────────────────────────────
        # Slider sets the Heat-mode setpoint only.  Device schedule untouched.
        if self._hvac_mode == HVACMode.AUTO:
            if self._target_temp != temp:
                self._target_temp = temp
                self._persist_setpoint(temp)
                _LOGGER.debug(
                    "AUTO: Heat setpoint pre-loaded to %.1f°C (schedule untouched)", temp
                )
            return

        # ── HEAT mode, sw-control ON ─────────────────────────────────────────
        if sw_control:
            self._target_temp = temp
            self._persist_setpoint(temp)
            self._sw_device_on = None  # force re-evaluation on next poll cycle
            # Safety setpoint: ceil(target) so the device thermostat cuts off
            # just above the real target if HA ever loses control.
            safety_setpoint = math.ceil(temp)   # e.g. 20.5→21, 20.0→20
            _LOGGER.debug(
                "SW ctrl: setpoint %.1f°C -> device setTemp=%d (safety backstop)",
                temp, safety_setpoint,
            )
            await self.convector.set_temperature(safety_setpoint)
            await asyncio.sleep(0.1)
            return

        # ── HEAT mode, sw-control OFF ────────────────────────────────────────
        # Floor to integer — slightly cool is better than overshoot.
        device_setpoint = int(temp)  # floor: 20.5 -> 20, 21.0 -> 21
        if self._target_temp != temp:
            _LOGGER.debug(
                "HEAT: setpoint %.1f°C -> device setTemp=%d (floor)", temp, device_setpoint
            )
            await self.convector.set_temperature(device_setpoint)
            self._target_temp = temp
            self._persist_setpoint(temp)
            await asyncio.sleep(0.1)


    async def async_set_opened_window(self, status):
        await self.convector.set_opened_window(status)

    async def async_handle_manual_window_status(self, status: str) -> None:
        now = self.hass.loop.time()

        if status == "on":
            if self._window_opened:
                _LOGGER.debug("Window already open (manual override)")
                return
            self._manual_window_override = True
            self._window_change_time = now
            self._prev_hvac_mode = self.hvac_mode
            self._min_temp_during_open = self.current_temperature
            await self.convector.set_opened_window("on")
            await self.async_set_hvac_mode(HVACMode.OFF, _from_window_detection=True)
            self._window_opened = True
            _LOGGER.info("Manual window open triggered via service")

        elif status == "off":
            if not self._window_opened:
                _LOGGER.debug("Window already closed (manual override)")
                return
            await self._close_window(now)
            _LOGGER.info("Manual window close triggered via service")
