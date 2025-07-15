"""Number platform for Tesy Convector Local integration.

Defines the temperature correction number entity for Tesy Convector devices.
"""

from homeassistant.components.number import NumberEntity
from homeassistant.core import HomeAssistant

from .const import CONF_MODEL, CONF_TEMPERATURE_CORRECTION, DOMAIN


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set up temperature correction number entity for Tesy Convector."""
    # device = hass.data[DOMAIN][config_entry.entry_id]
    device = hass.data[DOMAIN][config_entry.entry_id]["device"]
    async_add_entities([TesyTemperatureCorrectionNumber(device, config_entry)])


class TesyTemperatureCorrectionNumber(NumberEntity):
    """Number entity for Tesy Convector temperature correction."""

    def __init__(self, device, config_entry) -> None:
        """Initialize the temperature correction number entity."""
        self._device = device
        self._config_entry = config_entry

        # Generate model-based entity ID (e.g., number.ht_2000_temperature_correction)
        model = getattr(device, CONF_MODEL, None)
        if model:
            model = model.replace(" ", "_").lower()
        else:
            model = "unknown"
        self._attr_entity_id = f"number.{model}_{CONF_TEMPERATURE_CORRECTION}"
        # self._attr_entity_id = f"number.{CONF_TEMPERATURE_CORRECTION}"

        # Maintain unique ID for entity registry
        self._attr_unique_id = f"{config_entry.entry_id}_temp_correction"

        # Device association
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "model": getattr(device, CONF_MODEL, "Unknown"),
            "manufacturer": "Tesy",
        }

        self._attr_native_min_value = -4
        self._attr_native_max_value = 4
        self._attr_native_step = 1

    @property
    def native_value(self):
        """Return the current temperature correction value."""
        return self._config_entry.options.get(CONF_TEMPERATURE_CORRECTION, 0)

    async def async_set_native_value(self, value: float):
        """Send correction value to device and update config entry."""
        await self._device.set_temperature_correction(int(value))

        # Update config entry
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            options={
                **self._config_entry.options,
                CONF_TEMPERATURE_CORRECTION: int(value),
            },
        )

    @property
    def name(self):
        """Return user-friendly name for UI."""
        return f"{self._device.model} Temperature Correction"
