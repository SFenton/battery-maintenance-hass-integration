"""Tests for battery entity display-name resolution."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_maintenance.entity import (
    battery_entity_display_name,
    battery_entity_id_for_review_reference,
    battery_entity_identity_digest,
    battery_entity_key,
    battery_entity_metadata,
)
from custom_components.battery_maintenance.helpers import stable_review_reference


def test_generic_battery_name_is_qualified_with_its_device() -> None:
    """A provider-only battery label gains recognizable device context."""
    assert (
        battery_entity_display_name(
            "sensor.fordpass_vehicle_battery",
            device_name="Mustang Mach-E",
            entity_name=None,
            original_name="Battery (12V)",
            state_name="Battery (12V)",
        )
        == "Mustang Mach-E Battery (12V)"
    )


def test_contextual_friendly_name_is_not_duplicated() -> None:
    """HA-composed device context remains unchanged."""
    assert (
        battery_entity_display_name(
            "sensor.front_door_contact_sensor_battery",
            device_name="Front Door Contact Sensor",
            entity_name=None,
            original_name="Battery",
            state_name="Front Door Contact Sensor Battery",
        )
        == "Front Door Contact Sensor Battery"
    )


def test_user_entity_name_is_authoritative_when_specific() -> None:
    """An explicit household-facing entity name wins over model metadata."""
    assert (
        battery_entity_display_name(
            "sensor.aqara_smart_lock_u400_battery",
            device_name="Aqara Smart Lock U400",
            entity_name="Front Door Lock Battery",
            original_name="Battery",
            state_name="Front Door Lock Battery",
        )
        == "Front Door Lock Battery"
    )


def test_generic_name_without_device_context_falls_back_to_entity_id() -> None:
    """An entity ID is more actionable than an ambiguous generic label."""
    entity_id = "sensor.unknown_battery"

    assert (
        battery_entity_display_name(
            entity_id,
            device_name=None,
            entity_name=None,
            original_name="Battery",
            state_name="Battery",
        )
        == entity_id
    )


def test_slug_derived_name_without_metadata_falls_back_to_entity_id() -> None:
    """A de-slugified backend ID is not mistaken for household-facing copy."""
    entity_id = "sensor.unknown_battery"

    assert (
        battery_entity_display_name(
            entity_id,
            device_name=None,
            entity_name=None,
            original_name=None,
            state_name="unknown battery",
        )
        == entity_id
    )


def test_hardware_name_without_metadata_falls_back_to_entity_id() -> None:
    """A raw hardware address is not published as a review title."""
    entity_id = "sensor.0x54ef441001499822_battery"

    assert (
        battery_entity_display_name(
            entity_id,
            device_name=None,
            entity_name=None,
            original_name=None,
            state_name="0x54ef441001499822 battery",
        )
        == entity_id
    )


async def test_metadata_qualifies_fordpass_name_with_device_registry(
    hass: HomeAssistant,
) -> None:
    """Registry wiring turns FordPass's generic leaf into a device title."""
    config_entry = MockConfigEntry(
        domain="fordpass",
        data={},
        entry_id="fordpass-entry",
    )
    config_entry.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("fordpass", "vehicle")},
        name="VIN: TEST",
    )
    updated_device = device_registry.async_update_device(
        device.id,
        name_by_user="Mustang Mach-E",
    )
    assert updated_device is not None
    entity_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "fordpass",
        "vehicle_battery",
        config_entry=config_entry,
        device_id=device.id,
        original_name="Battery (12V)",
        suggested_object_id="fordpass_vehicle_battery",
    )
    hass.states.async_set(
        entity_entry.entity_id,
        "66",
        {"friendly_name": "Battery (12V)"},
    )

    metadata = battery_entity_metadata(hass, entity_entry.entity_id)
    reference = stable_review_reference(
        battery_entity_key(hass, entity_entry.entity_id)
    )

    assert metadata.device_name == "Mustang Mach-E"
    assert metadata.entity_display_name == "Mustang Mach-E Battery (12V)"
    assert battery_entity_id_for_review_reference(hass, reference) == (
        entity_entry.entity_id
    )
    assert len(battery_entity_identity_digest(hass, entity_entry.entity_id)) == 64


async def test_review_reference_collision_raises(
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    """A legacy short-reference collision cannot silently choose an entity."""
    config_entry = MockConfigEntry(
        domain="mqtt",
        data={},
        entry_id="mqtt-collision-entry",
    )
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        "mqtt",
        "collision_one",
        config_entry=config_entry,
        original_name="Battery",
        suggested_object_id="collision_one_battery",
    )
    registry.async_get_or_create(
        "sensor",
        "mqtt",
        "collision_two",
        config_entry=config_entry,
        original_name="Battery",
        suggested_object_id="collision_two_battery",
    )
    monkeypatch.setattr(
        "custom_components.battery_maintenance.entity.stable_review_reference",
        lambda _entity_key: "REVIEW-COLLISION",
    )

    with pytest.raises(HomeAssistantError):
        battery_entity_id_for_review_reference(
            hass,
            "REVIEW-COLLISION",
        )
