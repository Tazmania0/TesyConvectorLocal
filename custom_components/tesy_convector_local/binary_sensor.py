"""Expose Window Open status as a binary sensor for Tesy Convector Local integration."""

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_SW_CONTROL_ENABLED, DOMAIN

SCAN_INTERVAL = timedelta(seconds=10)

DIAGNOSTIC_BINARY_SENSORS = (
    ("external_temp_valid", "External temperature valid", "mdi:thermometer-check"),
    ("firmware_fallback_active", "Firmware fallback active", "mdi:shield-check"),
    ("sw_heat_requested", "Software heating requested", "mdi:radiator"),
    ("firmware_limit_estimated", "Firmware limiting estimated", "mdi:thermometer-alert"),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up the window open binary sensor."""
    # climate_entity is stored in hass.data by climate.async_setup_entry
    # BEFORE async_add_entities is called, so this lookup is safe.
    climate_entity = hass.data[DOMAIN][entry.entry_id].get("climate_entity")
    if climate_entity is None:
        # Integration setup establishes climate before the other platforms,
        # but guard defensively. Cannot raise ConfigEntryNotReady from a platform
        # setup — HA won't retry it. Log and bail instead.
        import logging
        logging.getLogger(__name__).error(
            "binary_sensor setup: climate_entity not found for entry %s — "
            "ensure 'climate' is set up before 'binary_sensor'",
            entry.entry_id,
        )
        return

    async_add_entities([
        WindowOpenBinarySensor(climate_entity, entry),
        *[
            TesyControllerDiagnosticBinarySensor(climate_entity, entry, key, name, icon)
            for key, name, icon in DIAGNOSTIC_BINARY_SENSORS
        ],
    ])


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


class TesyControllerDiagnosticBinarySensor(BinarySensorEntity):
    """Read controller flags from memory without querying the device."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, climate, entry, key, name, icon):
        self._climate = climate
        self._entry = entry
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_diagnostic_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Tesy", model=climate.convector.model,
        )

    @property
    def suggested_object_id(self):
        """Give Recorder filters a stable diagnostic-only naming pattern."""
        return f"tesy_diagnostic_{self._entry.entry_id}_{self._key}"

    @property
    def available(self):
        return self._climate.available and self._entry.options.get(CONF_SW_CONTROL_ENABLED, False)

    @property
    def is_on(self):
        return self._climate.extra_state_attributes.get(self._key)
