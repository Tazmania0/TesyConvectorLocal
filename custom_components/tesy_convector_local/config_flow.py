"""Config flow and options flow for Tesy Convector Local integration.

This module handles the user and options flows for device setup, window detection, and temperature correction parameters.
"""

import asyncio
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    NumberSelector,
    NumberSelectorConfig,
    # SelectSelector,
    # SelectSelectorConfig,
)

from .const import (
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_TEMP_FALL_RATE,
    CONF_TEMP_RECOVERY_ABS,
    CONF_TEMP_RECOVERY_RATE,
    CONF_TEMPERATURE_CORRECTION,
    CONF_TEMPERATURE_ENTITY,
    CONF_USE_INTERNAL_TEMP,
    CONF_WINDOW_OPEN_ENABLED,
    DEFAULT_TEMP_FALL_RATE,
    DEFAULT_TEMP_RECOVERY_ABS,
    #    INTERNAL_TEMP_OPTION,
    #    INTERNAL_TEMP_LABEL,
    DEFAULT_TEMP_RECOVERY_RATE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Schema for the user input (IP Address)
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_IP_ADDRESS): str,
        vol.Required(CONF_MODEL): str,  # Add a model field to the schema
        vol.Optional(CONF_TEMPERATURE_ENTITY): EntitySelector(
            {"domain": "sensor"}  # Allow the user to select a temperature sensor entity
        ),
        # Do NOT include window detection parameters here; they belong in options only
    }
)


class TesyConvectorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow handler for Tesy Convector integration."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial user step for device setup.

        Checks for duplicate entries, tests device connectivity, and creates a config entry.
        """
        errors = {}

        if user_input is not None:
            ip_address = user_input[CONF_IP_ADDRESS]
            model = user_input[CONF_MODEL]

            # Duplicate check first
            for entry in self._async_current_entries():
                existing_ip = entry.data.get(CONF_IP_ADDRESS)
                existing_model = entry.data.get(CONF_MODEL)
                if existing_ip == ip_address and existing_model == model:
                    errors["base"] = "already_configured"
                    break

            # If no duplicate found, test the IP address
            if not errors:
                try:
                    async with asyncio.timeout(3):
                        reader, writer = await asyncio.open_connection(ip_address, 80)
                        writer.close()
                        await writer.wait_closed()
                except TimeoutError:
                    _LOGGER.warning("Connection timed out for %s:%s", ip_address, 80)
                    errors["base"] = "cannot_connect"
                except OSError as e:
                    _LOGGER.warning(
                        "Connection failed to %s:%s - %s", ip_address, 80, str(e)
                    )
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"{model} ({ip_address})", data=user_input
                    )

        # If this is the first time or errors occurred, show the form again
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler for this config entry."""
        return TesyConvectorOptionsFlowHandler()


class TesyConvectorOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow handler for Tesy Convector integration."""

    def __init__(self) -> None:
        """Initialize the options flow handler."""
        # No config_entry assignment needed; provided by base class

    async def async_step_init(self, user_input=None):
        """Show and handle the options form for window detection and correction.

        Dynamically builds the options schema, validates user input, and saves options.
        """
        errors = {}
        options = self.config_entry.options
        current_correction = options.get(CONF_TEMPERATURE_CORRECTION, 0)
        use_internal = options.get(CONF_USE_INTERNAL_TEMP, True)
        current_temp_entity = options.get(CONF_TEMPERATURE_ENTITY)
        window_open_enabled = options.get(CONF_WINDOW_OPEN_ENABLED, False)
        current_fall_rate = options.get(CONF_TEMP_FALL_RATE, DEFAULT_TEMP_FALL_RATE)
        current_recovery_rate = options.get(
            CONF_TEMP_RECOVERY_RATE, DEFAULT_TEMP_RECOVERY_RATE
        )
        current_recovery_abs = options.get(
            CONF_TEMP_RECOVERY_ABS, DEFAULT_TEMP_RECOVERY_ABS
        )

        # Build schema dynamically
        schema_dict = {
            vol.Required(
                CONF_TEMPERATURE_CORRECTION, default=float(current_correction)
            ): NumberSelector(NumberSelectorConfig(min=-4, max=4, step=1, mode="box")),
            vol.Required(CONF_USE_INTERNAL_TEMP, default=use_internal): bool,
        }
        if not use_internal:
            schema_dict[
                vol.Required(CONF_TEMPERATURE_ENTITY, default=current_temp_entity)
            ] = EntitySelector({"domain": "sensor"})
            schema_dict[
                vol.Optional(CONF_WINDOW_OPEN_ENABLED, default=window_open_enabled)
            ] = bool
            if window_open_enabled:
                schema_dict[
                    vol.Optional(CONF_TEMP_FALL_RATE, default=current_fall_rate)
                ] = NumberSelector(
                    NumberSelectorConfig(min=0.1, max=5, step=0.1, mode="box")
                )
                schema_dict[
                    vol.Optional(CONF_TEMP_RECOVERY_RATE, default=current_recovery_rate)
                ] = NumberSelector(
                    NumberSelectorConfig(min=0.1, max=5, step=0.1, mode="box")
                )
                schema_dict[
                    vol.Optional(CONF_TEMP_RECOVERY_ABS, default=current_recovery_abs)
                ] = NumberSelector(
                    NumberSelectorConfig(min=0.1, max=5, step=0.1, mode="box")
                )
        schema = vol.Schema(schema_dict)

        if user_input is not None:
            # Process user input, validate fields, and create entry or re-render form with errors.

            use_internal_new = user_input.get(CONF_USE_INTERNAL_TEMP, use_internal)
            window_open_enabled_new = user_input.get(
                CONF_WINDOW_OPEN_ENABLED, window_open_enabled
            )
            # Validate required fields
            if not use_internal_new and not user_input.get(CONF_TEMPERATURE_ENTITY):
                errors[CONF_TEMPERATURE_ENTITY] = "required"
            # If window detection is enabled, ensure all required fields are present
            if not use_internal_new and window_open_enabled_new:
                for key, _default in [
                    (CONF_TEMP_FALL_RATE, DEFAULT_TEMP_FALL_RATE),
                    (CONF_TEMP_RECOVERY_RATE, DEFAULT_TEMP_RECOVERY_RATE),
                    (CONF_TEMP_RECOVERY_ABS, DEFAULT_TEMP_RECOVERY_ABS),
                ]:
                    if user_input.get(key) is None:
                        errors[key] = "required"
            # Remove irrelevant fields before saving
            cleaned = dict(user_input)
            if use_internal_new:
                for key in [
                    CONF_TEMPERATURE_ENTITY,
                    CONF_WINDOW_OPEN_ENABLED,
                    CONF_TEMP_FALL_RATE,
                    CONF_TEMP_RECOVERY_RATE,
                    CONF_TEMP_RECOVERY_ABS,
                ]:
                    cleaned.pop(key, None)
            elif not window_open_enabled_new:
                for key in [
                    CONF_TEMP_FALL_RATE,
                    CONF_TEMP_RECOVERY_RATE,
                    CONF_TEMP_RECOVERY_ABS,
                ]:
                    cleaned.pop(key, None)
            if not errors:
                # Create the options entry with cleaned user input.
                return self.async_create_entry(title="", data=cleaned)
            # If errors, re-render form with errors and user values
            # Rebuild schema with current user_input
            schema_dict = {
                vol.Required(
                    CONF_TEMPERATURE_CORRECTION,
                    default=float(
                        user_input.get(CONF_TEMPERATURE_CORRECTION, current_correction)
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(min=-4, max=4, step=1, mode="box")
                ),
                vol.Required(CONF_USE_INTERNAL_TEMP, default=use_internal_new): bool,
            }
            if not use_internal_new:
                schema_dict[
                    vol.Required(
                        CONF_TEMPERATURE_ENTITY,
                        default=user_input.get(
                            CONF_TEMPERATURE_ENTITY, current_temp_entity
                        ),
                    )
                ] = EntitySelector({"domain": "sensor"})
                schema_dict[
                    vol.Optional(
                        CONF_WINDOW_OPEN_ENABLED,
                        default=user_input.get(
                            CONF_WINDOW_OPEN_ENABLED, window_open_enabled
                        ),
                    )
                ] = bool
                if window_open_enabled_new:
                    schema_dict[
                        vol.Optional(
                            CONF_TEMP_FALL_RATE,
                            default=user_input.get(
                                CONF_TEMP_FALL_RATE, current_fall_rate
                            ),
                        )
                    ] = NumberSelector(
                        NumberSelectorConfig(min=0.1, max=5, step=0.1, mode="box")
                    )
                    schema_dict[
                        vol.Optional(
                            CONF_TEMP_RECOVERY_RATE,
                            default=user_input.get(
                                CONF_TEMP_RECOVERY_RATE, current_recovery_rate
                            ),
                        )
                    ] = NumberSelector(
                        NumberSelectorConfig(min=0.1, max=5, step=0.1, mode="box")
                    )
                    schema_dict[
                        vol.Optional(
                            CONF_TEMP_RECOVERY_ABS,
                            default=user_input.get(
                                CONF_TEMP_RECOVERY_ABS, current_recovery_abs
                            ),
                        )
                    ] = NumberSelector(
                        NumberSelectorConfig(min=0.1, max=5, step=0.1, mode="box")
                    )
            schema = vol.Schema(schema_dict)
            return self.async_show_form(
                step_id="init",
                data_schema=schema,
                errors=errors,
            )
        # Show the options form with the current schema and no errors.
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
        )
