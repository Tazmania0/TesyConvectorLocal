from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .climate import async_setup_entry
from .tesy_convector import TesyConvector
from .const import DOMAIN


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Tesy Convector component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:

    device = TesyConvector(
        entry.data["ip_address"],
        entry.data["model"]
    )


    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    
    # Apply stored correction on startup
    if "temperature_correction" in entry.options:
        await device.set_temperature_correction(entry.options["temperature_correction"])
    
    hass.data[DOMAIN][entry.entry_id] = device
    await hass.config_entries.async_forward_entry_setups(entry, ["climate", "number"])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    return await hass.config_entries.async_forward_entry_unload(entry, "climate")
