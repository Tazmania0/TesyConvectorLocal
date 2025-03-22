from homeassistant.components.number import NumberEntity
from homeassistant.util import slugify

from .const import DOMAIN

async def async_setup_entry(hass, config_entry, async_add_entities):
    device = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([TesyTemperatureCorrectionNumber(device, config_entry)])

class TesyTemperatureCorrectionNumber(NumberEntity):
    def __init__(self, device, config_entry):
        self._device = device
        self._config_entry = config_entry
        

        # Generate model-based entity ID (e.g., number.ht_2000_temperature_correction)
        model = device.model.replace(" ", "_").lower()  # Explicit sanitization
        self._attr_entity_id = f"number.{model}_temperature_correction"
        
        # Maintain unique ID for entity registry
        self._attr_unique_id = f"{config_entry.entry_id}_temp_correction"
        

        # Device association
        self._attr_device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "model": device.model,
            "manufacturer": "Tesy"
        }
        
        self._attr_native_min_value = -4
        self._attr_native_max_value = 4
        self._attr_native_step = 1

    @property
    def native_value(self):
        return self._config_entry.options.get("temperature_correction", 0)

    async def async_set_native_value(self, value: float):
        """Directly send correction to device"""
        await self._device.set_temperature_correction(int(value))
        
        # Update config entry
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            options={**self._config_entry.options, "temperature_correction": int(value)}
        )

    @property
    def name(self):
        """User-friendly name in UI"""
        return f"{self._device.model} Temperature Correction"
