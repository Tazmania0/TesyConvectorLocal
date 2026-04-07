import asyncio
import importlib.util
import sys
import types
from pathlib import Path


class _NumberEntity:
    pass


class _BinarySensorEntity:
    pass


class _ConfigEntry:
    pass


class _DeviceInfo(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class _BinarySensorDeviceClass:
    WINDOW = "window"


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

    assert len(added) == 1
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
        (entry, ["climate", "number", "binary_sensor"])
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
