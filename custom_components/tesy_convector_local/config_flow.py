"""Config flow and options flow for Tesy Convector Local integration.

Design rules:
- Initial config flow (async_step_user) collects ip_address, model, and
  temperature_correction so the offset is available immediately even before
  the options flow is ever opened, and even when no external sensor is used.
- Options flow owns all runtime configuration. Temperature correction is always
  shown at the top regardless of external-sensor or sw-control settings so it
  can be adjusted in pure firmware-control mode too.
- Schema built once via _build_options_schema() — no duplication.

v0.3 additions:
- sw_control_enabled, sw_min_on_sec, sw_min_off_sec, sw_hysteresis appear
  when use_external_temp is True (external sensor required for sw-control).
- temperature_correction always visible — integer steps, range -4..+4.
"""

import asyncio
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
)

from .const import (
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_SW_CONTROL_ENABLED,
    CONF_SW_HYSTERESIS,
    CONF_SW_I_GAIN,
    CONF_SW_EMA_ALPHA,
    CONF_SW_LAG_TIME,
    CONF_SW_MIN_OFF_SEC,
    CONF_SW_MIN_ON_SEC,
    CONF_TEMP_FALL_RATE,
    CONF_TEMP_RECOVERY_ABS,
    CONF_TEMP_RECOVERY_RATE,
    CONF_TEMPERATURE_CORRECTION,
    CONF_TEMPERATURE_ENTITY,
    CONF_USE_EXTERNAL_TEMP,
    CONF_WINDOW_OPEN_ENABLED,
    DEFAULT_SW_HYSTERESIS,
    DEFAULT_SW_I_GAIN,
    DEFAULT_SW_EMA_ALPHA,
    DEFAULT_SW_LAG_TIME,
    DEFAULT_SW_MIN_OFF_SEC,
    DEFAULT_SW_MIN_ON_SEC,
    DEFAULT_TEMP_FALL_RATE,
    DEFAULT_TEMP_RECOVERY_ABS,
    DEFAULT_TEMP_RECOVERY_RATE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Initial setup: device identity + correction so offset works immediately
# even before the options flow is opened and even without an external sensor.
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_IP_ADDRESS): str,
        vol.Required(CONF_MODEL): str,
        vol.Optional(CONF_TEMPERATURE_CORRECTION, default=0): NumberSelector(
            NumberSelectorConfig(min=-4, max=4, step=1, mode="box")
        ),
    }
)

_CORRECTION_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=-4, max=4, step=1, mode="box")
)


def _build_options_schema(
    correction: int,
    use_external: bool,
    temp_entity,
    window_open_enabled: bool,
    fall_rate: float,
    recovery_rate: float,
    recovery_abs: float,
    sw_control_enabled: bool,
    sw_min_on: float,
    sw_min_off: float,
    sw_hysteresis: float,
    sw_lag_time: float,
    sw_i_gain: float,
    sw_ema_alpha: float,
) -> vol.Schema:
    """Build options schema.

    temperature_correction is always the first field so it is accessible
    regardless of external-sensor or sw-control configuration — the device
    uses it in firmware-control mode too.
    """
    schema_dict: dict = {
        # Always shown — works in firmware-control mode without any external sensor
        vol.Required(
            CONF_TEMPERATURE_CORRECTION, default=int(correction)
        ): _CORRECTION_SELECTOR,
        vol.Required(CONF_USE_EXTERNAL_TEMP, default=use_external): bool,
    }

    if use_external:
        schema_dict[
            vol.Required(CONF_TEMPERATURE_ENTITY, default=temp_entity)
        ] = EntitySelector(EntitySelectorConfig(domain="sensor"))

        # --- Window detection ---
        schema_dict[
            vol.Optional(CONF_WINDOW_OPEN_ENABLED, default=window_open_enabled)
        ] = bool

        if window_open_enabled:
            schema_dict[
                vol.Optional(CONF_TEMP_FALL_RATE, default=float(fall_rate))
            ] = NumberSelector(NumberSelectorConfig(min=0.1, max=5, step=0.1, mode="box"))
            schema_dict[
                vol.Optional(CONF_TEMP_RECOVERY_RATE, default=float(recovery_rate))
            ] = NumberSelector(NumberSelectorConfig(min=0.1, max=5, step=0.1, mode="box"))
            schema_dict[
                vol.Optional(CONF_TEMP_RECOVERY_ABS, default=float(recovery_abs))
            ] = NumberSelector(NumberSelectorConfig(min=0.1, max=5, step=0.1, mode="box"))

        # --- v0.3 Software on/off controller ---
        schema_dict[
            vol.Optional(CONF_SW_CONTROL_ENABLED, default=sw_control_enabled)
        ] = bool

        if sw_control_enabled:
            schema_dict[
                vol.Optional(CONF_SW_MIN_ON_SEC, default=float(sw_min_on))
            ] = NumberSelector(
                NumberSelectorConfig(min=10, max=300, step=10, mode="box",
                                     unit_of_measurement="s")
            )
            schema_dict[
                vol.Optional(CONF_SW_MIN_OFF_SEC, default=float(sw_min_off))
            ] = NumberSelector(
                NumberSelectorConfig(min=10, max=300, step=10, mode="box",
                                     unit_of_measurement="s")
            )
            schema_dict[
                vol.Optional(CONF_SW_HYSTERESIS, default=float(sw_hysteresis))
            ] = NumberSelector(
                NumberSelectorConfig(min=0.1, max=2.0, step=0.1, mode="box",
                                     unit_of_measurement="°C")
            )
            schema_dict[
                vol.Optional(CONF_SW_LAG_TIME, default=float(sw_lag_time))
            ] = NumberSelector(
                NumberSelectorConfig(min=0.5, max=30.0, step=0.5, mode="box",
                                     unit_of_measurement="min")
            )
            schema_dict[
                vol.Optional(CONF_SW_I_GAIN, default=float(sw_i_gain))
            ] = NumberSelector(
                NumberSelectorConfig(min=0.0, max=1.0, step=0.05, mode="slider")
            )
            schema_dict[
                vol.Optional(CONF_SW_EMA_ALPHA, default=float(sw_ema_alpha))
            ] = NumberSelector(
                NumberSelectorConfig(min=0.05, max=1.0, step=0.05, mode="slider")
            )

    return vol.Schema(schema_dict)


class TesyConvectorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow handler."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            ip_address = user_input[CONF_IP_ADDRESS]
            model = user_input[CONF_MODEL]

            for entry in self._async_current_entries():
                if (
                    entry.data.get(CONF_IP_ADDRESS) == ip_address
                    and entry.data.get(CONF_MODEL) == model
                ):
                    errors["base"] = "already_configured"
                    break

            if not errors:
                try:
                    async with asyncio.timeout(3):
                        _reader, writer = await asyncio.open_connection(ip_address, 80)
                        writer.close()
                        await writer.wait_closed()
                except TimeoutError:
                    _LOGGER.warning("Connection timed out for %s:80", ip_address)
                    errors["base"] = "cannot_connect"
                except OSError as e:
                    _LOGGER.warning("Connection failed to %s:80 - %s", ip_address, e)
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"{model} ({ip_address})", data=user_input
                    )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TesyConvectorOptionsFlowHandler()


class TesyConvectorOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow handler."""

    async def async_step_init(self, user_input=None):
        errors = {}
        options = self.config_entry.options

        # Current saved values — fall back to entry.data for correction so a
        # value set during initial setup is not lost if options were never saved.
        current_correction = int(options.get(
            CONF_TEMPERATURE_CORRECTION,
            self.config_entry.data.get(CONF_TEMPERATURE_CORRECTION, 0)
        ))
        use_external = options.get(CONF_USE_EXTERNAL_TEMP, False)
        current_temp_entity = options.get(CONF_TEMPERATURE_ENTITY)
        window_open_enabled = options.get(CONF_WINDOW_OPEN_ENABLED, False)
        current_fall_rate = options.get(CONF_TEMP_FALL_RATE, DEFAULT_TEMP_FALL_RATE)
        current_recovery_rate = options.get(CONF_TEMP_RECOVERY_RATE, DEFAULT_TEMP_RECOVERY_RATE)
        current_recovery_abs = options.get(CONF_TEMP_RECOVERY_ABS, DEFAULT_TEMP_RECOVERY_ABS)
        sw_control_enabled = options.get(CONF_SW_CONTROL_ENABLED, False)
        sw_min_on = options.get(CONF_SW_MIN_ON_SEC, DEFAULT_SW_MIN_ON_SEC)
        sw_min_off = options.get(CONF_SW_MIN_OFF_SEC, DEFAULT_SW_MIN_OFF_SEC)
        sw_hysteresis = options.get(CONF_SW_HYSTERESIS, DEFAULT_SW_HYSTERESIS)
        sw_lag_time  = options.get(CONF_SW_LAG_TIME,  DEFAULT_SW_LAG_TIME)
        sw_ema_alpha = options.get(CONF_SW_EMA_ALPHA, DEFAULT_SW_EMA_ALPHA)
        sw_i_gain   = options.get(CONF_SW_I_GAIN,   DEFAULT_SW_I_GAIN)

        if user_input is not None:
            use_external_new = user_input.get(CONF_USE_EXTERNAL_TEMP, False)
            window_open_enabled_new = user_input.get(CONF_WINDOW_OPEN_ENABLED, False)
            sw_control_new = user_input.get(CONF_SW_CONTROL_ENABLED, False)

            if use_external_new and not user_input.get(CONF_TEMPERATURE_ENTITY):
                errors[CONF_TEMPERATURE_ENTITY] = "required"

            if use_external_new and window_open_enabled_new:
                for key in [CONF_TEMP_FALL_RATE, CONF_TEMP_RECOVERY_RATE, CONF_TEMP_RECOVERY_ABS]:
                    if user_input.get(key) is None:
                        errors[key] = "required"

            if use_external_new and sw_control_new:
                for key in [CONF_SW_MIN_ON_SEC, CONF_SW_MIN_OFF_SEC, CONF_SW_HYSTERESIS, CONF_SW_LAG_TIME, CONF_SW_I_GAIN, CONF_SW_EMA_ALPHA]:
                    if user_input.get(key) is None:
                        errors[key] = "required"

            if not errors:
                cleaned = dict(user_input)
                # Ensure correction is stored as integer
                if CONF_TEMPERATURE_CORRECTION in cleaned:
                    cleaned[CONF_TEMPERATURE_CORRECTION] = int(cleaned[CONF_TEMPERATURE_CORRECTION])

                # Strip fields irrelevant to the current toggle state
                if not use_external_new:
                    for key in [
                        CONF_TEMPERATURE_ENTITY,
                        CONF_WINDOW_OPEN_ENABLED,
                        CONF_TEMP_FALL_RATE,
                        CONF_TEMP_RECOVERY_RATE,
                        CONF_TEMP_RECOVERY_ABS,
                        CONF_SW_CONTROL_ENABLED,
                        CONF_SW_MIN_ON_SEC,
                        CONF_SW_MIN_OFF_SEC,
                        CONF_SW_HYSTERESIS,
                        CONF_SW_LAG_TIME,
                        CONF_SW_I_GAIN,
                        CONF_SW_EMA_ALPHA,
                    ]:
                        cleaned.pop(key, None)
                else:
                    if not window_open_enabled_new:
                        for key in [CONF_TEMP_FALL_RATE, CONF_TEMP_RECOVERY_RATE, CONF_TEMP_RECOVERY_ABS]:
                            cleaned.pop(key, None)
                    if not sw_control_new:
                        for key in [CONF_SW_MIN_ON_SEC, CONF_SW_MIN_OFF_SEC, CONF_SW_HYSTERESIS, CONF_SW_LAG_TIME, CONF_SW_I_GAIN, CONF_SW_EMA_ALPHA]:
                            cleaned.pop(key, None)

                # Save options directly without triggering a config entry
                # reload.  async_create_entry in an options flow causes HA
                # to reload the entry which runs the full boot sequence,
                # sends turn_off() to the device, and disrupts heating.
                # Instead update the entry in-place and abort — the
                # _async_options_updated listener in __init__.py applies
                # the correction live, and all other params are read fresh
                # on the next poll cycle (within 10 s).
                self.hass.config_entries.async_update_entry(
                    self.config_entry, options=cleaned
                )
                return self.async_abort(reason="changes_saved")

            # Re-render with user's just-submitted values on validation error
            schema = _build_options_schema(
                correction=int(user_input.get(CONF_TEMPERATURE_CORRECTION, current_correction)),
                use_external=use_external_new,
                temp_entity=user_input.get(CONF_TEMPERATURE_ENTITY, current_temp_entity),
                window_open_enabled=window_open_enabled_new,
                fall_rate=user_input.get(CONF_TEMP_FALL_RATE, current_fall_rate),
                recovery_rate=user_input.get(CONF_TEMP_RECOVERY_RATE, current_recovery_rate),
                recovery_abs=user_input.get(CONF_TEMP_RECOVERY_ABS, current_recovery_abs),
                sw_control_enabled=sw_control_new,
                sw_min_on=user_input.get(CONF_SW_MIN_ON_SEC, sw_min_on),
                sw_min_off=user_input.get(CONF_SW_MIN_OFF_SEC, sw_min_off),
                sw_hysteresis=user_input.get(CONF_SW_HYSTERESIS, sw_hysteresis),
                sw_lag_time=user_input.get(CONF_SW_LAG_TIME, sw_lag_time),
                sw_ema_alpha=user_input.get(CONF_SW_EMA_ALPHA, sw_ema_alpha),
                sw_i_gain=user_input.get(CONF_SW_I_GAIN, sw_i_gain),
            )
            return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

        # Initial render
        schema = _build_options_schema(
            correction=current_correction,
            use_external=use_external,
            temp_entity=current_temp_entity,
            window_open_enabled=window_open_enabled,
            fall_rate=current_fall_rate,
            recovery_rate=current_recovery_rate,
            recovery_abs=current_recovery_abs,
            sw_control_enabled=sw_control_enabled,
            sw_min_on=sw_min_on,
            sw_min_off=sw_min_off,
            sw_hysteresis=sw_hysteresis,
            sw_lag_time=sw_lag_time,
            sw_ema_alpha=sw_ema_alpha,
            sw_i_gain=sw_i_gain,
        )
        return self.async_show_form(step_id="init", data_schema=schema)
