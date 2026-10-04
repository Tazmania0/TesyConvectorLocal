"""Fresh temperature receipts, independent of whether the value changed.

The optional passive Bluetooth adapter uses HA's already decoded incoming data.
It never treats general connectivity or accumulated processor data as a report.
"""

import logging
import math
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)


class SensorFreshness:
    """Track confirmed receipts; keep generic entity timestamps as fallback."""

    def __init__(self, hass):
        self.hass = hass
        self.entity_id = None
        self._entity = None
        self._remove = None
        self._received_at = None
        self._value = None
        self.source = "entity_report"

    def close(self):
        if self._remove is not None:
            self._remove()
        self._remove = None
        self._entity = None
        self._received_at = None
        self._value = None

    def bind(self, entity_id):
        """Retry on polls to handle late setup, entity replacement and reloads."""
        hass_data = getattr(self.hass, "data", {})
        component = hass_data.get("entity_components", {}).get("sensor") or hass_data.get("sensor")
        get_entity = getattr(component, "get_entity", None)
        entity = get_entity(entity_id) if get_entity and entity_id else None
        if entity_id == self.entity_id and entity is self._entity:
            return
        self.close()
        self.entity_id = entity_id
        self._entity = entity
        processor = getattr(entity, "processor", None)
        key = getattr(entity, "entity_key", None)
        coordinator = getattr(processor, "coordinator", None)
        register = getattr(coordinator, "async_register_processor", None)
        if key is None or not callable(register):
            return
        # Currently verified for BTHome's SensorUpdate / SensorValue contract.
        # Other integrations retain generic timestamps or an explicit receipt
        # timestamp until their parser contract has been verified.
        if not type(entity).__module__.startswith("homeassistant.components.bthome."):
            return
        previous_sample = None

        def convert(update):
            nonlocal previous_sample
            incoming = {}
            for device_key, sample in getattr(update, "entity_values", {}).items():
                if device_key.key != key.key or device_key.device_id != key.device_id:
                    continue
                # The parser creates a new SensorValue even for an unchanged
                # decoded reading, but reuses it when carrying cached fields.
                if previous_sample is not None and sample is not previous_sample:
                    incoming[key] = sample.native_value
                previous_sample = sample
                break
            return type(processor.data)(entity_data=incoming)

        def received(data):
            # Only incoming fields count; processor.entity_data is a cache.
            if data is None:
                self._received_at = None
                self._value = None
                return
            value = getattr(data, "entity_data", {}).get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return
            if not math.isfinite(value) or not -40 <= value <= 80:
                return
            self._value = float(value)
            self._received_at = self.hass.loop.time()

        remove_listener = None
        try:
            observer = type(processor)(convert)
            # This observer has no entities or restore data. Never seed receipt
            # times from HA's persisted processor cache.
            observer.restore_key = None
            remove_listener = observer.async_add_listener(received)
            remove_processor = register(observer)
            def remove():
                remove_listener()
                remove_processor()

            self._remove = remove
        except (AttributeError, TypeError, RuntimeError, ImportError):
            if remove_listener is not None:
                remove_listener()
            # This is an optional adapter to another integration's API.
            _LOGGER.debug("Temperature receipt listener unsupported for %s", entity_id)

    def age(self, state, value, timestamp_entity=None):
        """Return the newest credible temperature receipt age."""
        self.source = "entity_report"
        reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        now = datetime.now(timezone.utc)
        age = max(0.0, (now - reported).total_seconds()) if reported is not None else None
        if self._received_at is not None and self._value == value:
            receipt_age = max(0.0, self.hass.loop.time() - self._received_at)
            if age is None or receipt_age < age:
                age = receipt_age
                self.source = "decoded_temperature_report"
        # Explicitly configured timestamp must represent temperature receipts,
        # not a generic device last_seen, RSSI, battery or connectivity update.
        timestamp = self.hass.states.get(timestamp_entity) if timestamp_entity else None
        if timestamp is not None:
            try:
                date = datetime.fromisoformat(timestamp.state.replace("Z", "+00:00"))
                if date.tzinfo is not None and date <= now:
                    timestamp_age = (now - date).total_seconds()
                    if age is None or timestamp_age < age:
                        age = timestamp_age
                        self.source = "temperature_report_timestamp"
            except (ValueError, TypeError, AttributeError):
                pass
        return age
