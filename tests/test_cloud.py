"""MyTESY telemetry regressions without network or account credentials."""

import asyncio
import json
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest

# These protocol tests need no installed Home Assistant runtime.
_ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault('custom_components', types.ModuleType('custom_components'))
sys.modules.setdefault('custom_components.tesy_convector_local', types.ModuleType(
    'custom_components.tesy_convector_local'
)).__path__ = [str(_ROOT / 'custom_components/tesy_convector_local')]

from custom_components.tesy_convector_local.cloud import (
    CloudError, CloudTelemetry, async_discover_cloud, validate_cloud_config,
)
from custom_components.tesy_convector_local.const import CLOUD_TELEMETRY_MAX_AGE_SEC


def config(**changes):
    return {
        'mac': 'TEST_MAC', 'model': 'cn05uv', 'token': 'test-token',
        'host': 'mqtt.tesy.com', 'port': 8083,
        'username': 'test-broker-user', 'password': 'test-broker-secret', **changes,
    }


def report(**changes):
    return json.dumps({'code': 200, 'payload': {'heating': 'on', 'currentTemp': 22.5, **changes}}).encode()


def test_telemetry_freshness_is_about_heating_reports_not_connection():
    clock = types.SimpleNamespace(now=100.0)
    telemetry = CloudTelemetry(config(), Mock(), clock=lambda: clock.now)
    assert telemetry.heating is None
    telemetry._set_connected(True)
    assert telemetry.heating is None
    assert telemetry.receive(telemetry.topic, report())
    assert telemetry.heating is True
    assert telemetry.temperature == 22.5
    clock.now += CLOUD_TELEMETRY_MAX_AGE_SEC + 1
    assert telemetry.connected
    assert telemetry.heating is None
    assert telemetry.temperature is None
    assert telemetry.receive(telemetry.topic, report(heating='off'))
    assert telemetry.heating is False
    telemetry._set_connected(False)
    assert telemetry.heating is None
    telemetry._set_connected(True)
    assert telemetry.heating is None


@pytest.mark.parametrize('payload', [None, b'bad JSON', b'[]', b'{"code":500}',
                                    b'{"code":200,"payload":null}', report(heating='heating'),
                                    b'x' * 16385, b'\xff'])
def test_bad_reports_cannot_refresh_old_heating(payload):
    telemetry = CloudTelemetry(config(), Mock())
    assert not telemetry.receive(telemetry.topic, payload)
    assert telemetry.age is None


def test_wrong_device_token_model_or_retained_report_is_ignored():
    telemetry = CloudTelemetry(config(), Mock())
    for changes in [{'mac': 'OTHER_MAC'}, {'model': 'cn06uv'}, {'token': 'wrong-token'}]:
        other = CloudTelemetry(config(**changes), Mock())
        assert not telemetry.receive(other.topic, report())
    assert not telemetry.receive(telemetry.topic, report(), retained=True)
    assert telemetry.age is None


@pytest.mark.parametrize('temperature', [True, 'NaN', 'Infinity', 100, -50, None, 'invalid'])
def test_bad_temperature_does_not_discard_a_valid_heating_report(temperature):
    telemetry = CloudTelemetry(config(), Mock())
    telemetry._set_connected(True)
    assert telemetry.receive(telemetry.topic, report(currentTemp=temperature))
    assert telemetry.heating is True
    assert telemetry.temperature is None


@pytest.mark.parametrize('changes', [{'mac': '#'}, {'token': 'bad/+/token'}, {'model': 'boiler'},
                                    {'host': 'untrusted.example'}, {'port': True}, {'port': 0},
                                    {'password': None}])
def test_connection_settings_cannot_expand_the_subscription_or_redirect_it(changes):
    with pytest.raises(CloudError):
        validate_cloud_config(config(**changes))


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


def discovery_responses():
    return [
        Response({'userID': 1, 'email': 'test@example.com', 'password': 'returned-auth-value'}),
        Response({'MQTT_PROD_URL': 'mqtt.tesy.com', 'MQTT_PORT': '8083',
                  'MQTT_USERNAME': 'test-broker-user', 'MQTT_PASS': 'test-broker-secret'}),
        Response({'TEST_MAC': {'model': 'cn05uv', 'token': 'test-token', 'deviceName': 'Room heater'},
                  'UNRELATED': {'model': 'B15'}}),
    ]


def test_discovery_uses_vendor_auth_response_and_does_not_save_account_password():
    session = Session(discovery_responses())
    result = asyncio.run(async_discover_cloud(session, 'test@example.com', 'account-secret'))
    assert list(result) == ['TEST_MAC']
    assert result['TEST_MAC']['config'] == config()
    assert session.calls[1][2]['params']['userPass'] == 'returned-auth-value'
    assert 'account-secret' not in json.dumps(result)
    assert 'returned-auth-value' not in json.dumps(result)


@pytest.mark.parametrize('response,error', [(Response({'errors': {'email': ['Rejected']}}), 'invalid_auth'),
                                           (Response({}, 401), 'invalid_auth'),
                                           (Response({}, 503), 'cannot_connect'),
                                           (Response([]), 'invalid_cloud_response'),
                                           (Response({}), 'invalid_cloud_response')])
def test_discovery_errors_are_sanitized(response, error):
    session = Session([response])
    with pytest.raises(CloudError, match=error) as caught:
        asyncio.run(async_discover_cloud(session, 'private@example.com', 'private-secret'))
    assert 'private' not in str(caught.value)


def test_passive_client_subscribes_only_selected_topic_and_closes_on_unload(monkeypatch):
    class MqttError(Exception):
        pass

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.subscribed = []
            self.closed = False
            self.messages = self.stream()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def subscribe(self, topic):
            self.subscribed.append(topic)

        async def stream(self):
            yield types.SimpleNamespace(topic=telemetry.topic, payload=report(), retain=False)
            delivered.set()
            await asyncio.Event().wait()

    clients = []
    def make_client(**kwargs):
        client = Client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setitem(__import__('sys').modules, 'aiomqtt', types.SimpleNamespace(Client=make_client, MqttError=MqttError))
    async def run():
        nonlocal telemetry, delivered
        delivered = asyncio.Event()
        telemetry = CloudTelemetry(config(), Mock())
        hass = types.SimpleNamespace(async_create_background_task=lambda coro, name: asyncio.create_task(coro))
        telemetry.start(hass)
        await asyncio.wait_for(delivered.wait(), 3)
        assert telemetry.heating is True
        assert clients[0].subscribed == [telemetry.topic]
        assert clients[0].kwargs['transport'] == 'websockets'
        assert clients[0].kwargs['websocket_path'] == '/'
        assert clients[0].kwargs['tls_context'].check_hostname
        await telemetry.stop()
        assert clients[0].closed
        assert telemetry.heating is None
    telemetry = delivered = None
    asyncio.run(run())


def test_stale_reports_notify_ha_even_without_local_polls(monkeypatch):
    from custom_components.tesy_convector_local import cloud
    monkeypatch.setattr(cloud, 'CLOUD_TELEMETRY_MAX_AGE_SEC', 0.02)

    async def run():
        expired = asyncio.Event()
        def updated():
            if telemetry.age is not None and not telemetry.fresh:
                expired.set()
        telemetry = CloudTelemetry(config(), updated)
        telemetry._set_connected(True)
        assert telemetry.receive(telemetry.topic, report())
        assert telemetry.heating is True
        await asyncio.wait_for(expired.wait(), 2)
        assert telemetry.heating is None
        await telemetry.stop()
    asyncio.run(run())


def test_broker_failure_retries_without_logging_credentials(monkeypatch, caplog):
    from custom_components.tesy_convector_local import cloud
    class MqttError(Exception):
        pass

    attempts = []
    subscribed = []
    waits = []
    real_sleep = asyncio.sleep

    class Client:
        def __init__(self, **kwargs):
            attempts.append(kwargs)
            self.messages = self.stream()

        async def __aenter__(self):
            if len(attempts) == 1:
                raise MqttError('test-token test-broker-secret')
            return self

        async def __aexit__(self, *args):
            pass

        async def subscribe(self, topic):
            subscribed.append(topic)

        async def stream(self):
            yield types.SimpleNamespace(topic=telemetry.topic, payload=report(), retain=False)
            delivered.set()
            await asyncio.Event().wait()

    async def short_sleep(delay):
        waits.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(cloud.asyncio, 'sleep', short_sleep)
    monkeypatch.setitem(__import__('sys').modules, 'aiomqtt', types.SimpleNamespace(Client=Client, MqttError=MqttError))
    async def run():
        nonlocal telemetry, delivered
        delivered = asyncio.Event()
        telemetry = CloudTelemetry(config(), Mock())
        telemetry.start(types.SimpleNamespace(async_create_background_task=lambda coro, name: asyncio.create_task(coro)))
        await asyncio.wait_for(delivered.wait(), 3)
        assert telemetry.heating is True
        assert waits == [5]
        assert subscribed == [telemetry.topic]
        assert attempts[1]['logger'].disabled
        await telemetry.stop()
    telemetry = delivered = None
    asyncio.run(run())
    assert 'test-token' not in caplog.text
    assert 'test-broker-secret' not in caplog.text
