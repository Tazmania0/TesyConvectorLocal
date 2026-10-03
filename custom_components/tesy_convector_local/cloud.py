"""Optional, passive MyTESY cloud heating telemetry.

Protocol derived from the MyTESY 2.7 client. No heater commands are published.
"""

import asyncio
from contextlib import suppress
import importlib
import json
import logging
import math
import re
import ssl
from time import monotonic

from .const import CLOUD_TELEMETRY_MAX_AGE_SEC

_LOGGER = logging.getLogger(__name__)
# Paho's debug subscription logs contain the device token in the topic.
# Use a private disabled logger; emit only sanitized lifecycle logs ourselves.
_MQTT_LOGGER = logging.Logger("tesy_cloud_transport")
_MQTT_LOGGER.disabled = True
_REST_URL = "https://ad.mytesy.com/rest"
_CONVECTOR_MODELS = {"cn05uv", "cn06uv", "cn06as"}
_SEGMENT = re.compile(r"[A-Za-z0-9:_.=-]+")


class CloudError(Exception):
    """A sanitized cloud setup error suitable for a config flow."""


def _segment(value):
    return isinstance(value, str) and bool(_SEGMENT.fullmatch(value))


def validate_cloud_config(config):
    """Reject malformed device identities and arbitrary broker addresses."""
    if not isinstance(config, dict):
        raise CloudError("invalid_cloud_response")
    if config.get("model") not in _CONVECTOR_MODELS or not all(
        _segment(config.get(key)) for key in ("mac", "token")
    ):
        raise CloudError("invalid_cloud_response")
    if config.get("host") != "mqtt.tesy.com":
        raise CloudError("invalid_cloud_response")
    port = config.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise CloudError("invalid_cloud_response")
    if not all(isinstance(config.get(key), str) and config[key] for key in ("username", "password")):
        raise CloudError("invalid_cloud_response")


async def async_discover_cloud(session, email, password):
    """Use account credentials transiently to discover this user's convectors."""
    async def request(method, path, **kwargs):
        # Never log bodies, credentials, URLs with query strings, or exceptions.
        try:
            async with asyncio.timeout(15):
                async with session.request(method, _REST_URL + path, **kwargs) as response:
                    if response.status in (401, 403):
                        raise CloudError("invalid_auth")
                    if response.status != 200:
                        raise CloudError("cannot_connect")
                    value = await response.json(content_type=None)
        except CloudError:
            raise
        except Exception:
            raise CloudError("cannot_connect") from None
        if not isinstance(value, dict):
            raise CloudError("invalid_cloud_response")
        return value

    login = await request("POST", "/app-user-login", json={"email": email, "password": password, "lang": "en"})
    if login.get("errors"):
        raise CloudError("invalid_auth")
    if not login.get("userID") or not login.get("email") or not login.get("password"):
        raise CloudError("invalid_cloud_response")
    auth = {"userID": login["userID"], "userEmail": login["email"], "userPass": login["password"], "lang": "en"}
    # GET with these parameters is the vendor client's discovery protocol.
    settings = await request("GET", "/get-app-settings", params=auth)
    devices = await request("GET", "/get-my-devices", params=auth)
    if settings.get("errors") or devices.get("errors"):
        raise CloudError("invalid_auth")
    try:
        port = int(settings["MQTT_PORT"])
        broker = {
            "host": settings["MQTT_PROD_URL"], "port": port,
            "username": settings["MQTT_USERNAME"], "password": settings["MQTT_PASS"],
        }
    except (KeyError, TypeError, ValueError):
        raise CloudError("invalid_cloud_response") from None
    result = {}
    for mac, device in devices.items():
        if not isinstance(device, dict) or device.get("model") not in _CONVECTOR_MODELS:
            continue
        config = {**broker, "mac": mac, "model": device["model"], "token": device.get("token")}
        validate_cloud_config(config)
        result[mac] = {"config": config, "name": str(device.get("deviceName") or device["model"])}
    if not result:
        raise CloudError("no_cloud_devices")
    return result


class CloudTelemetry:
    """Cache only fresh, non-retained telemetry for one selected device."""

    def __init__(self, config, on_update, clock=monotonic):
        validate_cloud_config(config)
        self.config = dict(config)
        self._on_update = on_update
        self._clock = clock
        self._task = None
        self.connected = False
        self._received_at = None
        self._heating = None
        self._temperature = None
        self._expiry_handle = None
        self.topic = "v1/{mac}/response/{model}/{token}/setTempStatistic".format(**config)

    @property
    def age(self):
        return None if self._received_at is None else max(0.0, self._clock() - self._received_at)

    @property
    def fresh(self):
        return self.connected and self.age is not None and self.age <= CLOUD_TELEMETRY_MAX_AGE_SEC

    @property
    def heating(self):
        return self._heating if self.fresh else None

    @property
    def temperature(self):
        return self._temperature if self.fresh else None

    def receive(self, topic, payload, retained=False):
        """Validate identity and content before refreshing the heating timestamp."""
        if topic != self.topic or retained or not isinstance(payload, (str, bytes, bytearray)) or len(payload) > 16384:
            return False
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("code") != 200:
                return False
            values = data.get("payload")
            if not isinstance(values, dict) or values.get("heating") not in ("on", "off"):
                return False
            raw = values.get("currentTemp")
            temperature = None
            if raw is not None and not isinstance(raw, bool):
                with suppress(ValueError, TypeError):
                    candidate = float(raw)
                    if math.isfinite(candidate) and -40 <= candidate <= 80:
                        temperature = candidate
        except (ValueError, TypeError, UnicodeDecodeError):
            return False
        self._heating = values["heating"] == "on"
        self._temperature = temperature
        self._received_at = self._clock()
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
            self._expiry_handle = None
        with suppress(RuntimeError):
            self._expiry_handle = asyncio.get_running_loop().call_later(
                CLOUD_TELEMETRY_MAX_AGE_SEC + 0.1, self._expire
            )
        self._on_update()
        return True

    def _expire(self):
        """Refresh HA even if local HTTP polling is backing off or unavailable."""
        self._expiry_handle = None
        if not self.fresh:
            self._on_update()

    def _set_connected(self, connected):
        self.connected = connected
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
            self._expiry_handle = None
        # A reconnect must wait for a new live report, even if the old one is recent.
        self._received_at = None
        self._heating = self._temperature = None
        self._on_update()

    def start(self, hass):
        if self._task is None:
            self._task = hass.async_create_background_task(self._run(), "Tesy cloud telemetry")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._set_connected(False)

    async def _run(self):
        aiomqtt = await asyncio.to_thread(importlib.import_module, "aiomqtt")

        # Certificate loading can access disk; keep it off HA's event loop.
        tls_context = await asyncio.to_thread(ssl.create_default_context)
        delay = 5
        warned = False
        try:
            while True:
                try:
                    async with aiomqtt.Client(
                        hostname=self.config["host"], port=self.config["port"],
                        username=self.config["username"], password=self.config["password"],
                        transport="websockets", websocket_path="/",
                        tls_context=tls_context, timeout=15, keepalive=60,
                        max_queued_incoming_messages=100,
                        logger=_MQTT_LOGGER,
                    ) as client:
                        # Exact topic: no subscriptions to other devices or accounts.
                        await client.subscribe(self.topic)
                        self._set_connected(True)
                        delay = 5
                        if warned:
                            _LOGGER.info("Tesy cloud telemetry connection restored")
                            warned = False
                        async for message in client.messages:
                            self.receive(str(message.topic), message.payload, message.retain)
                except (aiomqtt.MqttError, OSError):
                    if not warned:
                        _LOGGER.warning("Tesy cloud telemetry unavailable; local control continues")
                        warned = True
                finally:
                    self._set_connected(False)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
        finally:
            self._set_connected(False)
