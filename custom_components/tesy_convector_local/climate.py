"""Climate platform for Tesy Convector Local integration.

Defines the TesyConvectorClimate entity, window detection logic, and device state management.
"""

import asyncio
from datetime import timedelta
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service
from statistics import mean
import voluptuous as vol


from .const import (
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_TEMP_FALL_RATE,
    CONF_TEMP_RECOVERY_ABS,
    CONF_TEMP_RECOVERY_RATE,
    CONF_TEMPERATURE_ENTITY,
    CONF_USE_INTERNAL_TEMP,
    CONF_WINDOW_OPEN_ENABLED,
    DEFAULT_TEMP_FALL_RATE,
    DEFAULT_TEMP_RECOVERY_ABS,
    DEFAULT_TEMP_RECOVERY_RATE,
    DOMAIN,
    WINDOW_RECOVERY_HYSTERESIS_SEC,
)
from .tesy_convector import TesyConvector

_LOGGER = logging.getLogger(__name__)

# Define the schema for the set_opened_window service
# This service allows manual control of the window open status for the climate entity.
# It expects an entity_id and a status ("on" or "off").
# This is useful for testing or manual control scenarios.
SET_OPENED_WINDOW_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("status"): vol.In(["on", "off"]),
    }
)


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set up the Tesy Convector climate entity."""
    ip_address = config_entry.data.get(CONF_IP_ADDRESS)
    temperature_entity = config_entry.options.get(CONF_TEMPERATURE_ENTITY)
    model = config_entry.data.get(CONF_MODEL)
    convector = TesyConvector(ip_address, model)

    # Determine internal vs. external temperature source
    use_internal_temp = config_entry.options.get(
        CONF_USE_INTERNAL_TEMP, not bool(temperature_entity)
    )
    if use_internal_temp:
        temperature_entity = None  # Ignore external if internal is forced

    # entity = TesyConvectorClimate(convector, temperature_entity)
    entity = TesyConvectorClimate(convector, temperature_entity, config_entry)
    async_add_entities([entity])

    # Make the entity accessible to binary_sensor
    hass.data[DOMAIN][config_entry.entry_id]["climate_entity"] = entity

    # Register manual override service
    schema, handler = create_handle_set_opened_window(hass, config_entry.entry_id)
    hass.services.async_register(
        DOMAIN,
        "set_opened_window",
        handler,
        schema=schema,
    )


def create_handle_set_opened_window(hass: HomeAssistant, entry_id: str):
    """Return a validated service handler for manual window override."""

    async def handle(call):
        entity_id = call.data["entity_id"]
        status = call.data["status"]

        entity = hass.data[DOMAIN][entry_id].get("climate_entity")
        if not entity:
            _LOGGER.error("Climate entity not found for entry_id %s", entry_id)
            return
        if entity.entity_id != entity_id:
            _LOGGER.warning("Entity ID mismatch: ignoring call to %s", entity_id)
            return

        await entity.async_handle_manual_window_status(status)

    return SET_OPENED_WINDOW_SCHEMA, handle


class TesyConvectorClimate(ClimateEntity):
    """Representation of a Tesy Convector as a ClimateEntity."""

    def __init__(self, convector, temperature_entity, config_entry) -> None:
        """Initialize the Tesy Convector climate entity."""
        super().__init__()
        self._device = convector

        self._config_entry = config_entry
        self._remove_update_listener = None
        self.convector = convector
        # options = config_entry.options
        # self.temperature_entity = options.get(CONF_TEMPERATURE_ENTITY)
        self.temperature_entity = temperature_entity
        self._attr_name = f"Tesy Convector {convector.model} ({convector.ip_address})"
        # self._attr_name = "Tesy Convector"
        self._attr_temperature_unit = (
            UnitOfTemperature.CELSIUS
        )  # Updated to use UnitOfTemperature.CELSIUS
        # Explicitly declare both TARGET_TEMPERATURE
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

        # self._config_entry = None
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
        self._attr_min_temp = 10
        self._attr_max_temp = 30
        self._attr_target_temperature_step = 1
        self._hvac_mode = HVACMode.OFF  # Default to off initially
        self._current_temp = None  # Variable to store current temperature
        self._target_temp = None  # Variable to store target temperature
        # Generate unique ID using model and IP address
        self._attr_unique_id = f"{convector.model}_{convector.ip_address}"
        self._bad_response_logged = False
        self._temp_history = []
        self._window_opened = False
        self._window_open_log = False
        self._window_close_log = False
        self._prev_hvac_mode = None
        # self._config_entry_id = config_entry_id
        self._recovery_started_at = None
        self._window_change_time = None
        self._min_temp_during_open = None  # Track minimum temp during open state
        # Add a manual override flag to the climate entity - from service.yaml
        self._manual_window_override: bool = False

    def _get_options(self):
        """Return the latest options from the config entry."""
        return self._config_entry.options if self._config_entry else {}
        # entry = self.hass.config_entries.async_get_entry(self._config_entry_id)
        # return entry.options if entry else {}

    async def async_added_to_hass(self):
        """Run when entity is added to Home Assistant.

        Sets up periodic updates and stores config entry ID.
        """
        self._remove_update_listener = async_track_time_interval(
            self.hass, self.async_update, timedelta(seconds=10)
        )

        """Find and store the actual ConfigEntry for this entity based on host IP."""
        # for entry in self.hass.config_entries.async_entries(DOMAIN):
        #    if entry.data.get(CONF_IP_ADDRESS) == self._config_entry_id:
        #        self._config_entry = entry
        #    break
        # self._config_entry_id = (
        #    self._attr_unique_id.rsplit("_", maxsplit=1)[-1]
        #    if hasattr(self, "_attr_unique_id")
        #    else None
        # )

    async def async_update(self, *args):
        """Fetch new state data and handle window open detection.

        Updates device state, HVAC mode, and runs window detection logic.
        """
        options = self._get_options()
        window_open_enabled = options.get(CONF_WINDOW_OPEN_ENABLED, False)
        temp_fall_rate = options.get(CONF_TEMP_FALL_RATE, DEFAULT_TEMP_FALL_RATE)
        temp_recovery_rate = options.get(
            CONF_TEMP_RECOVERY_RATE, DEFAULT_TEMP_RECOVERY_RATE
        )
        temp_recovery_abs = options.get(
            CONF_TEMP_RECOVERY_ABS, DEFAULT_TEMP_RECOVERY_ABS
        )
        use_internal_temp = options.get(CONF_USE_INTERNAL_TEMP, True)

        # Always fetch and parse device status
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
            if status["payload"]["onOff"]["payload"]["status"] == "on":
                current_mode = status["payload"]["setMode"]["payload"]["name"]
                if current_mode == "program":
                    self._hvac_mode = HVACMode.AUTO
                else:
                    self._hvac_mode = HVACMode.HEAT
            else:
                self._hvac_mode = HVACMode.OFF
            self._target_temp = status["payload"]["setTemp"]["payload"]["temp"]
            await self._handle_window_detection(
                window_open_enabled=window_open_enabled,
                temp_fall_rate=temp_fall_rate,
                temp_recovery_rate=temp_recovery_rate,
                temp_recovery_abs=temp_recovery_abs,
                use_internal_temp=use_internal_temp,
                status=status,
            )
        else:
            if not self._bad_response_logged:
                _LOGGER.error(
                    "Unexpected response structure from Tesy Convector: %s", status
                )
                self._bad_response_logged = True
            self._hvac_mode = HVACMode.OFF
            self._target_temp = None

    def estimate_temp_trend(self, history: list[tuple[float, float]]) -> float | None:
        """Estimate temperature trend (slope in °C/min) via linear regression.

        This version is optimized for short-term responsiveness,
        minimizing smoothing over long buffers.
        """
        if len(history) < 2:
            return None

        # Use last N seconds to focus only on most recent changes
        now = history[-1][0]
        recent = [(t, temp) for t, temp in history if now - t <= 120]  # max 120s

        if len(recent) < 2:
            return None

        t0 = recent[0][0]
        times = [t - t0 for t, _ in recent]  # relative times in seconds
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

        return (numerator / denominator) * 60  # °C/min

    async def _handle_window_detection(
        self,
        window_open_enabled: bool,
        temp_fall_rate: float,
        temp_recovery_rate: float,
        temp_recovery_abs: float,
        use_internal_temp: bool,
        status: dict,
    ) -> None:
        """Handle window open/close detection and update current temperature.

        Tracks temperature history, detects window open/close events, and updates device state.
        """
        # Skip window detection if manual override is active
        # This allows manual control of the window open state via service calls.
        if self._manual_window_override:
            _LOGGER.debug("Window detection skipped due to manual override")
            return

        now = self.hass.loop.time()

        if self.temperature_entity and not use_internal_temp:
            temp_state = self.hass.states.get(self.temperature_entity)
            temp = None

            if temp_state and temp_state.state not in ("unavailable", "unknown"):
                try:
                    temp_val = float(temp_state.state)
                    if -40.0 <= temp_val <= 80.0:
                        temp = temp_val
                except ValueError:
                    # pass
                    # Handle if temp is invalid
                    # fallback to internal temp
                    if self._window_opened:
                        _LOGGER.warning(
                            "Invalid temperature value from entity %s: %s",
                            self.temperature_entity,
                            temp_state.state,
                        )
                        self._window_opened = False  # reset window state
                        await self.convector.set_opened_window(
                            "off"
                        )  # removes window open state of convector
                        if (
                            self._prev_hvac_mode
                            and self._prev_hvac_mode != HVACMode.OFF
                        ):
                            await self.async_set_hvac_mode(self._prev_hvac_mode)
                            self._prev_hvac_mode = None
                        self._window_close_log = True
                        self._window_open_log = False
                        self._window_change_time = now
                        self._temp_history = []  # reset after closing
                    try:
                        # Fallback to internal temp if external is invalid
                        self._current_temp = status["payload"]["setTemp"]["payload"][
                            "temp"
                        ]
                    except Exception:
                        self._current_temp = None
                    return

            self._current_temp = temp

            if temp is not None and window_open_enabled:
                self._temp_history.append((now, temp))
                self._temp_history = [
                    x for x in self._temp_history if now - x[0] <= 180
                ]

                trend = self.estimate_temp_trend(self._temp_history)

                if trend is None:
                    return

                # --- Detect Window Open ---
                if not self._window_opened and trend < -temp_fall_rate:
                    self._window_opened = True
                    self._prev_hvac_mode = self._hvac_mode
                    await self.convector.set_opened_window("on")
                    await self.async_set_hvac_mode(HVACMode.OFF)
                    self._window_open_log = True
                    self._window_close_log = False
                    self._window_change_time = now
                    self._min_temp_during_open = temp  # Start tracking from here
                    self._temp_history = []  # reset after mode switch
                    _LOGGER.info("Window open detected. Rate = %.2f °C/min", trend)

                # --- Detect Window Close ---
                elif self._window_opened:
                    if getattr(self, "_window_change_time", None) is None:
                        self._window_change_time = now

                    if (
                        self._min_temp_during_open is None
                        or temp < self._min_temp_during_open
                    ):
                        self._min_temp_during_open = temp

                    time_since_change = now - self._window_change_time
                    dtemp = (
                        temp - self._min_temp_during_open
                        if self._min_temp_during_open is not None
                        else 0
                    )

                    if time_since_change > WINDOW_RECOVERY_HYSTERESIS_SEC and (
                        trend > temp_recovery_rate or dtemp > temp_recovery_abs
                    ):
                        self._window_opened = False
                        await self.convector.set_opened_window("off")
                        if (
                            self._prev_hvac_mode
                            and self._prev_hvac_mode != HVACMode.OFF
                        ):
                            await self.async_set_hvac_mode(self._prev_hvac_mode)
                            self._prev_hvac_mode = None
                        self._window_close_log = True
                        self._window_open_log = False
                        self._window_change_time = now
                        self._temp_history = []  # reset after closing
                        _LOGGER.info(
                            "Window closed detected. Recovery rate = %.2f °C/min, delta = %.2f °C",
                            trend,
                            dtemp,
                        )
        else:
            # fallback to internal temp
            try:
                self._current_temp = status["payload"]["setTemp"]["payload"]["temp"]
            except Exception:
                self._current_temp = None

    @property
    def hvac_mode(self):
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temp

    @property
    def target_temperature(self):
        """Return the current target temperature."""
        return self._target_temp

    async def async_set_hvac_mode(self, hvac_mode):
        """Set the HVAC mode (on/off/auto)."""
        if hvac_mode == HVACMode.HEAT:
            await self.convector.set_mode("heating")  # Set to program mode
        elif hvac_mode == HVACMode.OFF:
            await self.convector.turn_off()
        elif hvac_mode == HVACMode.AUTO:  # Add this condition
            await self.convector.set_mode("program")  # Set to program mode

        # Add a delay after setting the HVAC mode to give the convector time to process
        await asyncio.sleep(0.1)

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temp = kwargs.get("temperature")
        if temp is not None and self._target_temp != temp:
            await self.convector.set_temperature(temp)
            self._target_temp = temp  # Update the internal target temperature
            await asyncio.sleep(0.1)

    async def async_set_opened_window(self, status):
        """Service call to set the opened window status."""
        await self.convector.set_opened_window(status)

    @property
    def is_window_opened(self) -> bool:
        """Helper function to expose window opening status."""
        return self._window_opened

    async def async_handle_manual_window_status(self, status: str) -> None:
        """Manually override window status and lock automatic detection logic."""
        now = self.hass.loop.time()

        if not hasattr(self, "_manual_window_override"):
            self._manual_window_override = False

        if status == "on":
            if self._window_opened:
                _LOGGER.debug("Window already open (manual override)")
                return

            self._manual_window_override = True
            self._window_change_time = now
            self._prev_hvac_mode = self.hvac_mode
            self._min_temp_during_open = self.current_temperature

            await self.convector.set_opened_window("on")
            await self.async_set_hvac_mode(HVACMode.OFF)
            self._window_opened = True

            _LOGGER.info("Manual window open triggered via service")

        elif status == "off":
            if not self._window_opened:
                _LOGGER.debug("Window already closed (manual override)")
                return

            self._manual_window_override = True
            self._window_change_time = now

            await self.convector.set_opened_window("off")
            if self._prev_hvac_mode and self._prev_hvac_mode != HVACMode.OFF:
                await self.async_set_hvac_mode(self._prev_hvac_mode)

            self._window_opened = False
            self._min_temp_during_open = None

            _LOGGER.info("Manual window close triggered via service")

