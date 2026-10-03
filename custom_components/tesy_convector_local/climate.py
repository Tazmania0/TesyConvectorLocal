"""Climate platform with adaptive external-temperature heating control."""

import asyncio
import math
from datetime import datetime, timedelta, timezone
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
from homeassistant.helpers.storage import Store

from .const import (
    CLOUD_TEMPERATURE_STEP,
    CONF_CLOUD_TELEMETRY_ENABLED,
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
    EXTERNAL_TEMP_MAX_AGE_SEC,
    WINDOW_OPEN_TIMEOUT_SEC,
    WINDOW_RECOVERY_HYSTERESIS_SEC,
)
from .tesy_convector import TesyConvector

_LOGGER = logging.getLogger(__name__)

SET_OPENED_WINDOW_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): vol.Any(cv.entity_id, [cv.entity_id]),
        vol.Required("status"): vol.In(["on", "off"]),
    }
)


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set up the Tesy Convector climate entity."""
    convector = hass.data[DOMAIN][config_entry.entry_id]["device"]
    entity = TesyConvectorClimate(convector, config_entry)
    hass.data[DOMAIN][config_entry.entry_id]["climate_entity"] = entity
    async_add_entities([entity])

    if not hass.services.has_service(DOMAIN, "set_opened_window"):
        schema, handler = _create_window_service_handler(hass)
        hass.services.async_register(DOMAIN, "set_opened_window", handler, schema=schema)


def _create_window_service_handler(hass: HomeAssistant):
    async def handle(call):
        entity_ids = call.data["entity_id"]
        requested = {entity_ids} if isinstance(entity_ids, str) else set(entity_ids)
        status = call.data["status"]
        matched = set()
        # Resolve current entries at call time, including later additions or
        # reloads. Always use the climate handler to preserve window safety.
        for entry_data in list(hass.data.get(DOMAIN, {}).values()):
            entity = entry_data.get("climate_entity")
            if entity is not None and entity.entity_id in requested:
                await entity.async_handle_manual_window_status(status)
                matched.add(entity.entity_id)
        if missing := requested - matched:
            _LOGGER.warning(
                "set_opened_window: no Tesy climate entity matched %s", sorted(missing)
            )

    return SET_OPENED_WINDOW_SCHEMA, handle


class TesyConvectorClimate(ClimateEntity):
    """Representation of a Tesy Convector as a ClimateEntity."""

    # Keep the normal climate history, but omit live diagnostic payloads.
    # Controller learning is persisted separately through Store.
    _unrecorded_attributes = frozenset({
        "sw_control_state", "external_temp_valid", "external_temp_age_sec",
        "filtered_external_temp", "raw_external_temp", "internal_temp",
        "external_temperature_error", "sw_heat_requested", "firmware_limit_estimated",
        "firmware_fallback_active", "overshoot_correction", "i_correction",
        "ramp_rate_c_per_min", "predicted_temp", "effective_off_threshold",
        "duty_cycle_pct", "duty_cycles_sampled",
        "cloud_heating", "cloud_current_temp", "cloud_telemetry_connected",
        "sw_firmware_headroom", "sw_firmware_setpoint", "sw_observed_coast_minutes",
        "sw_cloud_internal_rise_rate", "sw_firmware_cutoff_temperature", "sw_firmware_cutoff_eta_sec",
        "sw_internal_coast_rise", "sw_internal_coast_seconds",
        "cloud_telemetry_age_sec",
    })

    def __init__(self, convector: TesyConvector, config_entry) -> None:
        super().__init__()
        self._device = convector
        self.convector = convector
        self._config_entry = config_entry
        self._cloud_telemetry = None
        self._remove_update_listener = None

        self._attr_name = f"Tesy Convector {convector.model} ({convector.ip_address})"
        self._attr_icon = "mdi:radiator"
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
        self._external_temp_age_sec: float | None = None
        self._external_temp_valid = False
        self._external_raw_temp: float | None = None
        self._internal_temp: float | None = None
        self._sw_firmware_limit_estimated: bool | None = None
        self._external_temp_reason = "missing sensor"
        self._external_sensor_entity: str | None = None
        self._sw_fallback_active = False
        self._sw_was_enabled = False
        self._sw_ramp_rate: float | None = None
        self._sw_predicted_temp: float | None = None
        self._sw_effective_off_threshold: float | None = None
        self._sw_i_correction: float | None = None

        # Boot inhibit: window detection is suppressed until enough temperature
        # history has accumulated to make the trend estimate reliable.
        # Cleared automatically once min_span_sec of data is available.
        self._window_detection_ready: bool = False

        # v0.3 — software on/off controller state
        # True  = device commanded ON by sw-controller
        # False = device commanded OFF by sw-controller
        # None  = sw-control inactive or state unknown (will re-evaluate)
        self._sw_device_on: bool | None = None
        self._sw_headroom = 0
        self._sw_blocked_since = None
        self._sw_applied_setpoint = None
        self._sw_applied_at = None
        self._sw_cloud_previous_heat = None
        self._sw_cloud_coast = None
        self._sw_observed_coast_minutes = None
        self._sw_cloud_internal_history = []
        self._sw_cloud_room_history = []
        self._sw_cloud_internal_rise_rate = None
        self._sw_cloud_internal_rate_lower = None
        self._sw_cloud_cutoff_offset = None
        self._sw_cloud_veto_count = 0
        self._sw_cloud_veto_window_start = None
        self._sw_cloud_cutoff_eta = None
        self._sw_cloud_internal_coast = None
        self._sw_internal_coast_rise = None
        self._sw_internal_coast_seconds = None
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
        self._sw_learning_store: Store | None = None
        self._sw_restore_pending: dict | None = None

        # Communication failure tracking — retry gate
        # Record the last failed poll. Transient failures retain state and
        # permit a retry after 5s; three failures mark an outage with 30s backoff.
        self._comm_failed_at: float | None = None
        self._comm_retry_interval: float = 30.0  # seconds between retry attempts
        self._comm_failures = 0

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
            await self.convector.set_temperature_correction(int(correction), source="climate._apply_stored_temperature_correction")
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
        self._sw_learning_store = Store(
            self.hass, 1, f"{DOMAIN}.{self._config_entry.entry_id}.sw_learning"
        )
        self._sw_restore_pending = await self._sw_learning_store.async_load()
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
            await self.convector.set_opened_window("off", source="climate.async_added_to_hass")
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
                _LOGGER.debug("Boot: restored HEAT+sw-control — evaluating immediately")
                await self.async_update()
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
        # Confirm transient failures promptly; use 30s backoff for an outage.
        # This avoids flooding the device while it is unreachable and keeps
        # the stale state intact so the widget and sw-controller stay stable.
        now_mono = self.hass.loop.time()
        retry_interval = 5.0 if self._attr_available else self._comm_retry_interval
        if self._comm_failed_at is not None:
            elapsed = now_mono - self._comm_failed_at
            if elapsed < retry_interval:
                _LOGGER.debug(
                    "Tesy Convector: skipping poll — retrying in %.0fs",
                    retry_interval - elapsed,
                )
                return
            _LOGGER.debug("Tesy Convector: retry attempt after %.0fs", elapsed)

        status = await self.convector.get_status(source="climate.async_update")
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
            self._comm_failures = 0
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
            if self._sw_was_enabled and not sw_control:
                if self._target_temp is not None:
                    await self._set_firmware_target()
                    status["payload"]["setTemp"]["payload"]["temp"] = math.floor(self._target_temp)
                self._reset_sw_controller()
                self._sw_fallback_active = False
            self._sw_was_enabled = sw_control

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
            # device_on is always True in sw-control OFF phase (minimum setpoint)
            # so skip the device_on check — _update_hvac_action uses _sw_device_on.
            if sw_control and self._hvac_mode == HVACMode.HEAT:
                internal_temp = self._extract_internal_temp(status)
                self._device_is_heating = device_on and (
                    internal_temp is None or internal_temp < status["payload"]["setTemp"]["payload"]["temp"]
                )
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
            if sw_control and self._hvac_mode == HVACMode.HEAT and not self._window_opened and (
                self._sw_device_on is not None or self._sw_fallback_active
            ):
                device_mode = status["payload"]["setMode"]["payload"]["name"]
                device_setpoint = status["payload"]["setTemp"]["payload"]["temp"]

                # Mode changed externally — respect it, stop sw-controller.
                if not device_on:
                    _LOGGER.info(
                        "SW ctrl: device turned OFF externally — stopping controller"
                    )
                    self._hvac_mode = HVACMode.OFF
                    await self._set_firmware_target()
                    self._reset_sw_controller()
                    self._sw_fallback_active = False
                    self._update_hvac_action()
                    self.async_write_ha_state()
                elif device_mode == "program":
                    _LOGGER.info(
                        "SW ctrl: device switched to AUTO externally — stopping controller"
                    )
                    self._hvac_mode = HVACMode.AUTO
                    # Program mode has already installed its schedule target;
                    # do not overwrite that with the cached HEAT setpoint.
                    self._reset_sw_controller()
                    self._sw_fallback_active = False
                    self._update_hvac_action()
                    self.async_write_ha_state()
                else:
                    # Setpoint changed externally — update target and let
                    # sw-controller adapt on next cycle using new value.
                    # Ignore controller writes, including the legacy OFF value.
                    if self._target_temp is not None:
                        sw_on_sp = self._sw_applied_setpoint or math.ceil(self._target_temp)
                        sw_off_sp = math.floor(self._target_temp) - 1
                        is_sw_setpoint = device_setpoint in (sw_on_sp, sw_off_sp, self._attr_min_temp, math.floor(self._target_temp))
                    else:
                        is_sw_setpoint = False
                    if not is_sw_setpoint and device_setpoint != self._target_temp:
                        _LOGGER.info(
                            "SW ctrl: setpoint changed externally %s -> %s — adapting",
                            self._target_temp, device_setpoint,
                        )
                        self._target_temp = float(device_setpoint)
                        self._persist_setpoint(float(device_setpoint))
                        self._reset_sw_controller()
                        self._sw_fallback_active = False
                        self.async_write_ha_state()

            self._internal_temp = self._extract_internal_temp(status)
            external_temp = self._get_external_temp(temperature_entity, sw_ema_alpha)
            self._current_temp = (
                external_temp if external_temp is not None else self._extract_internal_temp(status)
            )
            if external_temp is None:
                self._temp_history = []
                self._window_detection_ready = False
                # Sensor failure must not leave an automatically or manually
                # inhibited heater stuck OFF. Health handling owns failover.
                if use_external_temp and self._window_opened:
                    await self._close_window(now_mono)

            if sw_control and self._hvac_mode == HVACMode.HEAT and external_temp is None:
                await self._enter_firmware_fallback(self._external_temp_reason)

            await self._handle_window_detection(
                window_open_enabled=window_open_enabled,
                temp_fall_rate=temp_fall_rate,
                temp_recovery_rate=temp_recovery_rate,
                temp_recovery_abs=temp_recovery_abs,
                current_temp=external_temp,
            )

            if sw_control and self._hvac_mode == HVACMode.HEAT and not self._window_opened:
                if external_temp is not None:
                    self._leave_firmware_fallback_if_needed()
                    await self._run_sw_controller(
                        current_temp=external_temp,
                        min_on_sec=sw_min_on,
                        min_off_sec=sw_min_off,
                        hysteresis=sw_hysteresis,
                        lag_time_min=sw_lag_time,
                        i_gain=sw_i_gain,
                    )
            # The device can veto our ON request through its own thermostat.
            # This is an estimate, not a measured element/power signal.
            self._sw_firmware_limit_estimated = (
                self._internal_temp >= (self._sw_applied_setpoint or math.ceil(self._target_temp))
                if self._sw_device_on is True and self._internal_temp is not None
                and self._target_temp is not None else None
            )
            self._update_hvac_action()
        else:
            if "error" in status:
                self._comm_failed_at = now_mono
                self._comm_failures += 1
                if self._comm_failures >= 3 and self._attr_available:
                    _LOGGER.warning(
                        "Tesy Convector unreachable — marking unavailable, "
                        "retrying in %.0fs: %s",
                        self._comm_retry_interval, status.get("error"),
                    )
                    # Mark entity unavailable — blocks automations and commands.
                    self._attr_available = False
                    # Unknown device state — reset sw-controller.
                    self._reset_sw_controller()
                    self._sw_fallback_active = False
                    self._update_hvac_action()
                    self.async_write_ha_state()
                elif self._attr_available:
                    _LOGGER.debug(
                        "Tesy status poll failed (%s/3); retaining state until next poll",
                        self._comm_failures,
                    )
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

    def _sw_learning_context(self) -> dict:
        """Learning is specific to the room sensor, target, and control tuning."""
        options = self._get_options()
        return {
            "sensor": options.get(CONF_TEMPERATURE_ENTITY),
            "target": self._target_temp,
            "hysteresis": float(options.get(CONF_SW_HYSTERESIS, DEFAULT_SW_HYSTERESIS)),
            "lag": float(options.get(CONF_SW_LAG_TIME, DEFAULT_SW_LAG_TIME)),
            "ema": float(options.get(CONF_SW_EMA_ALPHA, DEFAULT_SW_EMA_ALPHA)),
            "i_gain": float(options.get(CONF_SW_I_GAIN, DEFAULT_SW_I_GAIN)),
        }

    def _restore_sw_learning_if_needed(self) -> None:
        """Restore completed-cycle learning once, after fresh feedback arrives.

        Never restore an actuator phase, coast peak, temperature history, or
        monotonic timer: none describes the heater reliably after a restart.
        Pending learning survives startup fallback until feedback is usable.
        """
        saved = self._sw_restore_pending
        self._sw_restore_pending = None
        if saved is None:
            return
        context = self._sw_learning_context()
        if not isinstance(saved, dict) or saved.get("context") != context:
            _LOGGER.debug("SW controller: saved learning does not match current settings")
            return
        try:
            correction = float(saved["overshoot_correction"])
            cycles = saved["duty_cycles"]
            if not isinstance(cycles, list) or not 0 <= correction <= context["hysteresis"]:
                raise ValueError("Invalid learned correction or duty cycles")
            complete = []
            for cycle in cycles[-5:]:
                on, off = cycle
                on, off = float(on), float(off)
                if not (math.isfinite(on) and math.isfinite(off) and on > 0 and off > 0):
                    raise ValueError("Invalid completed cycle durations")
                complete.append((on, off))
        except (KeyError, TypeError, ValueError, OverflowError):
            _LOGGER.warning("SW controller: invalid saved learning ignored")
            return
        self._sw_overshoot_correction = correction
        self._sw_duty_cycles = complete
        self._sw_duty_pct = (
            round(100 * mean(on / (on + off) for on, off in complete)) if complete else None
        )
        _LOGGER.info(
            "SW controller: restored correction=%.3f and %d completed duty cycles",
            correction, len(complete),
        )

    async def _save_sw_learning(self) -> None:
        """Save once per completed heating cycle, rather than on every poll."""
        if self._sw_learning_store is None:
            return
        saved = {
            "context": self._sw_learning_context(),
            "overshoot_correction": self._sw_overshoot_correction,
            "duty_cycles": [[on, off] for on, off in self._sw_duty_cycles if off > 0],
        }
        try:
            await self._sw_learning_store.async_save(saved)
        except OSError as exc:
            _LOGGER.warning("SW controller: could not save learning: %s", exc)

    def _reset_sw_controller(self) -> None:
        """Discard history whenever another controller may have driven the heater."""
        self._sw_device_on = None
        self._sw_headroom = 0
        self._sw_blocked_since = None
        self._sw_applied_setpoint = None
        self._sw_applied_at = None
        self._sw_cloud_previous_heat = None
        self._sw_cloud_coast = None
        self._sw_observed_coast_minutes = None
        self._sw_cloud_internal_history = []
        self._sw_cloud_room_history = []
        self._sw_cloud_internal_rise_rate = None
        self._sw_cloud_internal_rate_lower = None
        self._sw_cloud_cutoff_offset = None
        self._sw_cloud_veto_count = 0
        self._sw_cloud_veto_window_start = None
        self._sw_cloud_cutoff_eta = None
        self._sw_cloud_internal_coast = None
        self._sw_internal_coast_rise = None
        self._sw_internal_coast_seconds = None
        self._sw_temp_history = []
        self._sw_long_history = []
        self._sw_last_on_time = None
        self._sw_last_off_time = None
        self._sw_peak_temp = None
        self._sw_overshoot_correction = 0.0
        self._sw_duty_cycles = []
        self._sw_duty_pct = None
        self._sw_ramp_rate = None
        self._sw_predicted_temp = None
        self._sw_effective_off_threshold = None
        self._sw_i_correction = None
        self._sw_firmware_limit_estimated = None

    async def _set_firmware_target(self) -> None:
        """Restore the integer firmware target, preferring slight undershoot."""
        if self._target_temp is not None:
            result = await self.convector.set_temperature(math.floor(self._target_temp), source="climate._set_firmware_target")
            if isinstance(result, dict) and "error" in result:
                raise RuntimeError(f"Could not restore firmware target: {result['error']}")

    async def _enter_firmware_fallback(self, reason: str) -> None:
        """Transfer ownership once, including when SW state is unknown at boot."""
        if self._sw_fallback_active or self._target_temp is None:
            return
        # Set the safety target before enabling heating. Failed writes must
        # remain retryable rather than claiming that fallback was applied.
        await self._set_firmware_target()
        result = await self.convector.set_mode("heating", source="climate._enter_firmware_fallback")
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(f"Could not enable firmware heating: {result['error']}")
        self._reset_sw_controller()
        self._sw_fallback_active = True
        if self._current_temp is not None:
            self._device_is_heating = self._current_temp < math.floor(self._target_temp)
        _LOGGER.warning("SW controller: external sensor %s, entering firmware fallback", reason)

    def _leave_firmware_fallback_if_needed(self) -> None:
        if self._sw_fallback_active:
            self._reset_sw_controller()
            self._sw_fallback_active = False
            _LOGGER.info("SW controller: external sensor recovered, resuming software control")

    def _learn_coast_peak(self, hysteresis: float) -> None:
        """Learn once per completed coast, with slow updates and reversible bias."""
        if self._sw_peak_temp is None:
            return
        error = self._sw_peak_temp - self._target_temp
        if error > 0.02:
            correction = self._sw_overshoot_correction + 0.3 * error
        else:
            # No overshoot: ease the early cutoff as room conditions change.
            correction = self._sw_overshoot_correction * 0.8
        self._sw_overshoot_correction = max(0.0, min(hysteresis, correction))
        _LOGGER.info(
            "SW ctrl learned overshoot: peak=%.2f target=%.2f correction=%.3f",
            self._sw_peak_temp, self._target_temp, self._sw_overshoot_correction,
        )
        self._sw_peak_temp = None

    async def _run_sw_controller(
        self,
        current_temp: float,
        min_on_sec: float,
        min_off_sec: float,
        hysteresis: float,
        lag_time_min: float,
        i_gain: float,
    ) -> None:
        """Adaptive hysteresis, cycle prediction, then slow steady-state bias."""
        if self._hvac_mode != HVACMode.HEAT or self._target_temp is None:
            return
        self._restore_sw_learning_if_needed()
        now = self.hass.loop.time()
        self._sw_temp_history.append((now, current_temp))
        self._sw_temp_history = [(t, v) for t, v in self._sw_temp_history if now - t <= 600]
        self._observe_cloud_coast(current_temp, now, lag_time_min)
        self._sw_long_history.append((now, current_temp))
        self._sw_long_history = [(t, v) for t, v in self._sw_long_history if now - t <= SW_I_WINDOW_SEC]
        if self._sw_device_on is False and self._sw_peak_temp is not None:
            self._sw_peak_temp = max(self._sw_peak_temp, current_temp)

        setpoint = self._target_temp
        lower = setpoint - hysteresis
        i_correction = 0.0
        # The I-term needs a substantial window, not just six startup polls.
        # It only reduces the cutoff; a cool mean never authorizes heating
        # above target. Reserve headroom for cycle-level overshoot learning.
        if i_gain > 0 and now - self._sw_long_history[0][0] >= SW_I_WINDOW_SEC / 2:
            avg_error = mean(v for _, v in self._sw_long_history) - setpoint
            i_correction = max(0.0, min(hysteresis * 0.25, avg_error * i_gain))
        self._sw_overshoot_correction = min(hysteresis, self._sw_overshoot_correction)
        # Leave a useful band between ON and OFF even at maximum adaptation.
        combined = min(hysteresis * 0.8, self._sw_overshoot_correction + i_correction)
        effective_upper = setpoint - combined
        ramp_rate = 0.0
        if self._sw_device_on:
            ramp_rate = max(0.0, self.estimate_temp_trend(self._sw_temp_history, min_span_sec=20.0) or 0.0)
        prediction_minutes = (
            min(lag_time_min, self._sw_observed_coast_minutes)
            if self._sw_observed_coast_minutes is not None else lag_time_min
        )
        predicted_temp = current_temp + ramp_rate * prediction_minutes
        self._sw_ramp_rate = ramp_rate
        self._sw_predicted_temp = predicted_temp
        self._sw_effective_off_threshold = effective_upper
        self._sw_i_correction = i_correction
        _LOGGER.debug(
            "SW ctrl: temp=%.2f lower=%.2f ramp=%.3f predicted=%.2f cutoff=%.2f",
            current_temp, lower, ramp_rate, predicted_temp, effective_upper,
        )
        desired_on = (
            current_temp < effective_upper and predicted_temp < effective_upper
            if self._sw_device_on else current_temp <= lower
        )
        headroom = self._cloud_headroom(current_temp, lower, predicted_temp, desired_on, now)
        device_target = (
            min(math.ceil(setpoint) + headroom, math.floor(setpoint + 3), int(self._attr_max_temp))
            if desired_on else int(self._attr_min_temp)
        )
        if desired_on:
            headroom = max(0, device_target - math.ceil(setpoint))
        same_phase = desired_on == self._sw_device_on
        if same_phase and device_target == self._sw_applied_setpoint:
            self._sw_headroom = headroom
            return
        if desired_on and self._sw_last_off_time is not None:
            if now - self._sw_last_off_time < min_off_sec:
                return
        cloud = self._cloud_telemetry if self._get_options().get(CONF_CLOUD_TELEMETRY_ENABLED, False) else None
        # With a live heating report, the room reaching target takes priority
        # over the wear timer. Prediction still respects normal minimum times.
        reached_target = cloud is not None and cloud.heating is True and current_temp >= setpoint
        if not desired_on and self._sw_device_on and self._sw_last_on_time is not None and not reached_target:
            if now - self._sw_last_on_time < min_on_sec:
                return

        # Keep the TCP connection alive by lowering the thermostat for OFF.
        # floor(target)-1 could still heat if the internal sensor reads colder
        # than the room sensor. Use the supported minimum, retaining low-temp
        # firmware protection. Live telemetry can justify bounded ON headroom.
        result = await self.convector.set_temperature(device_target, source="climate._run_sw_controller")
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(f"Could not apply SW phase: {result['error']}")
        if self._sw_device_on is None:
            result = await self.convector.set_mode("heating", source="climate._run_sw_controller")
            if isinstance(result, dict) and "error" in result:
                raise RuntimeError(f"Could not enable SW heating mode: {result['error']}")
        self._sw_headroom = headroom
        self._sw_applied_setpoint = device_target
        self._sw_applied_at = now
        self._sw_cloud_veto_count = 0
        self._sw_cloud_veto_window_start = None
        self._sw_blocked_since = None
        self._sw_cloud_cutoff_eta = None
        if same_phase:
            # Correct the firmware ceiling without inventing another duty cycle
            # or losing the room's warming trend and minimum-time accounting.
            return
        cycle_completed = desired_on and self._sw_peak_temp is not None
        if desired_on:
            self._learn_coast_peak(hysteresis)
            if (
                self._sw_duty_cycles and self._sw_last_off_time is not None
                and self._sw_duty_cycles[-1][1] == 0
            ):
                on_dur, _ = self._sw_duty_cycles[-1]
                self._sw_duty_cycles[-1] = (on_dur, now - self._sw_last_off_time)
                complete = [(on, off) for on, off in self._sw_duty_cycles if off > 0]
                if complete:
                    self._sw_duty_pct = round(100 * mean(on / (on + off) for on, off in complete))
            self._sw_last_on_time = now
        else:
            self._sw_last_off_time = now
            if self._sw_device_on is True:
                self._sw_peak_temp = current_temp
                if self._sw_last_on_time is not None:
                    self._sw_duty_cycles.append((now - self._sw_last_on_time, 0.0))
                    self._sw_duty_cycles = self._sw_duty_cycles[-5:]
        self._sw_device_on = desired_on
        self._sw_temp_history = [(now, current_temp)]
        if cycle_completed:
            await self._save_sw_learning()
        _LOGGER.info(
            "SW ctrl %s: temp=%.2f predicted=%.2f cutoff=%.2f",
            "ON" if desired_on else "OFF", current_temp, predicted_temp, effective_upper,
        )
        self._update_hvac_action()
        self.async_write_ha_state()

    def _observe_cloud_coast(self, current_temp, now, lag_limit):
        """Estimate coast horizon from actual heating stops and room rise rate."""
        cloud = self._cloud_telemetry if self._get_options().get(CONF_CLOUD_TELEMETRY_ENABLED, False) else None
        heating = cloud.heating if cloud is not None else None
        if heating is None:
            self._sw_cloud_previous_heat = None
            self._sw_cloud_coast = None
            # A previously learned live-session estimate can be reused only
            # while telemetry is present; stale periods use configured lag.
            self._sw_observed_coast_minutes = None
            self._sw_cloud_internal_history = []
            self._sw_cloud_room_history = []
            self._sw_cloud_internal_rise_rate = None
            self._sw_cloud_internal_rate_lower = None
            self._sw_cloud_cutoff_offset = None
            self._sw_cloud_veto_count = 0
            self._sw_cloud_veto_window_start = None
            self._sw_cloud_cutoff_eta = None
            self._sw_cloud_internal_coast = None
            return
        internal = cloud.temperature
        # Preserve the pre-stop room ramp across software phase changes.
        room_history = self._sw_cloud_room_history
        room_history.append((now, current_temp))
        room_history[:] = [(t, v) for t, v in room_history if now - t <= 300]
        if internal is not None and cloud.age is not None:
            reported_at = now - cloud.age
            history = self._sw_cloud_internal_history
            # Repeated HA polls of the same cloud report are not new samples.
            if not history or reported_at - history[-1][0] >= 1:
                history.append((reported_at, internal))
            history[:] = [(t, v) for t, v in history if reported_at - t <= 300]
            self._sw_cloud_internal_rise_rate = None
            self._sw_cloud_internal_rate_lower = None
            recent = [(t, v) for t, v in history if reported_at - t <= 120]
            if len(recent) >= 3:
                span = recent[-1][0] - recent[0][0]
                rise = recent[-1][1] - recent[0][1]
                # One 0.5 C step can be rounding alone. Require two steps
                # over a minute, then subtract endpoint uncertainty from slope.
                if span >= 60 and rise >= 2 * CLOUD_TEMPERATURE_STEP:
                    self._sw_cloud_internal_rise_rate = self.estimate_temp_trend(recent, min_span_sec=60)
                    self._sw_cloud_internal_rate_lower = max(0.0, 60 * (rise - CLOUD_TEMPERATURE_STEP) / span)
        else:
            self._sw_cloud_internal_history = []
            self._sw_cloud_internal_rise_rate = None
            self._sw_cloud_internal_rate_lower = None
        self._sw_cloud_cutoff_eta = None
        if self._sw_cloud_previous_heat is True and heating is False:
            rate = max(0.0, self.estimate_temp_trend(room_history, min_span_sec=20) or 0.0)
            self._sw_cloud_coast = (now, current_temp, current_temp, rate)
            self._sw_cloud_internal_coast = (
                (now, internal, internal, now) if internal is not None else None
            )
            # Learn only firmware vetoes during requested ON, with a report
            # newer than our command. Software OFF is not a thermostat cutoff.
            if (
                internal is not None and self._sw_device_on is True
                and self._sw_applied_at is not None and cloud.age is not None
                and now - cloud.age > self._sw_applied_at
                and abs(internal - self._sw_applied_setpoint) <= 1
            ):
                offset = internal - self._sw_applied_setpoint
                previous = self._sw_cloud_cutoff_offset
                self._sw_cloud_cutoff_offset = offset if previous is None else 0.8 * previous + 0.2 * offset
                if self._sw_cloud_veto_window_start is None or now - self._sw_cloud_veto_window_start > 300:
                    self._sw_cloud_veto_window_start = now
                    self._sw_cloud_veto_count = 0
                self._sw_cloud_veto_count += 1
        internal_rate = self._sw_cloud_internal_rise_rate
        internal_coast = self._sw_cloud_internal_coast
        if internal_coast is not None:
            stopped_at, stopped_temp, peak, peaked_at = internal_coast
            if internal is None:
                self._sw_cloud_internal_coast = None
            else:
                if internal > peak:
                    peak, peaked_at = internal, now
                if now - stopped_at >= 30 and heating is False and peak - internal >= CLOUD_TEMPERATURE_STEP:
                    self._sw_internal_coast_rise = peak - stopped_temp
                    self._sw_internal_coast_seconds = peaked_at - stopped_at
                    self._sw_cloud_internal_coast = None
                elif heating is True or now - stopped_at > 600:
                    self._sw_cloud_internal_coast = None
                else:
                    self._sw_cloud_internal_coast = (stopped_at, stopped_temp, peak, peaked_at)
        if (
            heating is True and internal is not None and internal_rate is not None
            and internal_rate >= 0.02 and self._sw_cloud_cutoff_offset is not None
            and self._sw_device_on is True and self._sw_applied_setpoint is not None
            and self._sw_applied_at is not None and cloud.age is not None
            and now - cloud.age > self._sw_applied_at
        ):
            cutoff = self._sw_applied_setpoint + self._sw_cloud_cutoff_offset
            # Both cutoff and current reading are rounded +/- 0.25 C.
            # Use the latest plausible cutoff and slowest plausible rise:
            # an apparent threshold crossing alone must not trigger a boost.
            lower_rate = self._sw_cloud_internal_rate_lower
            if lower_rate is not None and lower_rate >= 0.02:
                gap = max(0.0, cutoff - internal + CLOUD_TEMPERATURE_STEP)
                self._sw_cloud_cutoff_eta = 60 * gap / lower_rate
        coast = self._sw_cloud_coast
        if coast is not None:
            stopped_at, stopped_temp, peak, rate = coast
            peak = max(peak, current_temp)
            if now - stopped_at >= 30 and heating is False and current_temp < peak - 0.02:
                # Ignore nearly flat ramps: dividing by them amplifies noise.
                if rate >= 0.02 and peak - stopped_temp >= 0.02:
                    measured = max(0.0, min(lag_limit, (peak - stopped_temp) / rate))
                    previous = self._sw_observed_coast_minutes
                    # A single noisy coast must not sharply shorten prediction.
                    previous = lag_limit if previous is None else previous
                    self._sw_observed_coast_minutes = 0.8 * previous + 0.2 * measured
                self._sw_cloud_coast = None
            elif heating is True or now - stopped_at > 600:
                self._sw_cloud_coast = None
            else:
                self._sw_cloud_coast = (stopped_at, stopped_temp, peak, rate)
        self._sw_cloud_previous_heat = heating

    def _cloud_headroom(self, current_temp, lower, predicted_temp, desired_on, now):
        """Permit bounded firmware headroom only after sustained live vetoes."""
        cloud = self._cloud_telemetry if self._get_options().get(CONF_CLOUD_TELEMETRY_ENABLED, False) else None
        if cloud is None or cloud.heating is None:
            self._sw_blocked_since = None
            return 0
        eligible = (
            desired_on and self._sw_device_on is True
            and current_temp < lower and predicted_temp < lower and self._sw_applied_at is not None
            and cloud.age is not None and now - cloud.age > self._sw_applied_at
        )
        # Quantized temperature may hide the rise rate of a short thermostat
        # cycle. Three observed vetoes are stronger evidence than one step.
        if (
            eligible and cloud.heating is False and self._sw_cloud_veto_count >= 3
            and self._sw_cloud_veto_window_start is not None
            and now - self._sw_cloud_veto_window_start <= 300
            and now - self._sw_applied_at >= 120
        ):
            self._sw_blocked_since = None
            return min(3, self._sw_headroom + 1)
        # After observing a real firmware cutoff, its internal rise rate can
        # justify one step just before another veto, rather than waiting cold.
        if (
            eligible and cloud.heating is True and self._sw_cloud_cutoff_eta is not None
            and self._sw_cloud_cutoff_eta <= 30 and now - self._sw_applied_at >= 120
        ):
            self._sw_blocked_since = None
            return min(3, self._sw_headroom + 1)
        blocked = eligible and cloud.heating is False and (
            cloud.temperature is None
            or cloud.temperature - CLOUD_TEMPERATURE_STEP / 2 >= self._sw_applied_setpoint - 0.5
        ) and self._sw_cloud_internal_coast is None
        if not blocked:
            self._sw_blocked_since = None
            return self._sw_headroom
        if self._sw_blocked_since is None:
            self._sw_blocked_since = now
        if now - self._sw_blocked_since >= 60 and now - self._sw_applied_at >= 120:
            return min(3, self._sw_headroom + 1)
        return self._sw_headroom

    def _get_external_temp(
        self,
        temperature_entity: str | None,
        ema_alpha: float = DEFAULT_SW_EMA_ALPHA,
    ) -> float | None:
        """Acquire, validate freshness, and filter exactly once per poll."""
        if temperature_entity != self._external_sensor_entity:
            self._external_sensor_entity = temperature_entity
            self._ema_temp = None
            self._reset_sw_controller()
            self._temp_history = []
            self._window_detection_ready = False
        self._external_temp_valid = False
        self._external_raw_temp = None
        self._external_temp_age_sec = None
        self._external_temp_reason = "missing"
        state = self.hass.states.get(temperature_entity) if temperature_entity else None
        raw = None
        if state is not None:
            # last_reported includes unchanged numeric reports on modern HA.
            reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
            if reported is not None:
                self._external_temp_age_sec = max(0.0, (datetime.now(timezone.utc) - reported).total_seconds())
            try:
                raw = float(state.state)
            except (TypeError, ValueError):
                self._external_temp_reason = "unavailable or invalid"
            else:
                if not math.isfinite(raw) or not -40.0 <= raw <= 80.0:
                    self._external_temp_reason = "out of range"
                    raw = None
                elif self._external_temp_age_sec is None or self._external_temp_age_sec > EXTERNAL_TEMP_MAX_AGE_SEC:
                    self._external_temp_reason = "stale"
                    raw = None
        if raw is None:
            self._ema_temp = None
            return None
        self._external_temp_valid = True
        self._external_raw_temp = raw
        alpha = max(0.0, min(1.0, ema_alpha))
        self._ema_temp = raw if self._ema_temp is None else alpha * raw + (1.0 - alpha) * self._ema_temp
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
        current_temp: float | None,
    ) -> None:
        """Consume the poll's validated sample for window detection only."""
        if self._manual_window_override or current_temp is None or not window_open_enabled:
            return
        now = self.hass.loop.time()
        self._temp_history.append((now, current_temp))
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
            self._min_temp_during_open = current_temp
            self._temp_history = []
            self._window_detection_ready = False
            await self.convector.set_opened_window("on", source="climate._handle_window_detection")
            await self.async_set_hvac_mode(HVACMode.OFF, _from_window_detection=True)
            _LOGGER.info("Window open detected. Rate = %.2f °C/min", trend)

        elif self._window_opened:
            if self._window_change_time is None:
                self._window_change_time = now
            if self._min_temp_during_open is None or current_temp < self._min_temp_during_open:
                self._min_temp_during_open = current_temp

            time_open = now - self._window_change_time
            dtemp = (
                current_temp - self._min_temp_during_open
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

    def _extract_internal_temp(self, status: dict) -> float | None:
        """Extract current temperature from device status payload (tempAir key)."""
        try:
            return status["payload"]["tempAir"]["payload"]["temp"]
        except (KeyError, TypeError):
            return None

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

        await self.convector.set_opened_window("off", source="climate._close_window")
        if self._prev_hvac_mode and self._prev_hvac_mode != HVACMode.OFF:
            await self.async_set_hvac_mode(self._prev_hvac_mode, _from_window_detection=True)
            self._prev_hvac_mode = None

        # Reset sw-controller so it re-evaluates cleanly after window closes
        self._reset_sw_controller()
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
        Use HVACAction enum values, or None when enabled cloud telemetry is
        unknown, so stale reports are never presented as confirmed heating.
        """
        if self._window_opened:
            self._attr_hvac_action = HVACAction.OFF
            return
        if self._hvac_mode == HVACMode.OFF:
            self._attr_hvac_action = HVACAction.OFF
            return

        options = self._get_options()
        sw_control = options.get(CONF_SW_CONTROL_ENABLED, False)

        if options.get(CONF_CLOUD_TELEMETRY_ENABLED, False):
            reported = self._cloud_telemetry.heating if self._cloud_telemetry is not None else None
            self._attr_hvac_action = None if reported is None else (
                HVACAction.HEATING if reported else HVACAction.IDLE
            )
            return

        if self._hvac_mode == HVACMode.HEAT and sw_control and not self._sw_fallback_active:
            self._attr_hvac_action = HVACAction.HEATING if self._sw_device_on is True else HVACAction.IDLE
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
        if options.get(CONF_CLOUD_TELEMETRY_ENABLED, False):
            telemetry = self._cloud_telemetry
            attrs.update({
                "cloud_heating": telemetry.heating if telemetry is not None else None,
                "cloud_current_temp": telemetry.temperature if telemetry is not None else None,
                "cloud_telemetry_connected": telemetry.connected if telemetry is not None else False,
                "cloud_telemetry_age_sec": round(telemetry.age, 1) if telemetry is not None and telemetry.age is not None else None,
            })
        if not options.get(CONF_SW_CONTROL_ENABLED, False):
            return attrs
        state = "SW_DISABLED"
        if self._window_opened:
            state = "WINDOW_INHIBIT"
        elif self._hvac_mode == HVACMode.HEAT:
            state = "SW_WAITING_FOR_SENSOR" if self._sw_fallback_active else (
                "SW_HEATING" if self._sw_device_on else "SW_IDLE"
            )
        attrs.update({
            "sw_control_state": state,
            "external_temp_valid": self._external_temp_valid,
            "external_temp_age_sec": round(self._external_temp_age_sec, 1) if self._external_temp_age_sec is not None else None,
            "filtered_external_temp": self._ema_temp,
            "raw_external_temp": self._external_raw_temp,
            "internal_temp": self._internal_temp,
            "external_temperature_error": (
                round(self._external_raw_temp - self._target_temp, 3)
                if self._external_raw_temp is not None and self._target_temp is not None else None
            ),
            "sw_heat_requested": self._sw_device_on,
            "sw_firmware_headroom": self._sw_headroom,
            "sw_firmware_setpoint": self._sw_applied_setpoint,
            "sw_observed_coast_minutes": self._sw_observed_coast_minutes,
            "sw_cloud_internal_rise_rate": self._sw_cloud_internal_rise_rate,
            "sw_firmware_cutoff_temperature": (
                self._sw_applied_setpoint + self._sw_cloud_cutoff_offset
                if self._sw_device_on is True and self._sw_applied_setpoint is not None
                and self._sw_cloud_cutoff_offset is not None else None
            ),
            "sw_firmware_cutoff_eta_sec": self._sw_cloud_cutoff_eta,
            "sw_internal_coast_rise": self._sw_internal_coast_rise,
            "sw_internal_coast_seconds": self._sw_internal_coast_seconds,
            "firmware_limit_estimated": self._sw_firmware_limit_estimated,
            "firmware_fallback_active": self._sw_fallback_active,
            "overshoot_correction": round(self._sw_overshoot_correction, 3),
            "i_correction": self._sw_i_correction,
            "ramp_rate_c_per_min": self._sw_ramp_rate,
            "predicted_temp": self._sw_predicted_temp,
            "effective_off_threshold": self._sw_effective_off_threshold,
        })
        if self._sw_duty_pct is not None:
            attrs["duty_cycle_pct"] = self._sw_duty_pct
        if self._sw_duty_cycles:
            complete = [(on, off) for on, off in self._sw_duty_cycles if off > 0]
            attrs["duty_cycles_sampled"] = len(complete)
        return attrs

    @property
    def hvac_action(self) -> HVACAction | None:
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

        options = self._get_options()
        sw_control = options.get(CONF_SW_CONTROL_ENABLED, False)
        if hvac_mode == HVACMode.HEAT:
            if not sw_control:
                await self._set_firmware_target()
            await self.convector.set_mode("heating", source="climate.async_set_hvac_mode")
        elif hvac_mode == HVACMode.OFF:
            await self.convector.turn_off(source="climate.async_set_hvac_mode")
            await self._set_firmware_target()
        elif hvac_mode == HVACMode.AUTO:
            await self._set_firmware_target()
            await self.convector.set_mode("program", source="climate.async_set_hvac_mode")
        self._reset_sw_controller()
        self._sw_fallback_active = False
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
            self._reset_sw_controller()
            if self._sw_fallback_active:
                await self._set_firmware_target()
                return
            # Safety setpoint: ceil(target) so the device thermostat cuts off
            # just above the real target if HA ever loses control.
            safety_setpoint = math.ceil(temp)   # e.g. 20.5→21, 20.0→20
            _LOGGER.debug(
                "SW ctrl: setpoint %.1f°C -> device setTemp=%d (safety backstop)",
                temp, safety_setpoint,
            )
            await self.convector.set_temperature(safety_setpoint, source="climate.async_set_temperature")
            await asyncio.sleep(0.1)
            return

        # ── HEAT mode, sw-control OFF ────────────────────────────────────────
        # Floor to integer — slightly cool is better than overshoot.
        device_setpoint = int(temp)  # floor: 20.5 -> 20, 21.0 -> 21
        if self._target_temp != temp:
            _LOGGER.debug(
                "HEAT: setpoint %.1f°C -> device setTemp=%d (floor)", temp, device_setpoint
            )
            await self.convector.set_temperature(device_setpoint, source="climate.async_set_temperature")
            self._target_temp = temp
            self._persist_setpoint(temp)
            await asyncio.sleep(0.1)


    async def async_set_opened_window(self, status):
        await self.convector.set_opened_window(status, source="climate.async_set_opened_window")

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
            await self.convector.set_opened_window("on", source="climate.async_handle_manual_window_status")
            await self.async_set_hvac_mode(HVACMode.OFF, _from_window_detection=True)
            self._window_opened = True
            _LOGGER.info("Manual window open triggered via service")

        elif status == "off":
            if not self._window_opened:
                _LOGGER.debug("Window already closed (manual override)")
                return
            await self._close_window(now)
            _LOGGER.info("Manual window close triggered via service")
