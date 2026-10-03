import asyncio
from fnmatch import fnmatchcase
import importlib.util
import sys
import types
from pathlib import Path
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock


class _NumberEntity:
    pass


class _BinarySensorEntity:
    pass


class _SensorEntity:
    pass


@dataclass
class _SensorEntityDescription:
    key: str
    name: str
    device_class: str | None = None
    options: list | None = None
    native_unit_of_measurement: str | None = None
    state_class: str | None = None
    suggested_display_precision: int | None = None


class _ConfigEntry:
    pass


class _DeviceInfo(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class _BinarySensorDeviceClass:
    WINDOW = "window"


sys.modules.setdefault(
    "homeassistant.const", types.SimpleNamespace(
        EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
        UnitOfTemperature=types.SimpleNamespace(CELSIUS="°C"),
    )
)
sys.modules.setdefault(
    "homeassistant.components.sensor", types.SimpleNamespace(
        SensorEntity=_SensorEntity, SensorEntityDescription=_SensorEntityDescription,
        SensorDeviceClass=types.SimpleNamespace(TEMPERATURE="temperature", TEMPERATURE_DELTA="temperature_delta", ENUM="enum"),
        SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"),
    )
)

# Minimal Home Assistant module stubs used by tested modules.
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.core", types.SimpleNamespace(HomeAssistant=object))
sys.modules.setdefault(
    "homeassistant.config_entries", types.SimpleNamespace(ConfigEntry=_ConfigEntry)
)
sys.modules.setdefault(
    "homeassistant.components.number", types.SimpleNamespace(NumberEntity=_NumberEntity)
)
sys.modules.setdefault(
    "homeassistant.components.binary_sensor",
    types.SimpleNamespace(
        BinarySensorEntity=_BinarySensorEntity,
        BinarySensorDeviceClass=_BinarySensorDeviceClass,
    ),
)
sys.modules.setdefault(
    "homeassistant.helpers.entity", types.SimpleNamespace(DeviceInfo=_DeviceInfo)
)
sys.modules.setdefault(
    "homeassistant.helpers.aiohttp_client",
    types.SimpleNamespace(async_get_clientsession=lambda hass: object()),
)
sys.modules.setdefault(
    "aiohttp",
    types.SimpleNamespace(ClientError=Exception, ContentTypeError=Exception, ClientSession=object),
)


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.tesy_convector_local"


# Ensure package shells exist so relative imports work when loading from file paths.
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
subpkg = sys.modules.setdefault(PKG_NAME, types.ModuleType(PKG_NAME))
subpkg.__path__ = [str(ROOT / "custom_components" / "tesy_convector_local")]


def _load_module(module_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


const = _load_module(f"{PKG_NAME}.const", "custom_components/tesy_convector_local/const.py")
number = _load_module(f"{PKG_NAME}.number", "custom_components/tesy_convector_local/number.py")
binary_sensor = _load_module(
    f"{PKG_NAME}.binary_sensor", "custom_components/tesy_convector_local/binary_sensor.py"
)
sensor = _load_module(f"{PKG_NAME}.sensor", "custom_components/tesy_convector_local/sensor.py")
integration = _load_module(f"{PKG_NAME}.__init__", "custom_components/tesy_convector_local/__init__.py")


class _DummyConfigEntries:
    def __init__(self):
        self.calls = []

    def async_update_entry(self, entry, options):
        self.calls.append((entry, options))


class _DummyConfigEntryManager:
    def __init__(self):
        self.forward_calls = []
        self.unload_calls = []

    async def async_forward_entry_setups(self, entry, platforms):
        self.forward_calls.append((entry, platforms))

    async def async_unload_platforms(self, entry, platforms):
        self.unload_calls.append((entry, platforms))
        return True


class _DummyHass:
    def __init__(self):
        self.config_entries = _DummyConfigEntries()
        self.data = {const.DOMAIN: {}}


class _DummyDevice:
    def __init__(self, model="CN03"):
        self.model = model
        self.calls = []

    async def set_temperature_correction(self, value):
        self.calls.append(value)


class _DummyEntry:
    def __init__(self, entry_id="entry-1", options=None, data=None):
        self.entry_id = entry_id
        self.options = options or {}
        self.data = data or {}
        self.listeners = []

    def add_update_listener(self, listener):
        self.listeners.append(listener)
        return listener

    def async_on_unload(self, callback):
        self._on_unload = callback
        return callback


class _DummyClimate:
    def __init__(self, is_window_opened=False, available=True, model="CN03"):
        self.is_window_opened = is_window_opened
        self.available = available
        self.convector = types.SimpleNamespace(model=model)


def test_temperature_correction_entity_reads_and_updates_value():
    entry = _DummyEntry(options={const.CONF_TEMPERATURE_CORRECTION: 2})
    device = _DummyDevice(model="CN03")
    entity = number.TesyTemperatureCorrectionNumber(device, entry)
    entity.hass = _DummyHass()

    assert entity.native_value == 2

    asyncio.run(entity.async_set_native_value(3.7))

    # device receives integer correction and options are persisted as integer
    assert device.calls == [3]
    assert entity.hass.config_entries.calls == [
        (
            entry,
            {const.CONF_TEMPERATURE_CORRECTION: 3},
        )
    ]


def test_temperature_correction_entity_defaults_and_name():
    entry = _DummyEntry(options={})
    device = _DummyDevice(model="CN06AS")
    entity = number.TesyTemperatureCorrectionNumber(device, entry)

    assert entity.native_value == 0
    assert entity.name == "CN06AS Temperature Correction"


def test_window_binary_sensor_state_and_availability_follow_climate():
    climate = _DummyClimate(is_window_opened=True, available=False, model="Tesy X")
    entry = _DummyEntry(entry_id="abc")

    sensor = binary_sensor.WindowOpenBinarySensor(climate, entry)

    assert sensor.is_on is True
    assert sensor.available is False
    assert sensor._attr_unique_id == "abc_window_open"
    assert sensor._attr_name == "Tesy X Window Open"


def test_binary_sensor_async_setup_entry_adds_entity_when_climate_present():
    hass = _DummyHass()
    entry = _DummyEntry(entry_id="entry-42")
    climate = _DummyClimate()
    hass.data[const.DOMAIN][entry.entry_id] = {"climate_entity": climate}
    added = []

    def _add_entities(entities):
        added.extend(entities)

    asyncio.run(binary_sensor.async_setup_entry(hass, entry, _add_entities))

    assert len(added) == 7
    assert isinstance(added[0], binary_sensor.WindowOpenBinarySensor)


def test_binary_sensor_async_setup_entry_noop_without_climate_entity():
    hass = _DummyHass()
    entry = _DummyEntry(entry_id="entry-43")
    hass.data[const.DOMAIN][entry.entry_id] = {}
    added = []

    asyncio.run(
        binary_sensor.async_setup_entry(
            hass, entry, lambda entities: added.extend(entities)
        )
    )

    assert added == []


def test_integration_setup_entry_applies_initial_correction_and_forwards_platforms():
    device = _DummyDevice()
    entry = _DummyEntry(
        entry_id="entry-55",
        data={const.CONF_IP_ADDRESS: "192.168.1.5", const.CONF_MODEL: "CN03"},
        options={const.CONF_TEMPERATURE_CORRECTION: 2},
    )

    # Replace runtime dependencies inside integration module.
    integration.async_get_clientsession = lambda hass: object()
    integration.TesyConvector = lambda ip, model, session: device

    hass = types.SimpleNamespace(
        data={},
        config_entries=_DummyConfigEntryManager(),
    )

    result = asyncio.run(integration.async_setup_entry(hass, entry))

    assert result is True
    assert device.calls == [2]
    assert hass.data[const.DOMAIN][entry.entry_id]["device"] is device
    assert hass.config_entries.forward_calls == [
        (entry, ["climate"]),
        (entry, ["number", "binary_sensor", "sensor"]),
    ]
    assert entry.listeners == [integration._async_options_updated]


def test_integration_options_update_only_writes_when_correction_present():
    device = _DummyDevice()
    hass = types.SimpleNamespace(data={const.DOMAIN: {"entry-1": {"device": device}}})

    entry_with_correction = _DummyEntry(
        entry_id="entry-1", options={const.CONF_TEMPERATURE_CORRECTION: -1}
    )
    asyncio.run(integration._async_options_updated(hass, entry_with_correction))

    entry_without_correction = _DummyEntry(entry_id="entry-1", options={})
    asyncio.run(integration._async_options_updated(hass, entry_without_correction))

    assert device.calls == [-1]


def test_window_service_removed_only_after_last_entry_unloads():
    entries = {'a': {'device': object()}, 'b': {'device': object()}}
    services = types.SimpleNamespace(has_service=lambda *args: True, async_remove=Mock())
    hass = types.SimpleNamespace(data={const.DOMAIN: entries}, services=services, config_entries=_DummyConfigEntryManager())
    assert asyncio.run(integration.async_unload_entry(hass, _DummyEntry(entry_id='a')))
    services.async_remove.assert_not_called()
    assert asyncio.run(integration.async_unload_entry(hass, _DummyEntry(entry_id='b')))
    services.async_remove.assert_called_once_with(const.DOMAIN, 'set_opened_window')


def _diagnostic_climate(attributes=None, available=True):
    return types.SimpleNamespace(
        convector=types.SimpleNamespace(model='CN06AS'), available=available,
        extra_state_attributes=attributes or {},
    )


def test_diagnostic_sensor_setup_groups_enabled_entities_with_heater():
    entry = _DummyEntry(options={const.CONF_SW_CONTROL_ENABLED: True})
    climate = _diagnostic_climate({'sw_control_state': 'SW_HEATING', 'duty_cycle_pct': 67})
    hass = types.SimpleNamespace(data={const.DOMAIN: {entry.entry_id: {'climate_entity': climate}}})
    added = []
    asyncio.run(sensor.async_setup_entry(hass, entry, added.extend))
    assert len(added) == 22
    assert len({item._attr_unique_id for item in added}) == len(added)
    for item in added:
        assert item._attr_entity_category == 'diagnostic'
        assert item._attr_entity_registry_enabled_default is True
        assert item._attr_device_info['identifiers'] == {(const.DOMAIN, entry.entry_id)}
        assert item.available is (not item.entity_description.key.startswith('cloud_'))
        assert fnmatchcase(f'sensor.{item.suggested_object_id}', 'sensor.*tesy_diagnostic_*')
        assert item.entity_description.state_class is None
    by_key = {item.entity_description.key: item for item in added}
    assert by_key['duty_cycle_pct'].native_value == 67
    assert by_key['duty_cycle_pct'].entity_description.native_unit_of_measurement == '%'
    assert by_key['sw_control_state'].native_value == 'SW_HEATING'
    assert by_key['filtered_external_temp'].native_value is None
    climate.extra_state_attributes['duty_cycle_pct'] = 50
    assert by_key['duty_cycle_pct'].native_value == 50
    climate.available = False
    assert all(not item.available for item in added)


def test_diagnostics_support_enabling_software_control_without_reload():
    entry = _DummyEntry()
    climate = _diagnostic_climate()
    entities = {description.key: sensor.TesyControllerDiagnosticSensor(climate, entry, description)
                for description in sensor.DIAGNOSTIC_SENSORS}
    assert entities['sw_control_state'].available
    assert entities['sw_control_state'].native_value == 'SW_DISABLED'
    assert not entities['duty_cycle_pct'].available
    entry.options[const.CONF_SW_CONTROL_ENABLED] = True
    climate.extra_state_attributes.update({'sw_control_state': 'SW_IDLE', 'duty_cycle_pct': 42})
    assert entities['duty_cycle_pct'].available
    assert entities['duty_cycle_pct'].native_value == 42
    assert entities['sw_control_state'].native_value == 'SW_IDLE'


def test_temperature_differences_are_not_absolute_temperature_sensors():
    descriptions = {description.key: description for description in sensor.DIAGNOSTIC_SENSORS}
    for key in ('overshoot_correction', 'i_correction', 'external_temperature_error'):
        assert descriptions[key].device_class == 'temperature_delta'
    assert descriptions['filtered_external_temp'].device_class == 'temperature'


def test_controller_binary_diagnostics_follow_flags_and_unknown_values():
    entry = _DummyEntry(options={const.CONF_SW_CONTROL_ENABLED: True})
    climate = _diagnostic_climate({'external_temp_valid': True, 'firmware_fallback_active': False})
    entities = {key: binary_sensor.TesyControllerDiagnosticBinarySensor(climate, entry, key, name, icon)
                for key, name, icon in binary_sensor.DIAGNOSTIC_BINARY_SENSORS}
    assert entities['external_temp_valid'].is_on is True
    assert entities['firmware_fallback_active'].is_on is False
    assert entities['firmware_limit_estimated'].is_on is None
    climate.extra_state_attributes.update({'external_temp_valid': False, 'firmware_fallback_active': True})
    assert entities['external_temp_valid'].is_on is False
    assert entities['firmware_fallback_active'].is_on is True
    assert all(item._attr_entity_registry_enabled_default for item in entities.values())
    assert all(fnmatchcase(f'binary_sensor.{item.suggested_object_id}',
                          'binary_sensor.*tesy_diagnostic_*') for item in entities.values())
    entry.options[const.CONF_SW_CONTROL_ENABLED] = False
    assert all(not item.available for item in entities.values())


def test_diagnostic_entities_do_not_make_device_or_external_sensor_requests():
    device = types.SimpleNamespace(model='CN06AS', get_status=Mock())
    climate = types.SimpleNamespace(convector=device, available=True, extra_state_attributes={})
    entry = _DummyEntry(options={const.CONF_SW_CONTROL_ENABLED: True})
    numeric = sensor.TesyControllerDiagnosticSensor(climate, entry, next(d for d in sensor.DIAGNOSTIC_SENSORS if d.key == 'sw_control_state'))
    binary = binary_sensor.TesyControllerDiagnosticBinarySensor(climate, entry, 'external_temp_valid', 'Valid', 'mdi:thermometer')
    for _ in range(3):
        assert numeric.available
        numeric.native_value
        assert binary.available
        binary.is_on
    device.get_status.assert_not_called()


def test_cloud_diagnostics_do_not_depend_on_software_control_or_local_availability():
    entry = _DummyEntry(options={const.CONF_CLOUD_TELEMETRY_ENABLED: True})
    climate = _diagnostic_climate({'cloud_heating': False, 'cloud_current_temp': 23.0}, available=False)
    description = next(d for d in sensor.DIAGNOSTIC_SENSORS if d.key == 'cloud_current_temp')
    numeric = sensor.TesyControllerDiagnosticSensor(climate, entry, description)
    binary = binary_sensor.TesyControllerDiagnosticBinarySensor(climate, entry, 'cloud_heating', 'Heating', 'mdi:radiator')
    assert numeric.available and numeric.native_value == 23.0
    assert binary.available and binary.is_on is False
    climate.extra_state_attributes['cloud_heating'] = None
    climate.extra_state_attributes['cloud_current_temp'] = None
    assert binary.is_on is None
    assert numeric.native_value is None
    entry.options[const.CONF_CLOUD_TELEMETRY_ENABLED] = False
    assert not numeric.available and not binary.available


def test_cloud_options_start_replace_and_stop_subscription_without_resetting_controller(monkeypatch):
    from custom_components.tesy_convector_local import cloud
    from test_cloud import config

    instances = []
    class Telemetry:
        def __init__(self, settings, on_update):
            self.config = settings
            self.start = Mock()
            self.stop = AsyncMock()
            instances.append(self)

    monkeypatch.setattr(cloud, 'CloudTelemetry', Telemetry)
    climate = types.SimpleNamespace(
        _update_hvac_action=Mock(), async_write_ha_state=Mock(), _reset_sw_controller=Mock(),
    )
    entry = _DummyEntry(data={const.CONF_CLOUD_TELEMETRY: config()},
                        options={const.CONF_CLOUD_TELEMETRY_ENABLED: True})
    data = {'climate_entity': climate}
    hass = types.SimpleNamespace(data={const.DOMAIN: {entry.entry_id: data}})
    asyncio.run(integration._async_sync_cloud_telemetry(hass, entry))
    assert climate._cloud_telemetry is instances[0]
    asyncio.run(integration._async_sync_cloud_telemetry(hass, entry))
    instances[0].start.assert_called_once()
    entry.data[const.CONF_CLOUD_TELEMETRY] = config(token='replacement-token')
    asyncio.run(integration._async_sync_cloud_telemetry(hass, entry))
    instances[0].stop.assert_awaited_once()
    assert climate._cloud_telemetry is instances[1]
    entry.options[const.CONF_CLOUD_TELEMETRY_ENABLED] = False
    asyncio.run(integration._async_sync_cloud_telemetry(hass, entry))
    instances[1].stop.assert_awaited_once()
    assert climate._cloud_telemetry is None
    assert 'cloud_telemetry' not in data
    climate._reset_sw_controller.assert_not_called()


def test_invalid_cloud_configuration_does_not_block_local_setup():
    climate = types.SimpleNamespace(_update_hvac_action=Mock(), async_write_ha_state=Mock())
    entry = _DummyEntry(data={const.CONF_CLOUD_TELEMETRY: {}},
                        options={const.CONF_CLOUD_TELEMETRY_ENABLED: True})
    data = {'climate_entity': climate}
    hass = types.SimpleNamespace(data={const.DOMAIN: {entry.entry_id: data}})
    asyncio.run(integration._async_sync_cloud_telemetry(hass, entry))
    assert climate._cloud_telemetry is None
    assert 'cloud_telemetry' not in data


def test_unload_stops_cloud_subscription_before_removing_entities():
    telemetry = types.SimpleNamespace(stop=AsyncMock(), start=Mock())
    entry = _DummyEntry()
    async def unload(entry, platforms):
        telemetry.stop.assert_awaited_once()
        return True
    hass = types.SimpleNamespace(
        data={const.DOMAIN: {entry.entry_id: {'cloud_telemetry': telemetry}}},
        config_entries=types.SimpleNamespace(async_unload_platforms=unload),
        services=types.SimpleNamespace(has_service=Mock(return_value=False)),
    )
    assert asyncio.run(integration.async_unload_entry(hass, entry))
    assert entry.entry_id not in hass.data[const.DOMAIN]
    telemetry.start.assert_not_called()
