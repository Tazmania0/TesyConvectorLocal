import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock


class _ClientError(Exception):
    pass


class _ContentTypeError(Exception):
    pass


aiohttp_stub = types.SimpleNamespace(
    ClientError=_ClientError,
    ContentTypeError=_ContentTypeError,
    ClientSession=object,
)
sys.modules.setdefault("aiohttp", aiohttp_stub)

module_path = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesy_convector_local"
    / "tesy_convector.py"
)
spec = importlib.util.spec_from_file_location("tesy_convector_module", module_path)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
TesyConvector = module.TesyConvector


class _NoopTimeout:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _timeout_stub(seconds):  # noqa: ARG001
    return _NoopTimeout()


module.asyncio.timeout = _timeout_stub


class _ResponseContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _MockResponse:
    def __init__(self, json_value=None, json_exception=None, text_value=""):
        self._json_value = json_value
        self._json_exception = json_exception
        self._text_value = text_value

    async def json(self, content_type=None):  # noqa: ARG002
        if self._json_exception:
            raise self._json_exception
        return self._json_value

    async def text(self):
        return self._text_value


class _MockSession:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.called_url = None
        self.called_json = None

    def post(self, url, json):
        self.called_url = url
        self.called_json = json
        if self._error:
            raise self._error
        return _ResponseContext(self._response)


def test_send_command_success_returns_json_and_clears_flags():
    response = _MockResponse(json_value={"ok": True})
    session = _MockSession(response=response)
    client = TesyConvector("192.168.1.20", "CN06AS", session)

    client._unavailable_logged = True
    client._json_error_logged = True

    result = asyncio.run(client.send_command("getStatus", {"a": 1}))

    assert result == {"ok": True}
    assert session.called_url == "http://192.168.1.20/getStatus"
    assert session.called_json == {"a": 1}
    assert client._unavailable_logged is False
    assert client._json_error_logged is False


def test_send_command_invalid_json_returns_error_dict():
    response = _MockResponse(
        json_exception=json.JSONDecodeError("bad", "x", 0), text_value="not-json"
    )
    session = _MockSession(response=response)
    client = TesyConvector("192.168.1.20", "CN06AS", session)

    result = asyncio.run(client.send_command("getStatus", {}))

    assert result == {"error": "Invalid JSON: not-json"}
    assert client._json_error_logged is True


def test_send_command_network_error_returns_error_dict_and_sets_flag():
    session = _MockSession(error=_ClientError("network down"))
    client = TesyConvector("192.168.1.20", "CN06AS", session)

    result = asyncio.run(client.send_command("getStatus", {}))

    assert result == {"error": "network down"}
    assert client._unavailable_logged is True


def test_command_helpers_forward_to_send_command():
    client = TesyConvector("192.168.1.20", "CN06AS", _MockSession(response=_MockResponse(json_value={})))
    client.send_command = AsyncMock(return_value={"ok": True})

    asyncio.run(client.get_status())
    asyncio.run(client.turn_on())
    asyncio.run(client.turn_off())
    asyncio.run(client.set_mode("eco"))
    asyncio.run(client.set_temperature(22.5))
    asyncio.run(client.set_adaptive_start("on"))
    asyncio.run(client.set_opened_window("off"))
    asyncio.run(client.set_delayed_start(30, 20))
    asyncio.run(client.set_temperature_correction(1.0))
    asyncio.run(client.set_anti_frost("on"))
    asyncio.run(client.set_comfort_temperature(24))
    asyncio.run(client.set_eco_temperature(19, 60))
    asyncio.run(client.set_sleep_temperature(18, 90))
    asyncio.run(client.set_uv("off"))
    asyncio.run(client.lock_device("on"))

    assert client.send_command.await_args_list == [
        (("getStatus", {}),),
        (("onOff", {"status": "on"}),),
        (("onOff", {"status": "off"}),),
        (("setMode", {"name": "eco"}),),
        (("setTemp", {"temp": 22.5}),),
        (("setAdaptiveStart", {"status": "on"}),),
        (("setOpenedWindow", {"status": "off"}),),
        (("setDelayedStart", {"status": "on", "time": 30, "temp": 20}),),
        (("setTCorrection", {"temp": 1.0}),),
        (("setAntiFrost", {"status": "on"}),),
        (("setComfortTemp", {"temp": 24}),),
        (("setEcoTemp", {"temp": 19, "time": 60}),),
        (("setSleepTemp", {"temp": 18, "time": 90}),),
        (("setUV", {"status": "off"}),),
        (("setLockDevice", {"status": "on"}),),
    ]
