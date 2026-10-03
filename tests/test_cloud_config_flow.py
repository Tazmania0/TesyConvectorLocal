"""Cloud options flow tests using lightweight HA form stubs."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, Mock

import pytest

from test_cloud import config  # Initializes the lightweight integration package.
from custom_components.tesy_convector_local import cloud, const


@pytest.fixture
def flow(monkeypatch):
    if 'homeassistant' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'homeassistant', types.ModuleType('homeassistant'))
    if 'homeassistant.helpers' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'homeassistant.helpers', types.ModuleType('homeassistant.helpers'))
    class Base:
        def __init_subclass__(cls, **kwargs):
            pass

        def async_show_form(self, **kwargs):
            return {'type': 'form', **kwargs}

        def async_abort(self, **kwargs):
            return {'type': 'abort', **kwargs}

    class Selector:
        def __init__(self, config):
            self.config = config

        def __call__(self, value):
            return value

    monkeypatch.setitem(sys.modules, 'homeassistant.config_entries', types.SimpleNamespace(
        ConfigFlow=Base, OptionsFlow=Base, CONN_CLASS_LOCAL_POLL='local_poll',
    ))
    monkeypatch.setitem(sys.modules, 'homeassistant.helpers.selector', types.SimpleNamespace(
        EntitySelector=Selector, EntitySelectorConfig=dict,
        NumberSelector=Selector, NumberSelectorConfig=dict,
        TextSelector=Selector, TextSelectorConfig=dict,
    ))
    monkeypatch.setitem(sys.modules, 'homeassistant.core', types.SimpleNamespace(callback=lambda f: f))
    monkeypatch.setitem(sys.modules, 'homeassistant.helpers.aiohttp_client', types.SimpleNamespace(async_get_clientsession=lambda hass: object()))
    # `from homeassistant import config_entries` uses the package attribute.
    monkeypatch.setattr(sys.modules['homeassistant'], 'config_entries', sys.modules['homeassistant.config_entries'], raising=False)
    path = Path(__file__).resolve().parents[1] / 'custom_components/tesy_convector_local/config_flow.py'
    spec = importlib.util.spec_from_file_location('custom_components.tesy_convector_local._test_config_flow', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = module.TesyConvectorOptionsFlowHandler()
    handler.config_entry = types.SimpleNamespace(data={'ip_address': '192.0.2.1', 'model': 'CN06AS'}, options={})
    handler.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_update_entry=Mock()))
    return handler


def test_enabling_cloud_stages_options_until_a_device_is_selected(flow, monkeypatch):
    from test_cloud import config
    discovered = {'TEST_MAC': {'name': 'Room heater', 'config': config()}}
    monkeypatch.setattr(cloud, 'async_discover_cloud', AsyncMock(return_value=discovered))
    initial = asyncio.run(flow.async_step_init({
        const.CONF_TEMPERATURE_CORRECTION: 3, const.CONF_USE_EXTERNAL_TEMP: False,
        const.CONF_CLOUD_TELEMETRY_ENABLED: True,
    }))
    assert initial['step_id'] == 'cloud_login'
    assert next(selector for key, selector in initial['data_schema'].schema.items() if key.schema == 'password').config['type'] == 'password'
    flow.hass.config_entries.async_update_entry.assert_not_called()
    picked = asyncio.run(flow.async_step_cloud_login({'email': 'test@example.com', 'password': 'account-secret'}))
    assert picked['step_id'] == 'cloud_device'
    invalid = asyncio.run(flow.async_step_cloud_device({'cloud_device': 'OTHER_MAC'}))
    assert invalid['errors']['base'] == 'invalid_cloud_device'
    result = asyncio.run(flow.async_step_cloud_device({'cloud_device': 'TEST_MAC'}))
    assert result['type'] == 'abort'
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs
    assert saved['options'][const.CONF_CLOUD_TELEMETRY_ENABLED]
    assert saved['data'][const.CONF_CLOUD_TELEMETRY] == config()
    assert saved['data']['ip_address'] == '192.0.2.1'
    assert 'account-secret' not in str(saved)


def test_auth_error_does_not_change_running_options(flow, monkeypatch):
    monkeypatch.setattr(cloud, 'async_discover_cloud', AsyncMock(side_effect=cloud.CloudError('invalid_auth')))
    result = asyncio.run(flow.async_step_cloud_login({'email': 'test@example.com', 'password': 'wrong'}))
    assert result['errors']['base'] == 'invalid_auth'
    flow.hass.config_entries.async_update_entry.assert_not_called()


def test_existing_cloud_credentials_can_be_disabled_and_reenabled_without_login(flow):
    from test_cloud import config
    flow.config_entry.data[const.CONF_CLOUD_TELEMETRY] = config()
    for enabled in [False, True]:
        result = asyncio.run(flow.async_step_init({
            const.CONF_TEMPERATURE_CORRECTION: 3, const.CONF_USE_EXTERNAL_TEMP: False,
            const.CONF_CLOUD_TELEMETRY_ENABLED: enabled,
        }))
        assert result['type'] == 'abort'
        assert flow.hass.config_entries.async_update_entry.call_args.kwargs['options'][const.CONF_CLOUD_TELEMETRY_ENABLED] is enabled


def test_reconfigure_prompts_for_a_new_cloud_account(flow):
    from test_cloud import config
    flow.config_entry.data[const.CONF_CLOUD_TELEMETRY] = config()
    result = asyncio.run(flow.async_step_init({
        const.CONF_TEMPERATURE_CORRECTION: 3, const.CONF_USE_EXTERNAL_TEMP: False,
        const.CONF_CLOUD_TELEMETRY_ENABLED: True, const.CONF_CLOUD_TELEMETRY_RECONFIGURE: True,
    }))
    assert result['step_id'] == 'cloud_login'
