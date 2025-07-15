"""Integration for Tesy Convector heaters (local control)."""

# from .climate import async_setup_entry

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_IP_ADDRESS, CONF_MODEL, CONF_TEMPERATURE_CORRECTION, DOMAIN
from .tesy_convector import TesyConvector


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Tesy Convector component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tesy Convector from a config entry.

    Initializes the device, applies temperature correction, and forwards entry setups to platforms.
    """
    device = TesyConvector(entry.data[CONF_IP_ADDRESS], entry.data[CONF_MODEL])

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # Apply stored correction on startup
    if CONF_TEMPERATURE_CORRECTION in entry.options:
        await device.set_temperature_correction(
            entry.options[CONF_TEMPERATURE_CORRECTION]
        )

    hass.data[DOMAIN][entry.entry_id] = {"device": device}
    await hass.config_entries.async_forward_entry_setups(
        entry, ["climate", "number", "binary_sensor"]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    return await hass.config_entries.async_forward_entry_unload(entry, "climate")
