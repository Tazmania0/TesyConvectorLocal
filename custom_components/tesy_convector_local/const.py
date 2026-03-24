"""Constants for Tesy Convector Local integration."""

DOMAIN = "tesy_convector_local"

# FIX: binary_sensor was missing — must match _PLATFORMS in __init__.py.
# PLATFORMS is kept here for reference but __init__.py uses its own _PLATFORMS list
# as the authoritative source to avoid drift.
PLATFORMS = ["climate", "number", "binary_sensor"]

CONF_TEMP_FALL_RATE = "temp_fall_rate"
CONF_TEMP_RECOVERY_RATE = "temp_recovery_rate"
CONF_TEMP_RECOVERY_ABS = "temp_recovery_abs"
CONF_TEMPERATURE_ENTITY = "temperature_entity"
CONF_TEMPERATURE_CORRECTION = "temperature_correction"
CONF_WINDOW_OPEN_ENABLED = "window_open_enabled"
CONF_USE_EXTERNAL_TEMP = "use_external_temp"
CONF_MODEL = "model"
CONF_IP_ADDRESS = "ip_address"
CONF_LAST_HVAC_MODE = "last_hvac_mode"  # persisted across reboots
CONF_LAST_SETPOINT = "last_setpoint"      # heat setpoint persisted across reboots

# v0.3 — software on/off controller (half-degree setpoint + relay wear protection)
CONF_SW_CONTROL_ENABLED = "sw_control_enabled"
CONF_SW_MIN_ON_SEC = "sw_min_on_sec"
CONF_SW_MIN_OFF_SEC = "sw_min_off_sec"
CONF_SW_HYSTERESIS = "sw_hysteresis"
CONF_SW_LAG_TIME = "sw_lag_time"       # thermal lag minutes
CONF_SW_EMA_ALPHA = "sw_ema_alpha"     # EMA smoothing factor 0.1–1.0
CONF_SW_I_GAIN    = "sw_i_gain"         # integral correction gain 0-1

DEFAULT_SW_MIN_ON_SEC = 60          # seconds minimum ON phase
DEFAULT_SW_MIN_OFF_SEC = 60         # seconds minimum OFF phase
DEFAULT_SW_HYSTERESIS = 0.3         # °C dead-band around setpoint
DEFAULT_SW_LAG_TIME = 6.0           # minutes thermal lag (sensor + distance)
DEFAULT_SW_EMA_ALPHA = 0.2          # EMA factor: 0.2 = heavy smooth, 1.0 = raw
DEFAULT_SW_I_GAIN = 0.5              # integral gain: fraction of avg error applied
SW_I_WINDOW_SEC   = 1800             # seconds of history for integral average (30 min)

DEFAULT_TEMP_FALL_RATE = 0.5       # °C/min
DEFAULT_TEMP_RECOVERY_RATE = 0.3   # °C/min
DEFAULT_TEMP_RECOVERY_ABS = 1.0    # °C

INTERNAL_TEMP_OPTION = "__internal__"
INTERNAL_TEMP_LABEL = "Internal (device sensor)"

WINDOW_RECOVERY_HYSTERESIS_SEC = 30
WINDOW_OPEN_TIMEOUT_SEC = 3600
