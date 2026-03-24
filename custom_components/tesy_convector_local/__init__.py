"""Integration for Tesy Convector heaters (local control)."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_TEMPERATURE_CORRECTION,
    DOMAIN,
)
from .tesy_convector import TesyConvector

_PLATFORMS = ["climate", "number", "binary_sensor"]


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
    if correction is not None:
        await device.set_temperature_correction(correction)

    hass.data[DOMAIN][entry.entry_id] = {"device": device}
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply updated options live without reloading the config entry."""
    correction = entry.options.get(CONF_TEMPERATURE_CORRECTION)
    if correction is not None:
        device = hass.data[DOMAIN][entry.entry_id]["device"]
        await device.set_temperature_correction(correction)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
