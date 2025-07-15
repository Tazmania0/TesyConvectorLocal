"""Expose Window Open status as a binary sensor for Tesy Convector Local integration."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_MODEL, DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Initialisation function for the binary sensor."""
    climate_entity = hass.data[DOMAIN][entry.entry_id]["climate_entity"]
    async_add_entities([WindowOpenBinarySensor(climate_entity, entry)])


class WindowOpenBinarySensor(BinarySensorEntity):
    """Window opening sensor device class for Tesy Convector Local integration."""

    def __init__(self, climate_entity, entry) -> None:
        """Sensor device name initialization."""
        self._climate = climate_entity
        self._entry = entry

        model = getattr(climate_entity.convector, CONF_MODEL, None)
        if model:
            model_slug = model.replace(" ", "_").lower()
        else:
            model = "Tesy Convector"
            model_slug = "unknown"

        entry_id = entry.entry_id
        if entry_id is None:
            raise ValueError(
                "entry.entry_id is None — this will break device_info identifiers"
            )

        # Match naming with number.py
        self._attr_entity_id = f"binary_sensor.{model_slug}_window_open"
        self._attr_name = f"{model} Window Open"

        self._attr_unique_id = f"{entry.entry_id}_window_open"
        self._attr_device_class = BinarySensorDeviceClass.WINDOW

        # Grouped under same device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Tesy",
            model=model,
        )

    @property
    def is_on(self) -> bool:
        """Return the current window open status."""
        return self._climate.is_window_opened

    @property
    def available(self) -> bool:
        """Return the availability of the sensor."""
        return self._climate.available if hasattr(self._climate, "available") else True
