"""Entity and device metadata helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .helpers import stable_review_reference

_EUI64_PATTERN = re.compile(r"(?:serial_|zigbee2mqtt_0x)([0-9a-fA-F]{16})")
_GENERIC_BATTERY_NAME_PATTERN = re.compile(
    r"^(?:(?:unknown|unnamed)\s+)?"
    r"(?:battery(?:\s+(?:level|sensor))?|state of charge)"
    r"(?:\s*\([^)]*\))?$",
    re.IGNORECASE,
)
_TECHNICAL_BATTERY_NAME_PATTERN = re.compile(
    r"^(?:0x)?[0-9a-f]{12,}\s+battery(?:\s+(?:level|sensor))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BatteryEntityMetadata:
    """Resolved metadata for one selected battery entity."""

    entity_id: str
    physical_key: str
    device_name: str
    entity_display_name: str
    area_name: str | None


def _compact_name(value: str | None) -> str | None:
    """Normalize optional registry names without changing their wording."""
    compacted = " ".join(value.split()) if value else ""
    return compacted or None


def _is_generic_battery_name(value: str) -> bool:
    """Return whether a name lacks any recognizable device context."""
    return (
        _GENERIC_BATTERY_NAME_PATTERN.fullmatch(value) is not None
        or _TECHNICAL_BATTERY_NAME_PATTERN.fullmatch(value) is not None
    )


def _contains_name(value: str, name: str) -> bool:
    """Return whether one normalized display name already contains another."""
    return name.casefold() in value.casefold()


def battery_entity_display_name(
    entity_id: str,
    *,
    device_name: str | None,
    entity_name: str | None,
    original_name: str | None,
    state_name: str | None,
) -> str:
    """Resolve a specific review-task name for one battery entity."""
    normalized_entity_id = entity_id.strip()
    device = _compact_name(device_name)
    custom = _compact_name(entity_name)
    original = _compact_name(original_name)
    current = _compact_name(state_name)

    if custom and not _is_generic_battery_name(custom):
        return custom

    usable_device = device if device and not _is_generic_battery_name(device) else None
    if usable_device and current and _contains_name(current, usable_device):
        return current

    leaf = custom or original or current
    if usable_device:
        if not leaf or leaf == normalized_entity_id:
            if usable_device.casefold().endswith((" battery", " batteries")):
                return usable_device
            return f"{usable_device} Battery"
        if _contains_name(leaf, usable_device):
            return leaf
        if _contains_name(usable_device, leaf):
            return usable_device
        return f"{usable_device} {leaf}"

    for candidate in (custom, current, original):
        if (
            candidate
            and candidate != normalized_entity_id
            and not _is_generic_battery_name(candidate)
        ):
            return candidate
    return normalized_entity_id


def _battery_entity_key_from_entry(entity_entry: er.RegistryEntry) -> str:
    """Return the stable review identity represented by a registry entry."""
    if entity_entry.unique_id:
        return f"{entity_entry.platform}:{entity_entry.unique_id}"
    return f"entity_id:{entity_entry.entity_id}"


@callback
def battery_entity_key(hass: HomeAssistant, entity_id: str) -> str:
    """Return stable entity identity for discovery review tasks."""
    entity_entry = er.async_get(hass).async_get(entity_id)
    if entity_entry:
        return _battery_entity_key_from_entry(entity_entry)
    return f"entity_id:{entity_id}"


@callback
def battery_entity_identity_digest(hass: HomeAssistant, entity_id: str) -> str:
    """Return the full digest used to rebind renamed review entities."""
    return sha256(battery_entity_key(hass, entity_id).encode()).hexdigest()


@callback
def battery_entity_id_for_review_reference(
    hass: HomeAssistant,
    reference: str,
    identity_digest: str | None = None,
) -> str | None:
    """Resolve a renamed sensor entity from its persisted review identity."""
    matches: list[str] = []
    for entity_entry in er.async_get(hass).entities.values():
        if not entity_entry.entity_id.startswith("sensor."):
            continue
        entity_key = _battery_entity_key_from_entry(entity_entry)
        matches_digest = (
            sha256(entity_key.encode()).hexdigest() == identity_digest
            if identity_digest
            else stable_review_reference(entity_key) == reference
        )
        if matches_digest:
            matches.append(entity_entry.entity_id)

    if len(matches) > 1:
        raise HomeAssistantError(
            f"Multiple sensor entities match battery review {reference}"
        )
    return matches[0] if matches else None


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
    registry_device_name = (device_entry.name_by_user if device_entry else None) or (
        device_entry.name if device_entry else None
    )
    device_name = registry_device_name or (state.name if state else entity_id)
    entity_display_name = battery_entity_display_name(
        entity_id,
        device_name=registry_device_name,
        entity_name=entity_entry.name if entity_entry else None,
        original_name=entity_entry.original_name if entity_entry else None,
        state_name=state.name if state else None,
    )

    area_id = (entity_entry.area_id if entity_entry else None) or (
        device_entry.area_id if device_entry else None
    )
    area_entry = area_registry.async_get_area(area_id) if area_id else None

    return BatteryEntityMetadata(
        entity_id=entity_id,
        physical_key=physical_key,
        device_name=device_name,
        entity_display_name=entity_display_name,
        area_name=area_entry.name if area_entry else None,
    )
