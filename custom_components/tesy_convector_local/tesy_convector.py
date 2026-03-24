"""Tesy Convector device API wrapper.

Provides async methods for communicating with Tesy Convector devices over HTTP, including status, mode, temperature, and advanced features.
"""

import aiohttp
import asyncio
import logging
import json
# from .const import CONF_TEMPERATURE_CORRECTION

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

    async def send_command(self, endpoint, payload):
        """Send a command to the device and return the response.

        On transient network errors (connection reset, timeout) returns an
        error dict immediately — no inline sleep/retry here.  The caller
        (async_update in climate.py) tracks the failure time and skips polls
        for 30 seconds before trying again, keeping trying indefinitely until
        the device responds.

        Invalid JSON is returned as-is — not a transient issue.

        Args:
            endpoint (str): API endpoint name.
            payload (dict): JSON payload for the request.
        Returns:
            dict: Parsed JSON response or {"error": ...} on failure.
        """
        url = f"{self.base_url}/{endpoint}"

        try:
            async with asyncio.timeout(10):
                async with self._session.post(url, json=payload) as response:
                        try:
                            result = await response.json(content_type=None)
                            # Successful response — clear error flags
                            if self._unavailable_logged or self._json_error_logged:
                                _LOGGER.info("Tesy Convector communication restored")
                                self._unavailable_logged = False
                                self._json_error_logged = False
                            return result
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            text_response = await response.text()
                            if not self._json_error_logged:
                                _LOGGER.error(
                                    "Invalid JSON from Tesy Convector: %s",
                                    text_response,
                                )
                                self._json_error_logged = True
                            return {"error": f"Invalid JSON: {text_response}"}

        except (aiohttp.ClientError, TimeoutError) as e:
            if not self._unavailable_logged:
                _LOGGER.warning("Tesy Convector unreachable: %s", e)
                self._unavailable_logged = True
            return {"error": str(e)}

    def get_status(self):
        """Request current device status."""
        return self.send_command("getStatus", {})

    def turn_on(self):
        """Turn the device on."""
        return self.send_command("onOff", {"status": "on"})

    def turn_off(self):
        """Turn the device off."""
        return self.send_command("onOff", {"status": "off"})

    def set_mode(self, mode):
        """Set the device mode (e.g., off, eco, comfort).

        Mode could be: off, eco, comfort, etc.
        """
        return self.send_command("setMode", {"name": mode})

    def set_temperature(self, temp):
        """Set the target temperature."""
        return self.send_command("setTemp", {"temp": temp})

    def set_adaptive_start(self, status):
        """Enable or disable adaptive start.

        Status: on or off
        """

        return self.send_command("setAdaptiveStart", {"status": status})

    def set_opened_window(self, status):
        """Enable or disable window open detection.

        Status: on or off
        """
        return self.send_command("setOpenedWindow", {"status": status})

    def set_delayed_start(self, time, temp):
        """Set delayed start with time and temperature.

        Time in minutes and temperature
        """
        return self.send_command(
            "setDelayedStart", {"status": "on", "time": time, "temp": temp}
        )

    def set_temperature_correction(self, temp):
        """Set the temperature correction value.

        Use CONF_TEMPERATURE_CORRECTION for key if storing or referencing

        Example: self.data[CONF_TEMPERATURE_CORRECTION] = temp
        """
        return self.send_command("setTCorrection", {"temp": temp})

    def set_anti_frost(self, status):
        """Enable or disable anti-frost mode.

        Status: on or off
        """
        return self.send_command("setAntiFrost", {"status": status})

    def set_comfort_temperature(self, temp):
        """Set comfort mode temperature."""
        return self.send_command("setComfortTemp", {"temp": temp})

    def set_eco_temperature(self, temp, time):
        """Set eco mode temperature and duration.

        Time in minutes
        """
        return self.send_command("setEcoTemp", {"temp": temp, "time": time})

    def set_sleep_temperature(self, temp, time):
        """Set sleep mode temperature and duration.

        Time in minutes
        """
        return self.send_command("setSleepTemp", {"temp": temp, "time": time})

    def set_uv(self, status):
        """Enable or disable UV function.

        Status: on or off
        """
        return self.send_command("setUV", {"status": status})

    def lock_device(self, status):
        """Lock or unlock the device.

        Status: on or off
        """
        return self.send_command("setLockDevice", {"status": status})
