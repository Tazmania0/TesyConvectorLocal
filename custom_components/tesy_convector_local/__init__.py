"""Integration for Tesy Convector heaters (local control)."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_CLOUD_TELEMETRY,
    CONF_CLOUD_TELEMETRY_ENABLED,
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_TEMPERATURE_CORRECTION,
    DOMAIN,
)
from .tesy_convector import TesyConvector

_PLATFORMS = ["climate", "number", "binary_sensor", "sensor"]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tesy Convector from a config entry."""
    session = async_get_clientsession(hass)
    device = TesyConvector(entry.data[CONF_IP_ADDRESS], entry.data[CONF_MODEL], session)

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    correction = entry.options.get(
        CONF_TEMPERATURE_CORRECTION,
        entry.data.get(CONF_TEMPERATURE_CORRECTION),
    )
    data = {"device": device, "correction_pending": correction is not None}
    if correction is not None and getattr(hass, "is_running", True):
        result = await device.set_temperature_correction(correction)
        if not isinstance(result, dict) or "error" not in result:
            data["applied_correction"] = correction
            data["correction_pending"] = False

    hass.data[DOMAIN][entry.entry_id] = data
    # Diagnostic entities depend on the climate instance. HA may set up the
    # platforms in a single batch concurrently, so establish climate first.
    await hass.config_entries.async_forward_entry_setups(entry, ["climate"])
    await _async_sync_cloud_telemetry(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS[1:])
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply updated options live without reloading the config entry."""
    correction = entry.options.get(CONF_TEMPERATURE_CORRECTION)
    data = hass.data[DOMAIN][entry.entry_id]
    if getattr(hass, "is_running", True) and correction is not None and correction != data.get("applied_correction"):
        result = await data["device"].set_temperature_correction(correction)
        if not isinstance(result, dict) or "error" not in result:
            data["applied_correction"] = correction
            data["correction_pending"] = False
    await _async_sync_cloud_telemetry(hass, entry)


async def _async_sync_cloud_telemetry(hass, entry):
    """Start/stop telemetry live, without rebooting the local controller."""
    from .cloud import CloudError, CloudTelemetry

    data = hass.data[DOMAIN][entry.entry_id]
    telemetry = data.get("cloud_telemetry")
    config = entry.data.get(CONF_CLOUD_TELEMETRY)
    enabled = entry.options.get(CONF_CLOUD_TELEMETRY_ENABLED, False) and config is not None
    if telemetry is not None and enabled and telemetry.config == config:
        return
    if telemetry is not None:
        await telemetry.stop()
        data.pop("cloud_telemetry", None)
    climate = data.get("climate_entity")
    if climate is None:
        return
    climate._cloud_telemetry = None
    if enabled:
        def on_update():
            climate._update_hvac_action()
            climate.async_write_ha_state()

        try:
            telemetry = CloudTelemetry(config, on_update)
        except CloudError:
            _LOGGER.warning("Tesy cloud telemetry configuration invalid; reconnect it in options. Local control continues")
        else:
            data["cloud_telemetry"] = climate._cloud_telemetry = telemetry
            telemetry.start(hass)
    climate._update_hvac_action()
    climate.async_write_ha_state()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    telemetry = hass.data[DOMAIN][entry.entry_id].get("cloud_telemetry")
    if telemetry is not None:
        await telemetry.stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN] and hass.services.has_service(DOMAIN, "set_opened_window"):
            hass.services.async_remove(DOMAIN, "set_opened_window")
    elif telemetry is not None:
        telemetry.start(hass)
    return unload_ok
