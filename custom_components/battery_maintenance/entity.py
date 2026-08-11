"""Entity and device metadata helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_EUI64_PATTERN = re.compile(r"(?:serial_|zigbee2mqtt_0x)([0-9a-fA-F]{16})")


@dataclass(frozen=True, slots=True)
class BatteryEntityMetadata:
    """Resolved metadata for one selected battery entity."""

    entity_id: str
    physical_key: str
    device_name: str
    area_name: str | None


@callback
def battery_entity_key(hass: HomeAssistant, entity_id: str) -> str:
    """Return stable entity identity for discovery review tasks."""
    entity_entry = er.async_get(hass).async_get(entity_id)
    if entity_entry and entity_entry.unique_id:
        return f"{entity_entry.platform}:{entity_entry.unique_id}"
    return f"entity_id:{entity_id}"


@callback
def battery_entity_metadata(
    hass: HomeAssistant, entity_id: str
) -> BatteryEntityMetadata:
    """Resolve stable physical identity and household-facing metadata."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    entity_entry = entity_registry.async_get(entity_id)
    device_entry = (
        device_registry.async_get(entity_entry.device_id)
        if entity_entry and entity_entry.device_id
        else None
    )

    physical_key: str | None = None
    if device_entry:
        for _, identifier in sorted(device_entry.identifiers):
            match = _EUI64_PATTERN.search(str(identifier))
            if match:
                physical_key = f"eui64:{match.group(1).lower()}"
                break

        if physical_key is None:
            for connection_type, value in sorted(device_entry.connections):
                if connection_type == dr.CONNECTION_NETWORK_MAC:
                    normalized = str(value).lower().replace(":", "").replace("-", "")
                    physical_key = f"mac:{normalized}"
                    break

        if physical_key is None and device_entry.identifiers:
            canonical = "|".join(
                f"{domain}:{identifier}"
                for domain, identifier in sorted(device_entry.identifiers)
            )
            digest = sha256(canonical.encode()).hexdigest()
            physical_key = f"device_identifier:{digest}"

        if physical_key is None:
            physical_key = f"device_id:{device_entry.id}"

    if physical_key is None and entity_entry and entity_entry.unique_id:
        physical_key = f"entity:{entity_entry.platform}:{entity_entry.unique_id}"
    if physical_key is None:
        physical_key = f"entity_id:{entity_id}"

    state = hass.states.get(entity_id)
    device_name = (
        (device_entry.name_by_user if device_entry else None)
        or (device_entry.name if device_entry else None)
        or (state.name if state else entity_id)
    )

    area_id = (entity_entry.area_id if entity_entry else None) or (
        device_entry.area_id if device_entry else None
    )
    area_entry = area_registry.async_get_area(area_id) if area_id else None

    return BatteryEntityMetadata(
        entity_id=entity_id,
        physical_key=physical_key,
        device_name=device_name,
        area_name=area_entry.name if area_entry else None,
    )
