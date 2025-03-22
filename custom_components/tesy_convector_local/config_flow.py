import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import EntitySelector
from .const import DOMAIN

import asyncio
import logging
import async_timeout

_LOGGER = logging.getLogger(__name__)


# Schema for the user input (IP Address)
STEP_USER_DATA_SCHEMA = vol.Schema({
    vol.Required("ip_address"): str,
    vol.Required("model"): str, # Add a model field to the schema
    vol.Optional("temperature_entity"): EntitySelector(
        {"domain": "sensor"}  # Allow the user to select a temperature sensor entity
    )
})


class TesyConvectorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tesy Convector."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            ip_address = user_input["ip_address"]
            model = user_input["model"]

            # Duplicate check first
            for entry in self._async_current_entries():
                existing_ip = entry.data.get("ip_address")
                existing_model = entry.data.get("model")

                if existing_ip == ip_address and existing_model == model:
                    errors["base"] = "already_configured"
                    break

            # If no duplicate found, test the IP address
            if not errors:
                valid = await self._test_ip_address(ip_address)
                if valid:
                    return self.async_create_entry(
                          title=f"{model} ({ip_address})",
                          data=user_input
                      )
                else:
                     errors["base"] = "cannot_connect"

        # If this is the first time or errors occurred, show the form again

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    
    async def _test_ip_address(self, ip_address, port=80, timeout_sec=3):
        """Test if we can connect to the convector."""
        try:
            async with async_timeout.timeout(timeout_sec):
                reader, writer = await asyncio.open_connection(ip_address, port)
                writer.close()
                await writer.wait_closed()
            return True
        except asyncio.TimeoutError:
            _LOGGER.warning("Connection timed out for %s:%s", ip_address, port)
            return False
        except Exception as e:
            _LOGGER.warning("Connection failed to %s:%s - %s", ip_address, port, str(e))
            return False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return TesyConvectorOptionsFlowHandler()


class TesyConvectorOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Tesy Convector options flow."""

#    def __init__(self, config_entry):
#        """Initialize options flow."""
#        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        # Remove the __init__ method entirely
        # The config_entry is already available via self._config_entry
        # Access the config entry via self._config_entry
        current_correction = self.config_entry.options.get("temperature_correction", 0)
        
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    "temperature_correction",
                    default=current_correction
                ): vol.All(vol.Coerce(int), vol.Clamp(min=-4, max=4))
            })
        )

