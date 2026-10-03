"""Tesy Convector device API wrapper.

Provides async methods for communicating with Tesy Convector devices over HTTP, including status, mode, temperature, and advanced features.
"""

import aiohttp
import asyncio
import logging
import json
from time import monotonic

_LOGGER = logging.getLogger(__name__)


class TesyConvector:
    """Client for Tesy Convector device HTTP API."""

    def __init__(self, ip_address, model, session: aiohttp.ClientSession) -> None:
        """Initialize Tesy Convector client.

        Args:
            ip_address (str): Device IP address.
            model (str): Device model name.
        """
        self.base_url = f"http://{ip_address}"
        self.model = model
        self.ip_address = ip_address
        self._session = session
        self._unavailable_logged = False
        self._json_error_logged = False
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def send_command(self, endpoint, payload, source=None):
        """Serialize requests to the heater's limited local HTTP server."""
        async with self._request_lock:
            delay = self._next_request_at - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await self._send_command(endpoint, payload, source)
            finally:
                # The firmware can reset back-to-back connections even when
                # requests don't overlap. Give its HTTP server time to settle.
                self._next_request_at = monotonic() + 0.5

    async def _send_command(self, endpoint, payload, source=None):
        """Send once; never replay a potentially applied control write."""
        url = f"{self.base_url}/{endpoint}"
        started = monotonic()
        source = source or endpoint
        _LOGGER.debug(
            "Tesy request start: ip=%s endpoint=%s source=%s payload=%s",
            self.ip_address, endpoint, source, payload,
        )
        try:
            async with asyncio.timeout(10):
                async with self._session.post(url, json=payload) as response:
                    http_status = response.status
                    _LOGGER.debug(
                        "Tesy response: ip=%s endpoint=%s source=%s http_status=%s elapsed_ms=%.1f",
                        self.ip_address, endpoint, source, http_status,
                        (monotonic() - started) * 1000,
                    )
                    if not 200 <= http_status < 300:
                        if not self._unavailable_logged:
                            _LOGGER.warning(
                                "Tesy request failed: ip=%s endpoint=%s source=%s HTTP %s",
                                self.ip_address, endpoint, source, http_status,
                            )
                            self._unavailable_logged = True
                        return {"error": f"HTTP {http_status}"}
                    try:
                        result = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, json.JSONDecodeError):
                        text_response = await response.text()
                        if not self._json_error_logged:
                            _LOGGER.error(
                                "Invalid JSON: ip=%s endpoint=%s source=%s body=%s",
                                self.ip_address, endpoint, source, text_response,
                            )
                            self._json_error_logged = True
                        return {"error": f"Invalid JSON: {text_response}"}
                    if not isinstance(result, dict):
                        if not self._json_error_logged:
                            _LOGGER.error(
                                "Invalid response object: ip=%s endpoint=%s source=%s",
                                self.ip_address, endpoint, source,
                            )
                            self._json_error_logged = True
                        return {"error": "Expected JSON object"}
                    if "error" not in result:
                        if self._unavailable_logged or self._json_error_logged:
                            _LOGGER.info("Tesy Convector communication restored: ip=%s", self.ip_address)
                        self._unavailable_logged = False
                        self._json_error_logged = False
                    return result
        except (aiohttp.ClientError, TimeoutError) as exc:
            if not self._unavailable_logged:
                _LOGGER.warning(
                    "Tesy Convector unreachable: ip=%s endpoint=%s source=%s elapsed_ms=%.1f error=%s",
                    self.ip_address, endpoint, source, (monotonic() - started) * 1000, exc,
                )
                self._unavailable_logged = True
            return {"error": str(exc)}

    def get_status(self, source=None):
        """Request current device status."""
        return self.send_command("getStatus", {}, source=source)

    def turn_on(self, source=None):
        """Turn the device on."""
        return self.send_command("onOff", {"status": "on"}, source=source)

    def turn_off(self, source=None):
        """Turn the device off."""
        return self.send_command("onOff", {"status": "off"}, source=source)

    def set_mode(self, mode, source=None):
        """Set the device mode (e.g., off, eco, comfort).

        Mode could be: off, eco, comfort, etc.
        """
        return self.send_command("setMode", {"name": mode}, source=source)

    def set_temperature(self, temp, source=None):
        """Set the target temperature."""
        return self.send_command("setTemp", {"temp": temp}, source=source)

    def set_adaptive_start(self, status, source=None):
        """Enable or disable adaptive start.

        Status: on or off
        """

        return self.send_command("setAdaptiveStart", {"status": status}, source=source)

    def set_opened_window(self, status, source=None):
        """Enable or disable window open detection.

        Status: on or off
        """
        return self.send_command("setOpenedWindow", {"status": status}, source=source)

    def set_delayed_start(self, time, temp, source=None):
        """Set delayed start with time and temperature.

        Time in minutes and temperature
        """
        return self.send_command(
            "setDelayedStart", {"status": "on", "time": time, "temp": temp},
            source=source,
        )

    def set_temperature_correction(self, temp, source=None):
        """Set the temperature correction value.

        Use CONF_TEMPERATURE_CORRECTION for key if storing or referencing

        Example: self.data[CONF_TEMPERATURE_CORRECTION] = temp
        """
        return self.send_command("setTCorrection", {"temp": temp}, source=source)

    def set_anti_frost(self, status, source=None):
        """Enable or disable anti-frost mode.

        Status: on or off
        """
        return self.send_command("setAntiFrost", {"status": status}, source=source)

    def set_comfort_temperature(self, temp, source=None):
        """Set comfort mode temperature."""
        return self.send_command("setComfortTemp", {"temp": temp}, source=source)

    def set_eco_temperature(self, temp, time, source=None):
        """Set eco mode temperature and duration.

        Time in minutes
        """
        return self.send_command("setEcoTemp", {"temp": temp, "time": time}, source=source)

    def set_sleep_temperature(self, temp, time, source=None):
        """Set sleep mode temperature and duration.

        Time in minutes
        """
        return self.send_command("setSleepTemp", {"temp": temp, "time": time}, source=source)

    def set_uv(self, status, source=None):
        """Enable or disable UV function.

        Status: on or off
        """
        return self.send_command("setUV", {"status": status}, source=source)

    def lock_device(self, status, source=None):
        """Lock or unlock the device.

        Status: on or off
        """
        return self.send_command("setLockDevice", {"status": status}, source=source)
