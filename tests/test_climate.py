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
sys.modules.setdefault('homeassistant.components.climate', types.SimpleNamespace(ClimateEntity=ClimateEntity))
sys.modules.setdefault('homeassistant.components.climate.const', types.SimpleNamespace(
    HVACMode=HVACMode, HVACAction=HVACAction, ClimateEntityFeature=ClimateEntityFeature,
))
sys.modules.setdefault('homeassistant.const', types.SimpleNamespace(UnitOfTemperature=types.SimpleNamespace(CELSIUS='°C')))
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

    async def set_temperature(self, value):
        self.calls.append(('temperature', value))
        self.setpoint = value
        return {}

    async def set_mode(self, mode):
        self.calls.append(('mode', mode))
        self.mode = mode
        self.on = True
        return {}

    async def turn_off(self):
        self.calls.append(('off',))
        self.on = False

    async def set_opened_window(self, value):
        self.calls.append(('window', value))

    async def get_status(self):
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
    assert entity.convector.setpoint == 21
    before = len(entity.convector.calls)
    run(entity, 21.8)
    assert len(entity.convector.calls) == before
    assert not any(call[0] == 'off' for call in entity.convector.calls)


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
    assert entity.convector.setpoint == 21
    entity._config_entry.options[const.CONF_SW_CONTROL_ENABLED] = False
    poll(entity, 23.0)
    assert entity.convector.setpoint == 22
    assert not entity._sw_fallback_active
    assert entity._sw_device_on is None


@pytest.mark.parametrize('mode', [HVACMode.OFF, HVACMode.AUTO])
def test_leaving_heat_restores_sw_off_setpoint(entity, mode):
    entity._target_temp = 22.5
    poll(entity, 23.0)
    assert entity.convector.setpoint == 21
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
    with pytest.raises(RuntimeError, match='restore firmware target'):
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
    with pytest.raises(RuntimeError, match='enable firmware heating'):
        asyncio.run(entity._enter_firmware_fallback('missing'))
    assert not entity._sw_fallback_active
    asyncio.run(entity._enter_firmware_fallback('missing'))
    assert entity._sw_fallback_active


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
