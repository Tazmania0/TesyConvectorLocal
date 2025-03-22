import asyncio

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta

from .tesy_convector import TesyConvector
from .const import DOMAIN 
import logging

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the Tesy Convector climate entity."""
    ip_address = config_entry.data.get("ip_address")
    temperature_entity = config_entry.data.get("temperature_entity")  # Get temperature entity if provided
    model = config_entry.data.get("model")  # Get temperature entity if provided
    convector = TesyConvector(ip_address, model)
    async_add_entities([TesyConvectorClimate(convector, temperature_entity)])

    async def handle_set_opened_window(call):
        """Handle the service call."""
        entity_id = call.data.get("entity_id")
        status = call.data.get("status")

        climate_entity = hass.data[DOMAIN][config_entry.entry_id]
        await climate_entity.async_set_opened_window(status)

    hass.services.async_register(DOMAIN, "set_opened_window", handle_set_opened_window)


class TesyConvectorClimate(ClimateEntity):
    """Representation of a Tesy Convector as a ClimateEntity."""

    def __init__(self, convector, temperature_entity=None):
        """Initialize the climate entity."""
        self._remove_update_listener = None
        self.convector = convector
        self.temperature_entity = temperature_entity
        self._attr_name = "Tesy Convector"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS  # Updated to use UnitOfTemperature.CELSIUS
        # Explicitly declare both TARGET_TEMPERATURE
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        )
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
        self._attr_min_temp = 10
        self._attr_max_temp = 30
        self._attr_target_temperature_step = 1
        self._hvac_mode = HVACMode.OFF  # Default to off initially
        self._current_temp = None  # Variable to store current temperature
        self._target_temp = None  # Variable to store target temperature
        # Generate unique ID using model and IP address
        self._attr_unique_id = f"{convector.model}_{convector.ip_address}"

    async def async_update(self, *args):
        """Fetch new state data for this entity."""
        if self.temperature_entity:
            # Get the temperature from the specified entity
            temp_state = self.hass.states.get(self.temperature_entity)
            if temp_state:
                self._current_temp = float(temp_state.state)
        else:
            # Fallback to using the convector's temperature
            status = await self.convector.get_status()
            self._current_temp = status['payload']['setTemp']['payload']['temp']

        """Fetch new state data for this entity."""
        status = await self.convector.get_status()

        # Log the full response for debugging
        _LOGGER.debug("Tesy Convector status: %s", status)

        # Check if 'payload' exists before accessing it
        # Check if the heater is on and in "program" mode
        if 'payload' in status and 'onOff' in status['payload'] and 'status' in status['payload']['onOff']['payload']:
            if status['payload']['onOff']['payload']['status'] == 'on':
                # Check the current mode
                current_mode = status['payload']['setMode']['payload']['name']  # Assuming the mode is in 'setMode'
                if current_mode == 'program':
                    self._hvac_mode = HVACMode.AUTO  # Set to AUTO if in program mode
                else:
                    self._hvac_mode = HVACMode.HEAT  # Otherwise, set to HEAT
            else:
                self._hvac_mode = HVACMode.OFF
            self._target_temp = status['payload']['setTemp']['payload']['temp']  # Set target temperature
        else:
            _LOGGER.error("Unexpected response structure from Tesy Convector: %s", status)
            # Optionally set default values in case of an unexpected structure
            self._hvac_mode = HVACMode.OFF
            self._target_temp = None

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

    async def async_added_to_hass(self):
        """Run when entity is added to Home Assistant."""
        # Update the state every 20 seconds
        self._remove_update_listener = async_track_time_interval(
            self.hass, self.async_update, timedelta(seconds=10)
        )

    async def async_set_hvac_mode(self, hvac_mode):
        """Set the HVAC mode (on/off)."""
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
