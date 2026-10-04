"""Climate control regressions with a deterministic clock and local HA stubs.

The repository's tests run without installing a complete Home Assistant runtime.
Device mocks update their reported state so repeated polls exercise ownership.
"""
import asyncio
import importlib.util
import sys
import types
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from enum import IntFlag, StrEnum
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest


class HVACMode(StrEnum):
    HEAT = 'heat'
    OFF = 'off'
    AUTO = 'auto'


class HVACAction(StrEnum):
    HEATING = 'heating'
    IDLE = 'idle'
    OFF = 'off'


class ClimateEntityFeature(IntFlag):
    TARGET_TEMPERATURE = 1
    TURN_OFF = 2
    TURN_ON = 4


class ClimateEntity:
    def async_write_ha_state(self):
        pass


class MemoryStore:
    """Model HA's per-entry persisted payload across new entity instances."""

    def __init__(self, hass, version, key):
        self.hass = hass
        self.key = key
        self.saves = 0
        if not hasattr(hass, 'storage'):
            hass.storage = {}

    async def async_load(self):
        return deepcopy(self.hass.storage.get(self.key))

    async def async_save(self, value):
        self.saves += 1
        self.hass.storage[self.key] = deepcopy(value)


sys.modules.setdefault('homeassistant', types.ModuleType('homeassistant'))
sys.modules.setdefault('homeassistant.core', types.SimpleNamespace(HomeAssistant=object))
sys.modules.setdefault('homeassistant.exceptions', types.SimpleNamespace(HomeAssistantError=type('HomeAssistantError', (Exception,), {})))
sys.modules.setdefault('homeassistant.components.climate', types.SimpleNamespace(ClimateEntity=ClimateEntity))
sys.modules.setdefault('homeassistant.components.climate.const', types.SimpleNamespace(
    HVACMode=HVACMode, HVACAction=HVACAction, ClimateEntityFeature=ClimateEntityFeature,
))
sys.modules.setdefault('homeassistant.const', types.SimpleNamespace(UnitOfTemperature=types.SimpleNamespace(CELSIUS='°C'), EntityCategory=types.SimpleNamespace(DIAGNOSTIC='diagnostic')))
sys.modules.setdefault('homeassistant.helpers', types.ModuleType('homeassistant.helpers'))
sys.modules.setdefault('homeassistant.helpers.config_validation', types.SimpleNamespace(entity_id=str))
sys.modules.setdefault('homeassistant.helpers.entity', types.SimpleNamespace(DeviceInfo=dict))
sys.modules.setdefault('homeassistant.helpers.event', types.SimpleNamespace(async_track_time_interval=lambda *args: lambda: None))
sys.modules.setdefault('homeassistant.helpers.storage', types.SimpleNamespace(Store=MemoryStore))
sys.modules.setdefault('aiohttp', types.SimpleNamespace(ClientError=Exception, ContentTypeError=Exception, ClientSession=object))
ROOT = Path(__file__).resolve().parents[1]
PKG = 'custom_components.tesy_convector_local'
sys.modules.setdefault('custom_components', types.ModuleType('custom_components'))
sys.modules.setdefault(PKG, types.ModuleType(PKG)).__path__ = [str(ROOT / 'custom_components/tesy_convector_local')]
spec = importlib.util.spec_from_file_location(f'{PKG}.climate', ROOT / 'custom_components/tesy_convector_local/climate.py')
climate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = climate
spec.loader.exec_module(climate)
from custom_components.tesy_convector_local import const


class Device:
    model = 'CN03'
    ip_address = '192.0.2.1'

    def __init__(self):
        self.setpoint = 21
        self.mode = 'heating'
        self.on = True
        self.calls = []

    async def set_temperature(self, value, **kwargs):
        self.calls.append(('temperature', value))
        self.setpoint = value
        return {}

    async def set_mode(self, mode, **kwargs):
        self.calls.append(('mode', mode))
        self.mode = mode
        self.on = True
        return {}

    async def turn_off(self, **kwargs):
        self.calls.append(('off',))
        self.on = False

    async def set_opened_window(self, value, **kwargs):
        self.calls.append(('window', value))

    async def get_status(self, **kwargs):
        return {'payload': {
            'onOff': {'payload': {'status': 'on' if self.on else 'off'}},
            'setMode': {'payload': {'name': self.mode}},
            'setTemp': {'payload': {'temp': self.setpoint}},
            'tempAir': {'payload': {'temp': 20}},
        }}


@pytest.fixture
def entity():
    entry = types.SimpleNamespace(entry_id='test', data={}, options={
        const.CONF_USE_EXTERNAL_TEMP: True,
        const.CONF_SW_CONTROL_ENABLED: True,
        const.CONF_TEMPERATURE_ENTITY: 'sensor.room',
        const.CONF_SW_EMA_ALPHA: 1.0,
        const.CONF_SW_I_GAIN: 0.0,
    })
    device = Device()
    entity = climate.TesyConvectorClimate(device, entry)
    entity.clock = 1000.0
    entity.sensor = sensor(21.6)
    entity.hass = types.SimpleNamespace(
        loop=types.SimpleNamespace(time=lambda: entity.clock),
        states=types.SimpleNamespace(get=Mock(side_effect=lambda _: entity.sensor)),
        config_entries=types.SimpleNamespace(async_update_entry=lambda entry, options: setattr(entry, 'options', options)),
    )
    entity._hvac_mode = HVACMode.HEAT
    entity._target_temp = 22.0
    return entity


def sensor(value, age=0, legacy=False):
    state = types.SimpleNamespace(state=str(value), last_updated=datetime.now(timezone.utc) - timedelta(seconds=age))
    if not legacy:
        state.last_reported = state.last_updated
    return state


def run(entity, temp, dt=60, **kwargs):
    entity.clock += dt
    defaults = dict(min_on_sec=60, min_off_sec=60, hysteresis=0.3, lag_time_min=0, i_gain=0)
    defaults.update(kwargs)
    asyncio.run(entity._run_sw_controller(current_temp=temp, **defaults))


def poll(entity, value, dt=10, age=0):
    entity.clock += dt
    entity.sensor = sensor(value, age)
    asyncio.run(entity.async_update())


def test_normal_control_and_dead_band(entity):
    run(entity, 21.6)
    assert entity._sw_device_on is True
    assert entity.convector.setpoint == 22
    before = len(entity.convector.calls)
    run(entity, 21.8)
    assert len(entity.convector.calls) == before
    run(entity, 22.0)
    assert entity._sw_device_on is False
    assert entity.convector.setpoint == 10
    before = len(entity.convector.calls)
    run(entity, 21.8)
    assert len(entity.convector.calls) == before
    assert not any(call[0] == 'off' for call in entity.convector.calls)


def test_transient_poll_failure_preserves_controller_and_recovers(entity):
    run(entity, 21.6)
    entity.convector.get_status = AsyncMock(return_value={'error': 'timeout'})
    poll(entity, 21.6)
    assert entity._attr_available
    assert entity._sw_device_on is True
    assert entity._comm_failures == 1
    entity.convector.get_status = Device.get_status.__get__(entity.convector)
    poll(entity, 21.6)
    assert entity._attr_available
    assert entity._comm_failures == 0
    assert entity._comm_failed_at is None


def test_repeated_poll_failures_mark_unavailable_and_back_off(entity):
    entity.convector.get_status = AsyncMock(return_value={'error': 'timeout'})
    for _ in range(3):
        poll(entity, 21.6)
    assert not entity._attr_available
    assert entity._sw_device_on is None
    poll(entity, 21.6)
    assert entity.convector.get_status.await_count == 3
    entity.convector.get_status = Device.get_status.__get__(entity.convector)
    poll(entity, 21.6, dt=30)
    assert entity._attr_available
    assert entity._comm_failures == 0


def cloud_feedback(entity, heating=False, temperature=22.0, age=0):
    entity._config_entry.options[const.CONF_CLOUD_TELEMETRY_ENABLED] = True
    entity._cloud_telemetry = types.SimpleNamespace(
        heating=heating, temperature=temperature, age=age, connected=True,
    )
    return entity._cloud_telemetry


def test_live_firmware_veto_raises_bounded_headroom_without_new_phase(entity):
    cloud_feedback(entity)
    run(entity, 21.0)
    started = entity._sw_last_on_time
    run(entity, 21.0, dt=60)
    assert entity.convector.setpoint == 22
    run(entity, 21.0, dt=60)
    assert entity.convector.setpoint == 23
    assert entity._sw_headroom == 1
    assert entity._sw_last_on_time == started
    assert entity._sw_duty_cycles == []
    for internal in (23, 24, 25, 25):
        entity._cloud_telemetry.temperature = internal
        run(entity, 21.0, dt=60)
        run(entity, 21.0, dt=60)
    assert entity.convector.setpoint == 25
    assert entity._sw_headroom == 3


def test_lost_cloud_restores_normal_ceiling_in_same_phase(entity):
    test_live_firmware_veto_raises_bounded_headroom_without_new_phase(entity)
    entity._cloud_telemetry.heating = None
    run(entity, 21.0, dt=10)
    assert entity.convector.setpoint == 22
    assert entity._sw_headroom == 0


def test_warm_room_trend_delays_headroom_boost(entity):
    cloud_feedback(entity)
    run(entity, 21.0, lag_time_min=6)
    for temp in (21.1, 21.2, 21.3):
        run(entity, temp, dt=30, lag_time_min=6)
    assert entity._sw_headroom == 0


def test_old_report_cannot_raise_ceiling(entity):
    cloud = cloud_feedback(entity)
    run(entity, 21.0)
    for _ in range(5):
        cloud.age += 60
        run(entity, 21.0, dt=60)
    assert entity._sw_headroom == 0


def test_cloud_cutoff_uses_internal_rate_and_learns_firmware_threshold(entity):
    cloud = cloud_feedback(entity, heating=True, temperature=21.0)
    run(entity, 21.0)
    cloud.temperature = 21.5
    run(entity, 21.0, dt=30)
    cloud.temperature = 22.0
    run(entity, 21.0, dt=30)
    assert entity._sw_cloud_internal_rise_rate == pytest.approx(1.0)
    cloud.heating = False
    cloud.temperature = 22.0
    run(entity, 21.0, dt=30)
    assert entity._sw_cloud_cutoff_offset == pytest.approx(0)
    cloud.heating = True
    cloud.temperature = 22.5
    run(entity, 21.0, dt=30)
    assert entity._sw_headroom == 1
    assert entity.convector.setpoint == 23


def test_missing_internal_temp_does_not_invent_cutoff_forecast(entity):
    cloud = cloud_feedback(entity, heating=True, temperature=None)
    run(entity, 21.0)
    cloud.heating = False
    run(entity, 21.0, dt=30)
    assert entity._sw_cloud_cutoff_offset is None
    assert entity._sw_cloud_cutoff_eta is None


@pytest.mark.parametrize('readings', [
    [22.0, 22.0, 22.5, 22.5, 22.5],
    [22.0, 22.5, 22.0, 22.5, 22.0],
    [22.0, 22.0, 22.0, 22.0, 22.0],
])
def test_one_cloud_rounding_step_or_plateau_does_not_trigger_forecast(entity, readings):
    cloud = cloud_feedback(entity, heating=True, temperature=readings[0])
    run(entity, 21.0)
    entity._sw_cloud_cutoff_offset = 0.0
    for internal in readings[1:]:
        cloud.temperature = internal
        run(entity, 21.0, dt=30)
        assert entity._sw_cloud_internal_rise_rate is None
        assert entity._sw_cloud_cutoff_eta is None
        assert entity._sw_headroom == 0


def test_cutoff_forecast_includes_rounding_uncertainty(entity):
    cloud = cloud_feedback(entity, heating=True, temperature=21.0)
    run(entity, 21.0)
    entity._sw_cloud_cutoff_offset = 0.0
    cloud.temperature = 21.5
    run(entity, 21.0, dt=30)
    cloud.temperature = 22.0
    run(entity, 21.0, dt=30)
    # Equal displayed temperatures still leave endpoint rounding uncertainty.
    assert entity._sw_cloud_cutoff_eta == pytest.approx(60.0)
    assert entity._sw_headroom == 0


def test_repeated_actual_vetoes_can_adapt_without_resolved_internal_rate(entity):
    cloud = cloud_feedback(entity, heating=True, temperature=21.5)
    run(entity, 21.0)
    for _ in range(4):
        cloud.heating = False
        cloud.temperature = 22.0
        run(entity, 21.0, dt=20)
        cloud.heating = True
        cloud.temperature = 21.5
        run(entity, 21.0, dt=20)
    assert entity._sw_cloud_internal_rise_rate is None
    assert entity._sw_headroom == 1
    assert entity.convector.setpoint == 23


def test_live_target_cutoff_overrides_minimum_on_timer(entity):
    cloud_feedback(entity, heating=True)
    run(entity, 21.6)
    run(entity, 22.0, dt=10)
    assert entity._sw_device_on is False
    assert entity.convector.setpoint == 10


def test_cloud_room_coast_learning_uses_actual_stop_and_positive_ramp(entity):
    cloud = cloud_feedback(entity, heating=True)
    run(entity, 21.0, lag_time_min=6)
    run(entity, 21.1, dt=60, lag_time_min=6)
    cloud.heating = False
    run(entity, 21.2, dt=60, lag_time_min=6)
    run(entity, 21.3, dt=60, lag_time_min=6)
    run(entity, 21.35, dt=60, lag_time_min=6)
    run(entity, 21.32, dt=60, lag_time_min=6)
    assert entity._sw_observed_coast_minutes == pytest.approx(5.1)
    entity._cloud_telemetry.heating = None
    run(entity, 21.32, dt=10, lag_time_min=6)
    assert entity._sw_observed_coast_minutes is None


def test_internal_heat_trail_tracks_peak_after_actual_stop(entity):
    cloud = cloud_feedback(entity, heating=True, temperature=21.0)
    run(entity, 21.0)
    cloud.heating = False
    cloud.temperature = 22.0
    run(entity, 21.0, dt=30)
    cloud.temperature = 22.5
    run(entity, 21.0, dt=30)
    cloud.temperature = 23.0
    run(entity, 21.0, dt=30)
    cloud.temperature = 22.5
    run(entity, 21.0, dt=30)
    assert entity._sw_internal_coast_rise == pytest.approx(1.0)
    assert entity._sw_internal_coast_seconds == 60


def test_cloud_resumption_discards_unfinished_heat_trail(entity):
    cloud = cloud_feedback(entity, heating=True, temperature=21.0)
    run(entity, 21.0)
    run(entity, 21.1, dt=30)
    cloud.heating = False
    run(entity, 21.2, dt=30)
    cloud.temperature = 22.4
    run(entity, 21.3, dt=30)
    cloud.heating = True
    run(entity, 21.4, dt=30)
    assert entity._sw_cloud_coast is None
    assert entity._sw_cloud_internal_coast is None
    assert entity._sw_observed_coast_minutes is None
    assert entity._sw_internal_coast_rise is None


def test_raising_firmware_ceiling_does_not_replace_room_target_on_poll(entity):
    cloud = cloud_feedback(entity)
    poll(entity, 21.0)
    for _ in range(10):
        cloud.temperature = entity.convector.setpoint
        poll(entity, 21.0, dt=60)
    assert entity._target_temp == 22.0
    assert entity.convector.setpoint == 25


def test_failed_headroom_write_does_not_claim_an_applied_adjustment(entity):
    cloud_feedback(entity)
    run(entity, 21.0)
    run(entity, 21.0, dt=60)
    entity.convector.set_temperature = AsyncMock(return_value={'error': 'timeout'})
    with pytest.raises(climate.DeviceCommandError):
        run(entity, 21.0, dt=60)
    assert entity._sw_headroom == 0
    assert entity._sw_applied_setpoint == 22


@pytest.mark.parametrize('target', [22.5, 29.0, 30.0])
def test_headroom_obeys_room_relative_and_device_limits(entity, target):
    entity._target_temp = target
    cloud = cloud_feedback(entity, temperature=target)
    run(entity, target - 1)
    for _ in range(10):
        cloud.temperature = entity.convector.setpoint
        run(entity, target - 1, dt=60)
    assert entity.convector.setpoint <= min(30, target + 3)


async def simulate_firmware_limited_room(entity, use_cloud):
    """Illustrative biased thermostat with delayed internal and room heating."""
    room, internal, output = 20.0, 21.5, 0.0
    heating = False
    cloud = cloud_feedback(entity) if use_cloud else None
    settled = []
    for now in range(0, 14400, 10):
        entity.clock = now
        if cloud is not None:
            cloud.heating = heating
            cloud.temperature = round(internal * 2) / 2
        await entity._run_sw_controller(room, 60, 60, 0.3, 6, 0)
        ceiling = entity.convector.setpoint
        for _ in range(10):
            if internal >= ceiling:
                heating = False
            elif internal <= ceiling - 0.3:
                heating = True
            output += (float(heating) - output) / 90
            room += 0.004 * output - 0.00008 * (room - 10)
            internal += (room + 1.5 + 2.2 * float(heating) - internal) / 40
        if now >= 10800:
            settled.append(room)
    return max(settled), sum(abs(t - 22) for t in settled) / len(settled)


def test_cloud_assistance_reduces_undershoot_in_biased_firmware_model(entity):
    baseline_peak, baseline_error = asyncio.run(simulate_firmware_limited_room(entity, False))
    entity._reset_sw_controller()
    assisted_peak, assisted_error = asyncio.run(simulate_firmware_limited_room(entity, True))
    assert assisted_error < baseline_error
    assert assisted_error <= 0.4
    assert assisted_peak <= 22.3


def test_initial_idle_enforces_minimum_off(entity):
    run(entity, 21.8)
    assert entity._sw_device_on is False
    run(entity, 21.6, dt=10)
    assert entity._sw_device_on is False
    run(entity, 21.6, dt=50)
    assert entity._sw_device_on is True


def test_minimum_on_delays_normal_cutoff(entity):
    run(entity, 21.6)
    run(entity, 22.1, dt=10)
    assert entity._sw_device_on is True
    run(entity, 22.1, dt=50)
    assert entity._sw_device_on is False


def test_predictive_cutoff(entity):
    run(entity, 21.6)
    run(entity, 21.65, dt=30, lag_time_min=2)
    run(entity, 21.8, dt=30, lag_time_min=2)
    assert entity._sw_device_on is False
    assert entity._sw_predicted_temp >= entity._sw_effective_off_threshold
    assert entity._sw_peak_temp == 21.8


def test_coast_peak_learning_and_earlier_next_cutoff(entity):
    run(entity, 21.6)
    run(entity, 21.7, dt=30, lag_time_min=2)
    run(entity, 21.8, dt=30, lag_time_min=2)
    assert entity._sw_device_on is False
    run(entity, 22.3)
    assert entity._sw_peak_temp == 22.3
    run(entity, 22.1)
    assert entity._sw_peak_temp == 22.3
    run(entity, 21.6)
    assert entity._sw_overshoot_correction == pytest.approx(0.09)
    run(entity, 21.92)
    assert entity._sw_device_on is False
    assert entity._sw_effective_off_threshold == pytest.approx(21.91)


def test_peak_learning_is_once_per_cycle_and_decays(entity):
    entity._sw_overshoot_correction = 0.2
    run(entity, 21.6)
    run(entity, 21.9)
    assert entity._sw_device_on is False
    for _ in range(3):
        before = entity._sw_overshoot_correction
        run(entity, 21.6)
        assert entity._sw_overshoot_correction == pytest.approx(before * 0.8)
        learned = entity._sw_overshoot_correction
        run(entity, 21.65)
        assert entity._sw_overshoot_correction == learned
        run(entity, 22.0)


def test_adaptation_bounded_and_i_never_authorizes_overshoot(entity):
    entity._sw_overshoot_correction = 0.29
    entity._sw_peak_temp = 24.0
    entity._learn_coast_peak(0.3)
    assert entity._sw_overshoot_correction == 0.3
    entity._sw_long_history = [(entity.clock - 1000, 23.0)]
    run(entity, 21.6, i_gain=1)
    assert entity._sw_i_correction == pytest.approx(0.075)
    assert entity._sw_effective_off_threshold == pytest.approx(21.76)
    entity._reset_sw_controller()
    entity._sw_long_history = [(entity.clock - 1000, 20.0)]
    run(entity, 21.6, i_gain=1)
    run(entity, 22.0, i_gain=1)
    assert entity._sw_i_correction == 0
    assert entity._sw_device_on is False


@pytest.mark.parametrize('value', ['unavailable', 'unknown', 'nonsense', 'nan', 'inf', '-41', '81', None])
def test_invalid_sensor_fallback_is_idempotent(entity, value, caplog):
    poll(entity, 21.6)
    entity._target_temp = 22.5
    entity.sensor = None if value is None else sensor(value)
    entity.clock += 1  # failure bypasses minimum ON protection
    asyncio.run(entity.async_update())
    assert entity._sw_fallback_active
    assert entity._sw_device_on is None
    assert entity.convector.setpoint == 22
    assert entity.convector.mode == 'heating'
    assert entity._sw_temp_history == []
    before = len(entity.convector.calls)
    asyncio.run(entity.async_update())
    assert len(entity.convector.calls) == before
    assert caplog.text.count('entering firmware fallback') == 1
    assert entity.target_temperature == 22.5
    assert entity.extra_state_attributes['sw_control_state'] == 'SW_WAITING_FOR_SENSOR'


def test_stale_numeric_sensor_fallback(entity):
    poll(entity, 21.6)
    poll(entity, 21.6, age=const.EXTERNAL_TEMP_MAX_AGE_SEC + 1)
    assert entity._sw_fallback_active
    assert entity.extra_state_attributes['external_temp_age_sec'] > const.EXTERNAL_TEMP_MAX_AGE_SEC
    assert entity._external_temp_reason == 'stale'


@pytest.mark.parametrize('age,valid', [(400, True), (449, True), (451, False)])
def test_external_sensor_extended_timeout_boundary(entity, age, valid):
    poll(entity, 21.6, age=age)
    assert entity._external_temp_valid is valid
    assert entity._sw_fallback_active is (not valid)


def test_fresh_unchanged_report_and_legacy_timestamp(entity):
    entity.sensor = sensor(21.6, age=1000)
    entity.sensor.last_reported = datetime.now(timezone.utc)
    asyncio.run(entity.async_update())
    assert not entity._sw_fallback_active
    entity.sensor = sensor(21.6, legacy=True)
    asyncio.run(entity.async_update())
    assert entity._external_temp_valid
    entity.sensor = types.SimpleNamespace(state='21.6')
    asyncio.run(entity.async_update())
    assert entity._sw_fallback_active


def attach_decoded_sensor(entity):
    """Model BTHome's processor contract including cached SensorValue reuse."""
    # Hashable keys with the fields HA uses.
    from collections import namedtuple
    key = namedtuple('EntityKey', 'key device_id')('temperature', None)
    observers = []

    class Data:
        def __init__(self, entity_data=None):
            self.entity_data = entity_data or {}

    class Processor:
        def __init__(self, convert=None):
            self.convert = convert
            self.data = Data()
            self.listeners = []

        def async_add_listener(self, listener):
            self.listeners.append(listener)
            return lambda: self.listeners.remove(listener)

    def register(observer):
        observers.append(observer)
        return lambda: observers.remove(observer)

    processor = Processor()
    processor.coordinator = types.SimpleNamespace(async_register_processor=register)
    source_type = type('BTHomeSensor', (), {'__module__': 'homeassistant.components.bthome.sensor'})
    source = source_type()
    source.processor = processor
    source.entity_key = key
    sources = {'sensor.room': source}
    entity.hass.data = {'sensor': types.SimpleNamespace(get_entity=lambda name: sources.get(name))}

    def emit(value=None, sample=None, field=key):
        if sample is None:
            sample = types.SimpleNamespace(native_value=value)
        update = types.SimpleNamespace(entity_values={field: sample})
        for observer in list(observers):
            data = observer.convert(update)
            for listener in list(observer.listeners):
                listener(data)
        return sample

    return emit, observers, sources, key


def test_unchanged_decoded_receipts_preserve_active_cycle(entity):
    emit, observers, _, _ = attach_decoded_sensor(entity)
    poll(entity, 21.6)
    assert len(observers) == 1
    emit(21.6)  # baseline: may be a cached sample, don't claim freshness
    entity.clock += 300
    emit(21.6)  # a newly decoded unchanged temperature
    history = list(entity._sw_temp_history)
    poll(entity, 21.6, age=600)
    assert not entity._sw_fallback_active
    assert entity._external_temp_age_sec == 10
    assert entity._sw_temp_history[:len(history)] == history
    assert entity.extra_state_attributes['external_temp_freshness_source'] == 'decoded_temperature_report'


def test_cached_or_unrelated_decoded_fields_do_not_refresh(entity):
    emit, _, _, key = attach_decoded_sensor(entity)
    poll(entity, 21.6)
    emit(21.6)
    sample = emit(21.6)
    entity.clock += 451
    emit(sample=sample)  # old SensorValue carried by RSSI-only update
    emit(71, field=type(key)('battery', None))
    poll(entity, 21.6, age=600)
    assert entity._sw_fallback_active
    assert entity._external_temp_reason == 'stale'


@pytest.mark.parametrize('value', [None, True, float('nan'), float('inf'), 81, -41])
def test_invalid_decoded_receipt_does_not_refresh(entity, value):
    emit, _, _, _ = attach_decoded_sensor(entity)
    poll(entity, 21.6)
    emit(21.6)
    emit(value)
    poll(entity, 21.6, age=600)
    assert entity._sw_fallback_active


def test_receipt_listener_rebind_and_cleanup(entity):
    emit, observers, sources, _ = attach_decoded_sensor(entity)
    poll(entity, 21.6)
    emit(21.6)
    emit(21.6)
    # Sensor replacement/reload must drop old receipt time and old subscription.
    old = sources['sensor.room']
    sources['sensor.room'] = type(old)()
    sources['sensor.room'].processor = old.processor
    sources['sensor.room'].entity_key = old.entity_key
    poll(entity, 21.6, age=600)
    assert len(observers) == 1
    assert entity._sw_fallback_active
    asyncio.run(entity.async_will_remove_from_hass())
    assert observers == []


@pytest.mark.parametrize('timestamp,valid', [
    (datetime.now(timezone.utc).isoformat(), True),
    ((datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat(), False),
    ((datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(), False),
    ('unavailable', False), ('unknown', False), ('not-a-date', False),
    ('2026-10-04T10:00:00', False),
])
def test_generic_temperature_receipt_timestamp(entity, timestamp, valid):
    entity._config_entry.options[const.CONF_TEMPERATURE_REPORT_ENTITY] = 'sensor.last_temperature_report'
    entity.hass.states.get = lambda name: (
        types.SimpleNamespace(state=timestamp) if name == 'sensor.last_temperature_report' else entity.sensor
    )
    poll(entity, 21.6, age=600)
    assert entity._external_temp_valid is valid


def test_fresh_receipt_does_not_rescue_unavailable_temperature(entity):
    entity._config_entry.options[const.CONF_TEMPERATURE_REPORT_ENTITY] = 'sensor.last_temperature_report'
    entity.hass.states.get = lambda name: (
        types.SimpleNamespace(state=datetime.now(timezone.utc).isoformat())
        if name == 'sensor.last_temperature_report' else entity.sensor
    )
    poll(entity, 'unavailable')
    assert entity._sw_fallback_active


def test_decoded_receipt_for_other_temperature_channel_is_ignored(entity):
    emit, _, _, key = attach_decoded_sensor(entity)
    poll(entity, 21.6)
    emit(21.6)
    emit(21.6, field=type(key)('temperature', 'other_probe'))
    poll(entity, 21.6, age=600)
    assert entity._sw_fallback_active


def test_optional_adapter_missing_keeps_generic_sensor_working(entity):
    _, observers, sources, _ = attach_decoded_sensor(entity)
    sources.clear()
    poll(entity, 21.6)
    assert entity._external_temp_valid
    assert observers == []
    poll(entity, 21.6, age=600)
    assert entity._sw_fallback_active


def test_receipt_adapter_uses_current_ha_entity_component_registry(entity):
    emit, observers, _, _ = attach_decoded_sensor(entity)
    component = entity.hass.data.pop('sensor')
    entity.hass.data['entity_components'] = {'sensor': component}
    entity.hass.data['sensor'] = object()  # modern sensor-domain runtime data
    poll(entity, 21.6)
    assert len(observers) == 1
    emit(21.6)
    emit(21.6)
    poll(entity, 21.6, age=600)
    assert entity._external_temp_valid


def test_unchanged_decoded_receipt_does_not_add_heater_commands(entity):
    emit, _, _, _ = attach_decoded_sensor(entity)
    poll(entity, 21.6)
    emit(21.6)
    before = list(entity.convector.calls)
    for _ in range(4):
        emit(21.6)
        poll(entity, 21.6, dt=10, age=600)
    assert entity.convector.calls == before


def test_recovery_seeds_ema_and_clean_histories(entity):
    entity._config_entry.options[const.CONF_SW_EMA_ALPHA] = 0.2
    poll(entity, 21.6)
    entity._sw_overshoot_correction = 0.2
    entity._sw_peak_temp = 23.0
    poll(entity, 'unavailable')
    poll(entity, 21.9)
    assert not entity._sw_fallback_active
    assert entity.current_temperature == 21.9
    assert entity._sw_device_on is False
    assert entity._sw_temp_history == [(entity.clock, 21.9)]
    assert entity._sw_long_history == [(entity.clock, 21.9)]
    assert entity._sw_last_on_time is None
    assert entity._sw_last_off_time == entity.clock
    assert entity._sw_peak_temp is None
    assert entity._sw_overshoot_correction == 0


@pytest.mark.parametrize('device_on', [True, False])
def test_restart_missing_sensor_restores_persisted_target(entity, device_on):
    entity._config_entry.options.update({const.CONF_LAST_HVAC_MODE: 'heat', const.CONF_LAST_SETPOINT: 22.5})
    entity._hvac_mode = HVACMode.OFF
    entity._target_temp = None
    entity.convector.on = device_on
    entity.sensor = None
    asyncio.run(entity.async_added_to_hass())
    assert entity._sw_fallback_active
    assert entity.convector.setpoint == 22
    assert entity.convector.on
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.target_temperature == 22.5


@pytest.mark.parametrize('opened', [False, True])
def test_manual_window_override_does_not_bypass_fallback(entity, opened):
    poll(entity, 21.6)
    if opened:
        asyncio.run(entity.async_handle_manual_window_status('on'))
        assert entity.hvac_mode == HVACMode.OFF
    else:
        entity._manual_window_override = True
    poll(entity, 'unavailable')
    assert entity._sw_fallback_active
    assert entity.hvac_mode == HVACMode.HEAT
    assert not entity.is_window_opened
    assert entity.convector.setpoint == 22


def test_single_acquisition_and_shared_ema(entity):
    entity._config_entry.options[const.CONF_SW_EMA_ALPHA] = 0.2
    entity._config_entry.options[const.CONF_WINDOW_OPEN_ENABLED] = True
    poll(entity, 21.6)
    entity.hass.states.get.reset_mock()
    poll(entity, 22.0)
    entity.hass.states.get.assert_called_once_with('sensor.room')
    expected = 0.2 * 22.0 + 0.8 * 21.6
    assert entity.current_temperature == pytest.approx(expected)
    assert entity._temp_history[-1][1] == pytest.approx(expected)
    assert entity._sw_long_history[-1][1] == pytest.approx(expected)


def test_disabling_sw_restores_real_target(entity):
    entity._target_temp = 22.5
    poll(entity, 23.0)
    assert entity.convector.setpoint == 10
    entity._config_entry.options[const.CONF_SW_CONTROL_ENABLED] = False
    poll(entity, 23.0)
    assert entity.convector.setpoint == 22
    assert not entity._sw_fallback_active
    assert entity._sw_device_on is None


@pytest.mark.parametrize('mode', [HVACMode.OFF, HVACMode.AUTO])
def test_leaving_heat_restores_sw_off_setpoint(entity, mode):
    entity._target_temp = 22.5
    poll(entity, 23.0)
    assert entity.convector.setpoint == 10
    asyncio.run(entity.async_set_hvac_mode(mode))
    assert entity.convector.setpoint == 22
    assert entity._sw_device_on is None
    assert not entity._sw_fallback_active
    assert entity.hvac_mode == mode


def test_target_change_while_in_fallback_uses_floor(entity):
    poll(entity, 'unavailable')
    asyncio.run(entity.async_set_temperature(temperature=23.5))
    assert entity.convector.setpoint == 23
    assert entity._sw_fallback_active
    poll(entity, 'unavailable')
    assert entity.target_temperature == 23.5


def test_failed_fallback_write_is_retried(entity):
    entity.convector.set_temperature = AsyncMock(side_effect=[{'error': 'offline'}, {}])
    with pytest.raises(climate.DeviceCommandError, match='restore firmware target'):
        asyncio.run(entity._enter_firmware_fallback('missing'))
    assert not entity._sw_fallback_active
    asyncio.run(entity._enter_firmware_fallback('missing'))
    assert entity._sw_fallback_active
    assert entity.convector.set_temperature.await_count == 2


def test_hvac_action_remains_valid_enum_with_duty_cycle(entity):
    entity._sw_device_on = True
    entity._sw_duty_pct = 50
    entity._update_hvac_action()
    assert entity.hvac_action == HVACAction.HEATING
    assert entity.extra_state_attributes['duty_cycle_pct'] == 50


def test_minimum_off_after_complete_heating_phase(entity):
    run(entity, 21.6)
    run(entity, 22.0)
    run(entity, 21.6, dt=10)
    assert entity._sw_device_on is False
    run(entity, 21.6, dt=50)
    assert entity._sw_device_on is True


def test_window_detection_still_inhibits_and_restores_heat(entity):
    entity._config_entry.options[const.CONF_WINDOW_OPEN_ENABLED] = True
    poll(entity, 21.6)
    poll(entity, 21.3, dt=30)
    poll(entity, 21.0, dt=30)
    assert entity.is_window_opened
    assert entity.hvac_mode == HVACMode.OFF
    assert not entity.convector.on
    assert entity.extra_state_attributes['sw_control_state'] == 'WINDOW_INHIBIT'
    poll(entity, 20.9, dt=40)
    poll(entity, 22.1, dt=40)
    poll(entity, 22.2, dt=40)
    assert not entity.is_window_opened
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.convector.on
    assert entity._sw_device_on is False


def test_auto_schedule_survives_polling(entity):
    entity._hvac_mode = HVACMode.AUTO
    entity.convector.mode = 'program'
    entity.convector.setpoint = 24
    poll(entity, 21.6)
    assert entity.hvac_mode == HVACMode.AUTO
    assert entity.convector.setpoint == 24
    assert entity.target_temperature == 22.0
    assert entity.convector.calls == []


@pytest.mark.parametrize('on,mode', [(False, 'heating'), (True, 'program')])
def test_external_mode_change_clears_controller_ownership(entity, on, mode):
    poll(entity, 'unavailable')
    entity.convector.on = on
    entity.convector.mode = mode
    if mode == 'program':
        entity.convector.setpoint = 24
    poll(entity, 'unavailable')
    assert not entity._sw_fallback_active
    assert entity.hvac_mode == (HVACMode.AUTO if on else HVACMode.OFF)
    assert entity._sw_device_on is None
    assert entity.convector.on == on
    assert entity.convector.setpoint == (24 if on else 22)


def test_failed_mode_write_does_not_claim_fallback(entity):
    entity.convector.set_mode = AsyncMock(side_effect=[{'error': 'offline'}, {}])
    with pytest.raises(climate.DeviceCommandError, match='enable firmware heating'):
        asyncio.run(entity._enter_firmware_fallback('missing'))
    assert not entity._sw_fallback_active
    asyncio.run(entity._enter_firmware_fallback('missing'))
    assert entity._sw_fallback_active


@pytest.mark.parametrize('method', ['set_temperature', 'set_mode'])
def test_restart_fallback_connection_reset_keeps_entity_and_retries(entity, method, caplog):
    if method == 'set_mode':
        entity.convector.on = False
    entity._config_entry.options.update({
        const.CONF_LAST_HVAC_MODE: 'heat', const.CONF_LAST_SETPOINT: 22.0,
    })
    entity.sensor = sensor('unavailable')
    original = getattr(entity.convector, method)
    failed_write = AsyncMock(return_value={'error': '[Errno 104] Connection reset by peer'})
    setattr(entity.convector, method, failed_write)
    asyncio.run(entity.async_added_to_hass())
    assert entity._remove_update_listener is not None
    assert entity._attr_available
    assert not entity._sw_fallback_active
    assert entity._sw_device_on is None
    assert entity._control_error_logged
    poll(entity, 'unavailable', dt=2)
    assert failed_write.await_count == 1
    assert caplog.text.count('Tesy control command failed') == 1
    setattr(entity.convector, method, original)
    poll(entity, 'unavailable', dt=8)
    assert entity._sw_fallback_active
    assert not entity._control_error_logged
    poll(entity, 21.6)
    assert not entity._sw_fallback_active
    assert entity._sw_device_on is True


def test_restart_failed_first_software_phase_remains_retryable(entity):
    entity._config_entry.options.update({
        const.CONF_LAST_HVAC_MODE: 'heat', const.CONF_LAST_SETPOINT: 22.0,
    })
    original = entity.convector.set_temperature
    entity.convector.set_temperature = AsyncMock(return_value={'error': 'connection reset'})
    asyncio.run(entity.async_added_to_hass())
    assert entity._sw_device_on is None
    assert entity._attr_available
    entity.convector.set_temperature = original
    poll(entity, 21.6)
    assert entity._sw_device_on is True


def test_restart_off_target_failure_preserves_pending_mode_until_retry(entity):
    entity._config_entry.options.update({
        const.CONF_LAST_HVAC_MODE: 'off', const.CONF_LAST_SETPOINT: 22.0,
    })
    original = entity.convector.set_temperature
    entity.convector.set_temperature = AsyncMock(return_value={'error': 'connection reset'})
    asyncio.run(entity.async_added_to_hass())
    assert entity._pending_boot_mode == HVACMode.OFF
    entity.convector.set_temperature = original
    poll(entity, 21.6)
    assert entity._pending_boot_mode is None
    assert entity.hvac_mode == HVACMode.OFF
    assert not entity.convector.on


@pytest.mark.parametrize('device_on,device_mode,expected', [
    (False, 'heating', HVACMode.OFF),
    (True, 'heating', HVACMode.HEAT),
    (True, 'program', HVACMode.AUTO),
])
def test_ha_startup_reports_actual_mode_without_writes(entity, device_on, device_mode, expected):
    entity.hass.is_running = False
    entity.convector.on = device_on
    entity.convector.mode = device_mode
    entity._config_entry.options.update({
        const.CONF_LAST_HVAC_MODE: 'heat' if expected != HVACMode.HEAT else 'off',
        const.CONF_LAST_SETPOINT: 22.5,
    })
    entity.sensor = sensor('unavailable')
    asyncio.run(entity.async_added_to_hass())
    assert entity.hvac_mode == expected
    assert entity.convector.calls == []
    assert not entity._sw_fallback_active
    poll(entity, 'unavailable', dt=30)
    assert entity.convector.calls == []
    assert entity.hvac_mode == expected


def test_ha_startup_status_tracks_changes_and_leaves_off_after_start(entity):
    entity.hass.is_running = False
    entity._config_entry.options[const.CONF_LAST_HVAC_MODE] = 'heat'
    entity.convector.on = False
    asyncio.run(entity.async_added_to_hass())
    assert entity.hvac_mode == HVACMode.OFF
    entity.convector.on = True
    poll(entity, 21.0)
    assert entity.hvac_mode == HVACMode.HEAT
    entity.convector.on = False
    poll(entity, 21.0)
    assert entity.hvac_mode == HVACMode.OFF
    entity.hass.is_running = True
    poll(entity, 21.0)
    assert entity.hvac_mode == HVACMode.OFF
    assert entity.convector.calls == []


def test_ha_startup_waits_for_room_sensor_then_reuses_existing_heating_mode(entity):
    entity.hass.is_running = False
    entity._config_entry.options.update({
        const.CONF_LAST_HVAC_MODE: 'heat', const.CONF_LAST_SETPOINT: 22.0,
    })
    entity.sensor = sensor('unavailable')
    asyncio.run(entity.async_added_to_hass())
    assert entity.convector.calls == []
    entity.hass.is_running = True
    poll(entity, 'unavailable', dt=10)
    assert not entity._sw_fallback_active
    assert entity.convector.calls == []
    poll(entity, 21.6, dt=10)
    assert entity._sw_device_on is True
    assert ('mode', 'heating') not in entity.convector.calls


def test_ha_startup_missing_sensor_falls_back_after_bounded_wait(entity):
    entity.hass.is_running = False
    entity._config_entry.options.update({
        const.CONF_LAST_HVAC_MODE: 'heat', const.CONF_LAST_SETPOINT: 22.0,
    })
    entity.sensor = sensor('unavailable')
    asyncio.run(entity.async_added_to_hass())
    entity.hass.is_running = True
    poll(entity, 'unavailable', dt=60)
    assert entity._sw_fallback_active
    assert ('mode', 'heating') not in entity.convector.calls


@pytest.mark.parametrize('room,expected_setpoint', [(22.5, 10), (21.0, 23)])
def test_ha_restart_during_software_off_preserves_low_setpoint_until_sensor_ready(entity, room, expected_setpoint):
    entity.hass.is_running = False
    entity.convector.on = True
    entity.convector.mode = 'heating'
    entity.convector.setpoint = 10
    entity._config_entry.options.update({
        const.CONF_LAST_HVAC_MODE: 'heat', const.CONF_LAST_SETPOINT: 22.5,
    })
    entity.sensor = sensor('unavailable')
    asyncio.run(entity.async_added_to_hass())
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.convector.calls == []
    entity.hass.is_running = True
    poll(entity, 'unavailable', dt=10)
    assert entity.convector.setpoint == 10
    assert entity.convector.calls == []
    poll(entity, room, dt=10)
    assert entity.convector.setpoint == expected_setpoint
    assert entity.hvac_mode == HVACMode.HEAT
    assert ('mode', 'heating') not in entity.convector.calls


def test_mode_command_failure_is_a_normal_ha_error_and_not_saved(entity):
    entity.convector.turn_off = AsyncMock(return_value={'error': 'connection reset'})
    with pytest.raises(climate.HomeAssistantError, match='Could not turn off heater'):
        asyncio.run(entity.async_set_hvac_mode(HVACMode.OFF))
    assert entity.hvac_mode == HVACMode.HEAT
    assert const.CONF_LAST_HVAC_MODE not in entity._config_entry.options


@pytest.mark.parametrize('correction', [0, 3, -2])
def test_saved_correction_sent_after_ha_startup_even_when_heater_off(entity, correction):
    entity.hass.is_running = False
    entity.convector.on = False
    entity._config_entry.options[const.CONF_TEMPERATURE_CORRECTION] = correction
    entity.convector.set_temperature_correction = AsyncMock(return_value={})
    entity.hass.data = {const.DOMAIN: {entity._config_entry.entry_id: {'device': entity.convector}}}
    asyncio.run(entity.async_added_to_hass())
    entity.convector.set_temperature_correction.assert_not_awaited()
    poll(entity, 'unavailable')
    entity.convector.set_temperature_correction.assert_not_awaited()
    entity.hass.is_running = True
    poll(entity, 'unavailable')
    entity.convector.set_temperature_correction.assert_awaited_once_with(
        correction, source='climate._apply_stored_temperature_correction',
    )
    assert not entity._startup_correction_pending
    assert entity.hass.data[const.DOMAIN]['test']['applied_correction'] == correction
    assert entity.hvac_mode == HVACMode.OFF
    poll(entity, 'unavailable')
    assert entity.convector.set_temperature_correction.await_count == 1


def test_startup_correction_reset_retries_and_only_clears_pending_on_success(entity):
    entity.hass.is_running = False
    entity.convector.on = False
    entity._config_entry.data[const.CONF_TEMPERATURE_CORRECTION] = 3
    entity.convector.set_temperature_correction = AsyncMock(side_effect=[
        {'error': '[Errno 104] Connection reset by peer'}, {},
    ])
    asyncio.run(entity.async_added_to_hass())
    entity.hass.is_running = True
    poll(entity, 21.6)
    assert entity._startup_correction_pending
    poll(entity, 21.6, dt=2)
    assert entity.convector.set_temperature_correction.await_count == 1
    poll(entity, 21.6, dt=8)
    assert not entity._startup_correction_pending
    assert entity.convector.set_temperature_correction.await_count == 2
    assert entity.hvac_mode == HVACMode.OFF


def test_correction_pending_survives_ha_starting_between_integration_and_entity_setup(entity):
    entity.hass.is_running = True
    entity._config_entry.options[const.CONF_TEMPERATURE_CORRECTION] = 2
    entity.hass.data = {const.DOMAIN: {'test': {'device': entity.convector, 'correction_pending': True}}}
    entity.convector.set_temperature_correction = AsyncMock(return_value={})
    asyncio.run(entity.async_added_to_hass())
    poll(entity, 21.6)
    entity.convector.set_temperature_correction.assert_awaited_once_with(
        2, source='climate._apply_stored_temperature_correction',
    )
    assert not entity.hass.data[const.DOMAIN]['test']['correction_pending']


@pytest.mark.parametrize('heater_on', [True, False])
def test_correction_after_unavailability_retries_failed_write(entity, heater_on):
    entity.convector.on = heater_on
    entity._hvac_mode = HVACMode.HEAT if heater_on else HVACMode.OFF
    entity._config_entry.options[const.CONF_TEMPERATURE_CORRECTION] = 3
    entity.convector.set_temperature_correction = AsyncMock(side_effect=[
        {'error': 'connection reset'}, {},
    ])
    original_status = entity.convector.get_status
    entity.convector.get_status = AsyncMock(return_value={'error': 'offline'})
    for _ in range(3):
        poll(entity, 21.6)
    assert not entity._attr_available
    entity.convector.set_temperature_correction.assert_not_awaited()
    entity.convector.get_status = original_status
    poll(entity, 21.6, dt=30)
    assert entity._attr_available
    assert entity._startup_correction_pending
    assert entity.convector.set_temperature_correction.await_count == 1
    poll(entity, 21.6, dt=2)
    assert entity.convector.set_temperature_correction.await_count == 1
    poll(entity, 21.6, dt=8)
    assert not entity._startup_correction_pending
    assert entity.convector.set_temperature_correction.await_count == 2
    poll(entity, 21.6)
    assert entity.convector.set_temperature_correction.await_count == 2
    assert entity.convector.on is heater_on


def test_short_power_cycle_reapplies_correction_without_availability_drop(entity):
    entity._config_entry.options[const.CONF_TEMPERATURE_CORRECTION] = 3
    entity.convector.set_temperature_correction = AsyncMock(return_value={})
    original_status = entity.convector.get_status
    entity.convector.get_status = AsyncMock(return_value={'error': 'offline'})
    poll(entity, 21.6)
    entity.convector.get_status = original_status
    poll(entity, 21.6)
    assert entity._attr_available
    entity.convector.set_temperature_correction.assert_awaited_once_with(
        3, source='climate._apply_stored_temperature_correction',
    )
    poll(entity, 21.6)
    assert entity.convector.set_temperature_correction.await_count == 1


def test_power_cycle_between_polls_is_detected_by_reported_correction_reset(entity):
    entity._config_entry.options[const.CONF_TEMPERATURE_CORRECTION] = 3
    reported = [3]
    original_status = entity.convector.get_status

    async def status(**kwargs):
        value = await original_status(**kwargs)
        value['payload']['setTCorrection'] = {'payload': {'temp': reported[0]}}
        return value

    async def apply(value, **kwargs):
        reported[0] = value
        return {}

    entity.convector.get_status = status
    entity.convector.set_temperature_correction = AsyncMock(side_effect=apply)
    poll(entity, 21.6)
    entity.convector.set_temperature_correction.assert_not_awaited()
    reported[0] = 0  # reboot between status polls, with no observed network error
    poll(entity, 21.6)
    entity.convector.set_temperature_correction.assert_awaited_once_with(
        3, source='climate._apply_stored_temperature_correction',
    )
    poll(entity, 21.6)
    assert entity.convector.set_temperature_correction.await_count == 1


def test_sensor_change_seeds_new_history(entity):
    poll(entity, 21.6)
    entity._sw_overshoot_correction = 0.2
    entity._config_entry.options[const.CONF_TEMPERATURE_ENTITY] = 'sensor.other_room'
    poll(entity, 21.9)
    assert entity._sw_device_on is False
    assert entity._sw_long_history == [(entity.clock, 21.9)]
    assert entity._sw_overshoot_correction == 0


def test_duty_cycle_tracks_complete_cycles(entity):
    run(entity, 21.6)
    run(entity, 22.0, dt=120)
    run(entity, 21.6, dt=60)
    assert entity._sw_duty_pct == 67
    assert entity._sw_duty_cycles == [(120, 60)]


def restarted(entity, value=21.9):
    """Boot a new entity, retaining only entry options, storage, and device."""
    new = climate.TesyConvectorClimate(entity.convector, entity._config_entry)
    new.clock = entity.clock + 100
    new.sensor = sensor(value)
    new.hass = types.SimpleNamespace(
        loop=types.SimpleNamespace(time=lambda: new.clock),
        states=types.SimpleNamespace(get=Mock(side_effect=lambda _: new.sensor)),
        config_entries=entity.hass.config_entries,
        storage=entity.hass.storage,
    )
    asyncio.run(new.async_added_to_hass())
    return new


def saved_learning(entity, correction=0.09, cycles=None):
    return {
        'context': entity._sw_learning_context(),
        'overshoot_correction': correction,
        'duty_cycles': [[120, 60], [60, 120]] if cycles is None else cycles,
    }


def test_learning_saved_only_after_complete_cycle(entity):
    entity._sw_learning_store = MemoryStore(entity.hass, 1, 'test')
    run(entity, 21.6)
    run(entity, 22.0, dt=120)
    run(entity, 22.3)
    assert entity._sw_learning_store.saves == 0
    run(entity, 21.6)
    assert entity._sw_learning_store.saves == 1
    saved = entity.hass.storage['test']
    assert saved['overshoot_correction'] == pytest.approx(0.09)
    assert saved['duty_cycles'] == [[120, 120]]
    assert set(saved) == {'context', 'overshoot_correction', 'duty_cycles'}
    run(entity, 21.65)
    assert entity._sw_learning_store.saves == 1


def test_learning_and_duty_survive_real_restart_without_replaying_phase(entity):
    entity._config_entry.options.update({const.CONF_LAST_HVAC_MODE: 'heat', const.CONF_LAST_SETPOINT: 22.0})
    asyncio.run(entity.async_added_to_hass())
    run(entity, 22.0, dt=120)
    run(entity, 22.3, dt=60)
    run(entity, 21.6, dt=60)
    correction = entity._sw_overshoot_correction
    cycles = list(entity._sw_duty_cycles)
    duty = entity._sw_duty_pct
    new = restarted(entity)
    assert new._sw_overshoot_correction == pytest.approx(correction)
    assert new._sw_duty_cycles == cycles
    assert new._sw_duty_pct == duty
    assert new._sw_device_on is False  # fresh sensor says idle, despite old ON
    assert new._sw_last_on_time is None
    assert new._sw_peak_temp is None
    assert new._sw_long_history == [(new.clock, 21.9)]
    # Starting the first new ON phase must not relabel a saved completed OFF.
    run(new, 21.6, dt=60)
    assert new._sw_duty_cycles == cycles
    assert new._sw_learning_store.saves == 0
    run(new, 22.0, dt=90)
    run(new, 21.6, dt=120)
    assert new._sw_duty_cycles[-1] == (90, 120)
    assert new._sw_learning_store.saves == 1
    assert new._sw_overshoot_correction == pytest.approx(correction * 0.8)


def test_restart_startup_fallback_keeps_learning_pending_until_fresh_sensor(entity):
    entity._config_entry.options.update({const.CONF_LAST_HVAC_MODE: 'heat', const.CONF_LAST_SETPOINT: 22.0})
    entity.hass.storage = {f'{const.DOMAIN}.test.sw_learning': saved_learning(entity)}
    new = restarted(entity, 'unavailable')
    assert new._sw_fallback_active
    assert new.convector.setpoint == 22
    assert new._sw_overshoot_correction == 0
    poll(new, 21.9)
    assert not new._sw_fallback_active
    assert new._sw_overshoot_correction == pytest.approx(0.09)
    assert new._sw_duty_pct == 50
    assert new._sw_long_history == [(new.clock, 21.9)]
    assert new._sw_last_on_time is None


@pytest.mark.parametrize('setting,value', [
    (const.CONF_TEMPERATURE_ENTITY, 'sensor.other'),
    (const.CONF_SW_HYSTERESIS, 0.5),
    (const.CONF_SW_LAG_TIME, 2),
    (const.CONF_SW_EMA_ALPHA, 0.5),
    (const.CONF_SW_I_GAIN, 0.5),
])
def test_saved_learning_rejected_for_changed_settings(entity, setting, value):
    entity._sw_restore_pending = saved_learning(entity)
    entity._config_entry.options[setting] = value
    run(entity, 21.9)
    assert entity._sw_overshoot_correction == 0
    assert entity._sw_duty_cycles == []


def test_saved_learning_rejected_for_changed_target(entity):
    entity._sw_restore_pending = saved_learning(entity)
    entity._target_temp = 23.0
    run(entity, 23.0)
    assert entity._sw_overshoot_correction == 0
    assert entity._sw_duty_cycles == []


@pytest.mark.parametrize('bad_value', [
    {}, [],
    {'overshoot_correction': float('nan')},
    {'overshoot_correction': float('inf')},
    {'overshoot_correction': -0.1},
    {'overshoot_correction': 100},
    {'duty_cycles': [[120, 0]]},
    {'duty_cycles': [[float('inf'), 60]]},
    {'duty_cycles': [[120]]},
    {'duty_cycles': None},
])
def test_invalid_persisted_learning_does_not_interrupt_control(entity, bad_value):
    if isinstance(bad_value, dict) and bad_value:
        payload = saved_learning(entity)
        payload.update(bad_value)
    else:
        payload = bad_value
    entity._sw_restore_pending = payload
    run(entity, 21.6)
    assert entity._sw_device_on is True
    assert entity._sw_overshoot_correction == 0


def test_restore_keeps_only_last_five_completed_cycles(entity):
    entity._sw_restore_pending = saved_learning(entity, cycles=[[60, 120]] * 8)
    run(entity, 21.9)
    assert entity._sw_duty_cycles == [(60, 120)] * 5
    assert entity._sw_duty_pct == 33


def test_storage_write_failure_does_not_interrupt_phase_transition(entity, caplog):
    entity._sw_learning_store = types.SimpleNamespace(async_save=AsyncMock(side_effect=OSError('disk full')))
    run(entity, 21.6)
    run(entity, 22.0)
    run(entity, 21.6)
    assert entity._sw_device_on is True
    assert 'could not save learning' in caplog.text


def test_window_service_routes_to_multiple_current_entries(entity, caplog):
    first = types.SimpleNamespace(entity_id='climate.first', async_handle_manual_window_status=AsyncMock())
    second = types.SimpleNamespace(entity_id='climate.second', async_handle_manual_window_status=AsyncMock())
    entity.hass.data = {const.DOMAIN: {'first': {'climate_entity': first}}}
    schema, handler = climate._create_window_service_handler(entity.hass)
    entity.hass.data[const.DOMAIN]['second'] = {'climate_entity': second}
    data = schema({'entity_id': ['climate.first', 'climate.second'], 'status': 'on'})
    asyncio.run(handler(types.SimpleNamespace(data=data)))
    first.async_handle_manual_window_status.assert_awaited_once_with('on')
    second.async_handle_manual_window_status.assert_awaited_once_with('on')
    del entity.hass.data[const.DOMAIN]['first']
    asyncio.run(handler(types.SimpleNamespace(data={'entity_id': 'climate.first', 'status': 'off'})))
    assert 'no Tesy climate entity matched' in caplog.text
    assert second.async_handle_manual_window_status.await_count == 1


def test_climate_setup_registers_window_service_only_once(entity):
    handlers = {}
    def register(domain, name, handler, **kwargs):
        handlers[(domain, name)] = handler
    entity.hass.services = types.SimpleNamespace(
        has_service=lambda domain, name: (domain, name) in handlers,
        async_register=Mock(side_effect=register),
    )
    entity.hass.data = {const.DOMAIN: {'test': {'device': entity.convector}, 'second': {'device': Device()}}}
    entry2 = types.SimpleNamespace(entry_id='second', data={}, options={})
    added = []
    asyncio.run(climate.async_setup_entry(entity.hass, entity._config_entry, added.extend))
    asyncio.run(climate.async_setup_entry(entity.hass, entry2, added.extend))
    entity.hass.services.async_register.assert_called_once()
    assert len(added) == 2


def test_firmware_limit_estimate_and_raw_temperature_error(entity):
    original = entity.convector.get_status
    async def status(**kwargs):
        value = await original(**kwargs)
        value['payload']['tempAir']['payload']['temp'] = 23.0
        return value
    entity.convector.get_status = status
    poll(entity, 21.6)
    attrs = entity.extra_state_attributes
    assert attrs['firmware_limit_estimated'] is True
    assert attrs['sw_heat_requested'] is True
    assert attrs['internal_temp'] == 23.0
    assert attrs['external_temperature_error'] == pytest.approx(-0.4)
    poll(entity, 'unavailable')
    attrs = entity.extra_state_attributes
    assert attrs['firmware_limit_estimated'] is None
    assert attrs['external_temperature_error'] is None


async def simulate_thermal_room(entity, *, inertia, sensor_delay, predictive):
    """Synthetic six-hour loop, not a model of proprietary Tesy firmware.

    Heater output coasts through a first-order time constant. Room heat loss
    rises with indoor/outdoor difference. Both controllers receive the same
    delayed sensor and EMA and obey the same minimum phase durations.
    Reference is plain hysteresis; actual Tesy relay behavior is unknown.
    """
    room = sensed = filtered = 20.0
    output = 0.0
    samples = []
    starts = 0
    requested = None
    switched_at = -60
    for now in range(0, 21600, 10):
        entity.clock = now
        filtered = 0.2 * sensed + 0.8 * filtered
        before = requested
        if predictive:
            await entity._run_sw_controller(filtered, 60, 60, 0.3, 6.0, 0.0)
            requested = entity._sw_device_on
        else:
            desired = (
                filtered < 22.0 if requested else filtered <= 21.7
            )
            if desired != requested and now - switched_at >= 60:
                requested = desired
                switched_at = now
        starts += requested is True and before is not True
        for _ in range(10):
            output += (float(requested) - output) / inertia
            room += 0.004 * output - 0.00008 * (room - 10.0)
            sensed += (room - sensed) / sensor_delay
        if now >= 14400:  # settled final two hours
            samples.append(room)
    return {
        'min': min(samples), 'max': max(samples),
        'mean_absolute_error': sum(abs(value - 22.0) for value in samples) / len(samples),
        'starts': starts,
    }


@pytest.mark.parametrize('inertia,sensor_delay', [(30, 10), (90, 60), (180, 120)])
def test_closed_loop_thermal_coast_is_bounded_and_reduces_overshoot(entity, inertia, sensor_delay):
    reference = asyncio.run(simulate_thermal_room(entity, inertia=inertia, sensor_delay=sensor_delay, predictive=False))
    entity._reset_sw_controller()
    controlled = asyncio.run(simulate_thermal_room(entity, inertia=inertia, sensor_delay=sensor_delay, predictive=True))
    assert controlled['max'] < reference['max']
    assert controlled['max'] <= 22.2
    assert controlled['min'] >= 21.4
    assert controlled['mean_absolute_error'] <= 0.25


def test_sw_off_uses_minimum_even_when_internal_sensor_is_colder(entity):
    # Internal sensor is 20 C, room is already 22 C. A 21 C OFF setpoint
    # would allow the firmware to keep heating against our room feedback.
    poll(entity, 22.0)
    assert entity._sw_device_on is False
    assert entity.convector.setpoint == 10
    assert entity.convector.setpoint < entity._internal_temp
    assert entity.convector.on
    poll(entity, 21.6, dt=60)
    assert entity._sw_device_on is True
    assert entity.convector.setpoint == 22


def test_poll_does_not_treat_minimum_off_target_as_user_setpoint_change(entity):
    entity._target_temp = 22.5
    poll(entity, 23.0)
    poll(entity, 23.0)
    assert entity._sw_device_on is False
    assert entity.convector.setpoint == 10
    assert entity.target_temperature == 22.5


def test_live_diagnostics_are_excluded_from_climate_recorded_attributes(entity):
    poll(entity, 21.6)
    entity._sw_duty_pct = 50
    entity._sw_duty_cycles.append((120, 120))
    attrs = entity.extra_state_attributes
    assert attrs['duty_cycle_pct'] == 50
    assert attrs['duty_cycles_sampled'] == 1
    assert set(attrs) <= entity._unrecorded_attributes
    assert {'temperature', 'current_temperature', 'hvac_action'}.isdisjoint(
        entity._unrecorded_attributes
    )


def test_cloud_report_can_show_idle_while_software_still_requests_heat(entity):
    from custom_components.tesy_convector_local.cloud import CloudTelemetry
    from test_cloud import config, report

    entity._config_entry.options[const.CONF_CLOUD_TELEMETRY_ENABLED] = True
    telemetry = CloudTelemetry(config(), Mock())
    entity._cloud_telemetry = telemetry
    poll(entity, 21.6)
    assert entity._sw_device_on is True
    assert entity.hvac_action is None
    telemetry._set_connected(True)
    telemetry.receive(telemetry.topic, report(heating='off'))
    entity._update_hvac_action()
    assert entity.hvac_action == HVACAction.IDLE
    assert entity.extra_state_attributes['sw_heat_requested'] is True
    assert entity.extra_state_attributes['cloud_heating'] is False
    assert set(entity.extra_state_attributes) <= entity._unrecorded_attributes
    telemetry.receive(telemetry.topic, report(heating='on'))
    entity._update_hvac_action()
    assert entity.hvac_action == HVACAction.HEATING
    telemetry._set_connected(False)
    entity._update_hvac_action()
    assert entity.hvac_action is None
    assert entity._sw_device_on is True
    entity._config_entry.options[const.CONF_CLOUD_TELEMETRY_ENABLED] = False
    entity._update_hvac_action()
    assert entity.hvac_action == HVACAction.HEATING


def test_cloud_reporting_works_without_software_control_and_respects_local_off(entity):
    entity._config_entry.options[const.CONF_SW_CONTROL_ENABLED] = False
    entity._config_entry.options[const.CONF_CLOUD_TELEMETRY_ENABLED] = True
    entity._cloud_telemetry = types.SimpleNamespace(heating=True, temperature=23.0, connected=True, age=1)
    entity._hvac_mode = HVACMode.AUTO
    entity._update_hvac_action()
    assert entity.hvac_action == HVACAction.HEATING
    assert entity.extra_state_attributes['cloud_current_temp'] == 23.0
    entity._hvac_mode = HVACMode.OFF
    entity._update_hvac_action()
    assert entity.hvac_action == HVACAction.OFF
