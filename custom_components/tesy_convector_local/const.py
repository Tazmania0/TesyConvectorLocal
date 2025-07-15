"""Constants for Tesy Convector Local integration.

Defines all config keys, option names, and default values used across the integration.
"""

DOMAIN = "tesy_convector_local"
PLATFORMS = ["climate", "number"]  # Add 'number' here instead

CONF_TEMP_FALL_RATE = "temp_fall_rate"
CONF_TEMP_RECOVERY_RATE = "temp_recovery_rate"
CONF_TEMP_RECOVERY_ABS = "temp_recovery_abs"
CONF_TEMPERATURE_ENTITY = "temperature_entity"
CONF_TEMPERATURE_CORRECTION = "temperature_correction"
CONF_WINDOW_OPEN_ENABLED = "window_open_enabled"
CONF_USE_INTERNAL_TEMP = "use_internal_temp"
CONF_MODEL = "model"
CONF_IP_ADDRESS = "ip_address"

DEFAULT_TEMP_FALL_RATE = 0.5  # °C/min
DEFAULT_TEMP_RECOVERY_RATE = 0.3  # °C/min
DEFAULT_TEMP_RECOVERY_ABS = 1.0  # °C

INTERNAL_TEMP_OPTION = "__internal__"
INTERNAL_TEMP_LABEL = "Internal (device sensor)"

WINDOW_RECOVERY_HYSTERESIS_SEC = 30

