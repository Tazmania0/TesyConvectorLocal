"""Expose Window Open status as a binary sensor for Tesy Convector Local integration."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up the window open binary sensor."""
    # climate_entity is stored in hass.data by climate.async_setup_entry
    # BEFORE async_add_entities is called, so this lookup is safe.
    climate_entity = hass.data[DOMAIN][entry.entry_id].get("climate_entity")
    if climate_entity is None:
        # Should not happen if platform order is [climate, number, binary_sensor],
        # but guard defensively. Cannot raise ConfigEntryNotReady from a platform
        # setup — HA won't retry it. Log and bail instead.
        import logging
        logging.getLogger(__name__).error(
            "binary_sensor setup: climate_entity not found for entry %s — "
            "ensure 'climate' is set up before 'binary_sensor'",
            entry.entry_id,
        )
        return

    async_add_entities([WindowOpenBinarySensor(climate_entity, entry)])


class WindowOpenBinarySensor(BinarySensorEntity):
    """Binary sensor reporting whether the window is detected as open."""

    def __init__(self, climate_entity, entry) -> None:
        """Initialise sensor."""
        self._climate = climate_entity

        model = getattr(climate_entity.convector, "model", None) or "Tesy Convector"
        model_slug = model.replace(" ", "_").lower()

        self._attr_name = f"{model} Window Open"
        self._attr_unique_id = f"{entry.entry_id}_window_open"
        self._attr_device_class = BinarySensorDeviceClass.WINDOW
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Tesy",
            model=model,
        )
        # FIX: Removed manual _attr_entity_id assignment.
        # Entity IDs are managed by the HA entity registry; manually setting
        # _attr_entity_id bypasses that and can cause conflicts on rename.

    @property
    def is_on(self) -> bool:
        """Return True when window is detected open."""
        return self._climate.is_window_opened

    @property
    def available(self) -> bool:
        """Mirror availability of the parent climate entity."""
        return getattr(self._climate, "available", True)
