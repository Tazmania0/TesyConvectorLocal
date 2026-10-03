"""Read-only controller diagnostics from the climate entity's cached state."""

from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_CLOUD_TELEMETRY_ENABLED, CONF_SW_CONTROL_ENABLED, DOMAIN

# These polls only publish cached values; they never query the heater or sensor.
SCAN_INTERVAL = timedelta(seconds=10)
_DELTA_CLASS = getattr(SensorDeviceClass, "TEMPERATURE_DELTA", None)
_DIAGNOSTIC_ICONS = {
    "sw_control_state": "mdi:thermostat",
    "external_temperature_error": "mdi:thermometer-alert",
    "external_temp_age_sec": "mdi:timer-outline",
    "duty_cycle_pct": "mdi:percent",
    "ramp_rate_c_per_min": "mdi:trending-up",
    "overshoot_correction": "mdi:thermometer",
    "i_correction": "mdi:tune",
    "cloud_telemetry_age_sec": "mdi:timer-outline",
}

DIAGNOSTIC_SENSORS = (
    SensorEntityDescription(
        key="cloud_current_temp", name="Cloud device temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE, suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="cloud_telemetry_age_sec", name="Cloud heating report age",
        native_unit_of_measurement="s", suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key="sw_control_state", name="Controller state",
        device_class=SensorDeviceClass.ENUM,
        options=["SW_DISABLED", "SW_WAITING_FOR_SENSOR", "SW_IDLE", "SW_HEATING", "WINDOW_INHIBIT"],
    ),
    SensorEntityDescription(
        key="raw_external_temp", name="External temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="filtered_external_temp", name="Filtered external temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="internal_temp", name="Internal temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="external_temperature_error", name="Room temperature error",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=_DELTA_CLASS,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="external_temp_age_sec", name="External sensor report age",
        native_unit_of_measurement="s",
        suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key="duty_cycle_pct", name="Requested duty cycle",
        native_unit_of_measurement="%",
        suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key="ramp_rate_c_per_min", name="Temperature ramp rate",
        native_unit_of_measurement="°C/min",
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key="predicted_temp", name="Predicted temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="effective_off_threshold", name="Effective OFF threshold",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key="overshoot_correction", name="Learned overshoot correction",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=_DELTA_CLASS,
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key="i_correction", name="Average temperature correction",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=_DELTA_CLASS,
        suggested_display_precision=3,
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    """Expose diagnostics even if software control is enabled later."""
    climate = hass.data[DOMAIN][entry.entry_id]["climate_entity"]
    async_add_entities([
        TesyControllerDiagnosticSensor(climate, entry, description)
        for description in DIAGNOSTIC_SENSORS
    ])


class TesyControllerDiagnosticSensor(SensorEntity):
    """Publish an existing calculation without running the controller again."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, climate, entry, description):
        self._climate = climate
        self._entry = entry
        self.entity_description = description
        self._attr_icon = _DIAGNOSTIC_ICONS.get(description.key)
        self._attr_unique_id = f"{entry.entry_id}_diagnostic_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Tesy", model=climate.convector.model,
        )

    @property
    def suggested_object_id(self):
        """Give Recorder filters a stable diagnostic-only naming pattern."""
        return f"tesy_diagnostic_{self._entry.entry_id}_{self.entity_description.key}"

    @property
    def available(self):
        if self.entity_description.key.startswith("cloud_"):
            return self._entry.options.get(CONF_CLOUD_TELEMETRY_ENABLED, False)
        if not self._climate.available:
            return False
        return self.entity_description.key == "sw_control_state" or self._entry.options.get(
            CONF_SW_CONTROL_ENABLED, False
        )

    @property
    def native_value(self):
        value = self._climate.extra_state_attributes.get(self.entity_description.key)
        if value is None and self.entity_description.key == "sw_control_state":
            return "SW_DISABLED"
        return value
