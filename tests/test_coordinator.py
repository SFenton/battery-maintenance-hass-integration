"""Tests for DoneTick reconciliation."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_maintenance.const import (
    CONF_CHARGE_ENTITIES,
    CONF_DONETICK_ENTRY_ID,
    CONF_LOW_THRESHOLD,
    CONF_RECOVERY_THRESHOLD,
    CONF_REPLACE_ENTITIES,
    CONF_SCAN_TIME,
    CONF_UNKNOWN_ENTITIES,
    DOMAIN,
    DONETICK_COMPLETE_SERVICE,
)
from custom_components.battery_maintenance.coordinator import (
    BatteryMaintenanceCoordinator,
    ReconcileSummary,
)
from custom_components.battery_maintenance.entity import (
    BatteryEntityMetadata,
    battery_entity_identity_digest,
    battery_entity_key,
)
from custom_components.battery_maintenance.helpers import (
    review_reference_marker,
    stable_review_reference,
)
from custom_components.battery_maintenance.store import BatteryMaintenanceStore


class FakeEntry:
    """Minimal config entry used by the coordinator."""

    entry_id = "test-entry"
    options = {
        CONF_DONETICK_ENTRY_ID: "donetick-entry",
        CONF_REPLACE_ENTITIES: ["sensor.front_yard_battery"],
        CONF_CHARGE_ENTITIES: [],
        CONF_UNKNOWN_ENTITIES: [],
        CONF_LOW_THRESHOLD: 20,
        CONF_RECOVERY_THRESHOLD: 40,
        CONF_SCAN_TIME: "08:00:00",
    }


async def test_reconcile_creates_updates_and_suppresses(
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    """One low episode creates once, updates, then suppresses after completion."""
    monkeypatch.setattr(
        "custom_components.battery_maintenance.coordinator.TASK_REFRESH_DELAY",
        0,
    )
    donetick_entry = MockConfigEntry(
        domain="donetick",
        data={"auth_type": "jwt"},
        entry_id="donetick-entry",
    )
    donetick_entry.add_to_hass(hass)
    todo_entity = (
        er.async_get(hass)
        .async_get_or_create(
            "todo",
            "donetick",
            "dt_donetick-entry_all_tasks_internal",
            config_entry=donetick_entry,
            original_name="All Tasks Internal",
            suggested_object_id="all_tasks_internal",
        )
        .entity_id
    )
    hass.states.async_set(todo_entity, "0")
    hass.states.async_set(
        "sensor.front_yard_battery",
        "13",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Front Yard Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )

    tasks: dict[int, dict[str, Any]] = {}
    calls = {"create": 0, "update": 0}

    async def get_items(call: ServiceCall) -> dict[str, Any]:
        return {
            todo_entity: {
                "items": [
                    {
                        "description": task["description"],
                        "status": "needs_action",
                        "summary": task["name"],
                        "uid": f"{task_id}--{task.get('next_due_date')}",
                    }
                    for task_id, task in tasks.items()
                    if task["is_active"]
                ]
            }
        }

    async def create_task(call: ServiceCall) -> None:
        calls["create"] += 1
        tasks[100] = {
            **call.data,
            "id": 100,
            "is_active": True,
            "next_due_date": call.data["due_date"],
        }

    async def update_task(call: ServiceCall) -> None:
        calls["update"] += 1
        tasks[int(call.data["task_id"])].update(call.data)

    async def complete_task(call: ServiceCall) -> None:
        tasks[int(call.data["task_id"])]["is_active"] = False

    hass.services.async_register(
        "todo",
        "get_items",
        get_items,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("donetick", "create_task_form", create_task)
    hass.services.async_register("donetick", "update_task_form", update_task)
    hass.services.async_register(
        "donetick",
        DONETICK_COMPLETE_SERVICE,
        complete_task,
    )
    store = BatteryMaintenanceStore(hass, FakeEntry.entry_id, donetick_entry.entry_id)
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        FakeEntry(),
        store,
        donetick_entry.entry_id,
    )
    await coordinator.async_initialize()

    first = await coordinator.async_reconcile("test")
    assert first.created == 1
    assert calls["create"] == 1
    assert len(store.entries) == 1
    assert tasks[100]["name"] == "Replace Front Yard Battery \u00b7 13%"

    second = await coordinator.async_reconcile("test")
    assert second.created == 0
    assert calls["create"] == 1

    hass.states.async_set(
        "sensor.front_yard_battery",
        "12",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Front Yard Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    updated = await coordinator.async_reconcile("test")
    assert updated.updated == 1
    assert calls["update"] == 1
    assert tasks[100]["name"] == "Replace Front Yard Battery \u00b7 12%"
    assert (
        "due_date" not in tasks[100]
        or tasks[100]["due_date"] == tasks[100]["next_due_date"]
    )

    tasks[100]["is_active"] = False
    suppressed = await coordinator.async_reconcile("test")
    assert suppressed.suppressed == 1
    assert calls["create"] == 1

    hass.states.async_set(
        "sensor.front_yard_battery",
        "50",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Front Yard Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    recovered = await coordinator.async_reconcile("test")
    assert recovered.recovered == 1
    assert store.entries == {}


async def test_pending_creation_never_duplicates(
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    """A delayed DoneTick refresh leaves a recoverable pending marker."""
    monkeypatch.setattr(
        "custom_components.battery_maintenance.coordinator.TASK_REFRESH_DELAY",
        0,
    )
    monkeypatch.setattr(
        "custom_components.battery_maintenance.coordinator.TASK_REFRESH_TIMEOUT",
        0,
    )
    donetick_entry = MockConfigEntry(
        domain="donetick",
        data={"auth_type": "jwt"},
        entry_id="donetick-pending",
    )
    donetick_entry.add_to_hass(hass)
    todo_entity = (
        er.async_get(hass)
        .async_get_or_create(
            "todo",
            "donetick",
            "dt_donetick-pending_all_tasks_internal",
            config_entry=donetick_entry,
            original_name="All Tasks Internal",
            suggested_object_id="all_tasks_internal",
        )
        .entity_id
    )
    hass.states.async_set(todo_entity, "0")
    hass.states.async_set(
        "sensor.front_yard_battery",
        "13",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Front Yard Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )

    created: dict[str, Any] | None = None
    visible = False
    create_calls = 0

    async def get_items(call: ServiceCall) -> dict[str, Any]:
        items = []
        if visible and created is not None:
            items.append(
                {
                    "description": created["description"],
                    "status": "needs_action",
                    "summary": created["name"],
                    "uid": "100--2026-08-12 00:00:00+00:00",
                }
            )
        return {todo_entity: {"items": items}}

    async def create_task(call: ServiceCall) -> None:
        nonlocal created, create_calls
        create_calls += 1
        created = dict(call.data)

    async def update_task(call: ServiceCall) -> None:
        return None

    async def complete_task(call: ServiceCall) -> None:
        return None

    hass.services.async_register(
        "todo",
        "get_items",
        get_items,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("donetick", "create_task_form", create_task)
    hass.services.async_register("donetick", "update_task_form", update_task)
    hass.services.async_register(
        "donetick",
        DONETICK_COMPLETE_SERVICE,
        complete_task,
    )

    entry = FakeEntry()
    entry.entry_id = "pending-entry"
    entry.options = {
        **FakeEntry.options,
        CONF_DONETICK_ENTRY_ID: donetick_entry.entry_id,
    }
    store = BatteryMaintenanceStore(hass, entry.entry_id, donetick_entry.entry_id)
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        donetick_entry.entry_id,
    )
    await coordinator.async_initialize()

    with pytest.raises(HomeAssistantError):
        await coordinator.async_reconcile("test")
    assert create_calls == 1
    assert next(iter(store.entries.values()))["creation_pending"] is True

    visible = True
    recovered = await coordinator.async_reconcile("test")
    assert recovered.created == 0
    assert create_calls == 1
    assert next(iter(store.entries.values()))["task_id"] == 100
    assert next(iter(store.entries.values())).get("creation_pending", False) is False


async def test_new_battery_is_added_unknown_and_assigned_to_stephen(
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    """A newly available battery gets one Stephen-owned review task."""
    monkeypatch.setattr(
        "custom_components.battery_maintenance.coordinator.TASK_REFRESH_DELAY",
        0,
    )
    donetick_entry = MockConfigEntry(
        domain="donetick",
        data={"auth_type": "jwt"},
        entry_id="donetick-discovery",
    )
    donetick_entry.add_to_hass(hass)
    todo_entity = (
        er.async_get(hass)
        .async_get_or_create(
            "todo",
            "donetick",
            "dt_donetick-discovery_all_tasks_internal",
            config_entry=donetick_entry,
            original_name="All Tasks Internal",
            suggested_object_id="all_tasks_internal",
        )
        .entity_id
    )
    hass.states.async_set(todo_entity, "0")
    hass.states.async_set(
        "sensor.known_battery",
        "80",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Known Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    hass.states.async_set(
        "sensor.new_device_battery",
        "90",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "New Device Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    battery_entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        entry_id="battery-discovery",
        options={
            CONF_DONETICK_ENTRY_ID: donetick_entry.entry_id,
            CONF_REPLACE_ENTITIES: ["sensor.known_battery"],
            CONF_CHARGE_ENTITIES: [],
            CONF_UNKNOWN_ENTITIES: [],
            CONF_LOW_THRESHOLD: 20,
            CONF_RECOVERY_THRESHOLD: 40,
            CONF_SCAN_TIME: "08:00:00",
        },
    )
    battery_entry.add_to_hass(hass)

    tasks: dict[int, dict[str, Any]] = {}
    create_payloads: list[dict[str, Any]] = []

    async def get_items(call: ServiceCall) -> dict[str, Any]:
        return {
            todo_entity: {
                "items": [
                    {
                        "description": task["description"],
                        "status": "needs_action",
                        "summary": task["name"],
                        "uid": f"{task_id}--{task.get('due_date')}",
                    }
                    for task_id, task in tasks.items()
                ]
            }
        }

    async def create_task(call: ServiceCall) -> None:
        create_payloads.append(dict(call.data))
        tasks[200] = dict(call.data)

    async def update_task(call: ServiceCall) -> None:
        return None

    async def complete_task(call: ServiceCall) -> None:
        return None

    hass.services.async_register(
        "todo",
        "get_items",
        get_items,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("donetick", "create_task_form", create_task)
    hass.services.async_register("donetick", "update_task_form", update_task)
    hass.services.async_register(
        "donetick",
        DONETICK_COMPLETE_SERVICE,
        complete_task,
    )

    store = BatteryMaintenanceStore(
        hass, battery_entry.entry_id, donetick_entry.entry_id
    )
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        battery_entry,
        store,
        donetick_entry.entry_id,
    )
    await coordinator.async_initialize()

    summary = await coordinator.async_reconcile("discovery")

    assert summary.discovered == 1
    assert summary.review_created == 1
    assert battery_entry.options[CONF_UNKNOWN_ENTITIES] == ["sensor.new_device_battery"]
    assert len(store.reviews) == 1
    assert next(iter(store.reviews.values()))["display_title"] == "New Device Battery"
    assert create_payloads[0]["assignees"] == "1"
    assert create_payloads[0]["name"] == "New Device Battery"


async def test_review_uses_device_title_and_preserves_it_during_degradation(
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    """Review reconciliation renames in place without losing a useful title."""
    entity_id = "sensor.fordpass_vehicle_battery"
    hass.states.async_set(
        entity_id,
        "66",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Battery (12V)",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    reference = stable_review_reference(battery_entity_key(hass, entity_id))
    resolved_title = {"value": "Mustang Mach-E Battery (12V)"}

    def metadata(_hass: HomeAssistant, requested_entity_id: str):
        return BatteryEntityMetadata(
            entity_id=requested_entity_id,
            physical_key="device_id:mustang",
            device_name="Mustang Mach-E",
            entity_display_name=resolved_title["value"],
            area_name="Garage",
        )

    monkeypatch.setattr(
        "custom_components.battery_maintenance.coordinator.battery_entity_metadata",
        metadata,
    )

    task = {
        "description": (
            "Home Assistant discovered a new battery entity: Battery (12V)."
            f"\n\nBattery review reference: {reference}."
        ),
        "status": "needs_action",
        "summary": "Categorize battery: Battery (12V)",
        "uid": "536--2026-08-28 00:00:00+00:00",
    }
    update_payloads: list[dict[str, Any]] = []

    async def update_task(call: ServiceCall) -> None:
        payload = dict(call.data)
        update_payloads.append(payload)
        task["summary"] = payload["name"]
        task["description"] = payload["description"]

    hass.services.async_register("donetick", "update_task_form", update_task)

    entry = FakeEntry()
    entry.options = {
        **FakeEntry.options,
        CONF_REPLACE_ENTITIES: [],
        CONF_UNKNOWN_ENTITIES: [entity_id],
    }
    store = BatteryMaintenanceStore(hass, entry.entry_id, "donetick-entry")
    review = {
        "creation_pending": False,
        "entity_id": entity_id,
        "task_id": 536,
    }
    store.reviews[reference] = review
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        "donetick-entry",
    )

    first_summary = ReconcileSummary(reason="test")
    await coordinator._async_reconcile_review(
        reference,
        review,
        [task],
        {536: task},
        set(),
        first_summary,
    )

    assert first_summary.review_updated == 1
    assert review["display_title"] == "Mustang Mach-E Battery (12V)"
    assert update_payloads[0]["name"] == "Mustang Mach-E Battery (12V)"
    assert "due_date" not in update_payloads[0]

    resolved_title["value"] = entity_id
    second_summary = ReconcileSummary(reason="test")
    await coordinator._async_reconcile_review(
        reference,
        review,
        [task],
        {536: task},
        set(),
        second_summary,
    )

    assert second_summary.review_updated == 0
    assert len(update_payloads) == 1


async def test_review_without_specific_title_does_not_create_raw_id_task(
    hass: HomeAssistant,
) -> None:
    """An unresolved entity stays diagnostic-only instead of creating noise."""
    entity_id = "sensor.unknown_battery"
    hass.states.async_set(
        entity_id,
        "90",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    entry = FakeEntry()
    entry.options = {
        **FakeEntry.options,
        CONF_REPLACE_ENTITIES: [],
        CONF_UNKNOWN_ENTITIES: [entity_id],
    }
    review = {
        "creation_pending": False,
        "entity_id": entity_id,
        "task_id": 0,
    }
    store = BatteryMaintenanceStore(hass, entry.entry_id, "donetick-entry")
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        "donetick-entry",
    )
    summary = ReconcileSummary(reason="test")

    await coordinator._async_reconcile_review(
        "REVIEW-UNRESOLVED",
        review,
        [],
        {},
        set(),
        summary,
    )

    assert review["creation_pending"] is False
    assert "retired_at" not in review
    assert summary.review_created == 0
    assert summary.review_unresolved == 1


async def test_review_rebinds_renamed_entity_and_retires_mapped_task(
    hass: HomeAssistant,
) -> None:
    """A stable review identity follows an entity rename and closes stale work."""
    source_entry = MockConfigEntry(
        domain="mqtt",
        data={},
        entry_id="mqtt-battery-entry",
    )
    source_entry.add_to_hass(hass)
    entity_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "mqtt",
        "renamed_battery",
        config_entry=source_entry,
        original_name="Battery",
        suggested_object_id="master_bedroom_bed_presence_sensor_battery",
    )
    hass.states.async_set(
        entity_entry.entity_id,
        "100",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Master Bedroom Bed Presence Sensor Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    reference = stable_review_reference(
        battery_entity_key(hass, entity_entry.entity_id)
    )
    marker = review_reference_marker(reference)
    task = {
        "description": f"Old review instructions.\n\n{marker}",
        "status": "needs_action",
        "summary": "sensor.old_battery",
        "uid": "539--2026-08-31 00:00:00+00:00",
    }
    completed_task_ids: list[int] = []

    async def complete_task(call: ServiceCall) -> None:
        completed_task_ids.append(int(call.data["task_id"]))

    hass.services.async_register(
        "donetick",
        DONETICK_COMPLETE_SERVICE,
        complete_task,
    )
    entry = FakeEntry()
    entry.options = {
        **FakeEntry.options,
        CONF_REPLACE_ENTITIES: [entity_entry.entity_id],
    }
    review = {
        "creation_pending": False,
        "entity_id": "sensor.old_battery",
        "entity_identity_digest": battery_entity_identity_digest(
            hass,
            entity_entry.entity_id,
        ),
        "task_id": 539,
    }
    store = BatteryMaintenanceStore(hass, entry.entry_id, "donetick-entry")
    store.reviews[reference] = review
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        "donetick-entry",
    )
    summary = ReconcileSummary(reason="test")

    await coordinator._async_reconcile_review(
        reference,
        review,
        [],
        {539: task},
        set(),
        summary,
    )

    assert review["entity_id"] == entity_entry.entity_id
    assert len(str(review["entity_identity_digest"])) == 64
    assert review["retired_reason"] == "categorized"
    assert review["retired_at"]
    assert completed_task_ids == [539]
    assert summary.review_retired == 1

    await coordinator._async_reconcile_review(
        reference,
        review,
        [task],
        {539: task},
        set(),
        summary,
    )

    assert completed_task_ids == [539]
    assert summary.review_retired == 1


async def test_registry_only_review_waits_for_state_before_retiring(
    hass: HomeAssistant,
) -> None:
    """A sensor registry entry is not retired while its state is still loading."""
    source_entry = MockConfigEntry(
        domain="mqtt",
        data={},
        entry_id="mqtt-loading-entry",
    )
    source_entry.add_to_hass(hass)
    entity_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "mqtt",
        "loading_battery",
        config_entry=source_entry,
        original_name="Battery",
        suggested_object_id="loading_battery",
    )
    reference = stable_review_reference(
        battery_entity_key(hass, entity_entry.entity_id)
    )
    review = {
        "creation_pending": False,
        "entity_id": entity_entry.entity_id,
        "task_id": 0,
    }
    entry = FakeEntry()
    entry.options = {
        **FakeEntry.options,
        CONF_REPLACE_ENTITIES: [],
    }
    store = BatteryMaintenanceStore(hass, entry.entry_id, "donetick-entry")
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        "donetick-entry",
    )
    summary = ReconcileSummary(reason="test")

    await coordinator._async_reconcile_review(
        reference,
        review,
        [],
        {},
        set(),
        summary,
    )

    assert "retired_at" not in review
    assert summary.review_retired == 0
    assert summary.review_unresolved == 1


async def test_review_retires_ineligible_entity_task(
    hass: HomeAssistant,
) -> None:
    """A review that can no longer be configured does not remain actionable."""
    entity_id = "sensor.vehicle_12v_battery"
    reference = "REVIEW-33BB580E"
    marker = review_reference_marker(reference)
    hass.states.async_set(
        entity_id,
        "86",
        {
            "friendly_name": "Battery (12V)",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    task = {
        "description": f"Old review instructions.\n\n{marker}",
        "status": "needs_action",
        "summary": "Battery (12V)",
        "uid": "536--2026-08-28 00:00:00+00:00",
    }
    completed_task_ids: list[int] = []

    async def complete_task(call: ServiceCall) -> None:
        completed_task_ids.append(int(call.data["task_id"]))

    hass.services.async_register(
        "donetick",
        DONETICK_COMPLETE_SERVICE,
        complete_task,
    )
    entry = FakeEntry()
    entry.options = {
        **FakeEntry.options,
        CONF_REPLACE_ENTITIES: [],
    }
    review = {
        "creation_pending": False,
        "display_title": "Mustang Mach-E Battery (12V)",
        "entity_id": entity_id,
        "task_id": 536,
    }
    store = BatteryMaintenanceStore(hass, entry.entry_id, "donetick-entry")
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        "donetick-entry",
    )
    summary = ReconcileSummary(reason="test")

    await coordinator._async_reconcile_review(
        reference,
        review,
        [task],
        {536: task},
        set(),
        summary,
    )

    assert review["retired_reason"] == "ineligible"
    assert completed_task_ids == [536]
    assert summary.review_retired == 1


async def test_review_retirement_never_completes_task_without_marker(
    hass: HomeAssistant,
) -> None:
    """A stale task pointer cannot complete a task the integration does not own."""
    entity_id = "sensor.ineligible_battery"
    reference = "REVIEW-NOT-OWNED"
    task = {
        "description": "User-owned task without a battery review marker.",
        "status": "needs_action",
        "summary": "Unrelated task",
        "uid": "700--2026-08-31 00:00:00+00:00",
    }
    completed_task_ids: list[int] = []

    async def complete_task(call: ServiceCall) -> None:
        completed_task_ids.append(int(call.data["task_id"]))

    hass.services.async_register(
        "donetick",
        DONETICK_COMPLETE_SERVICE,
        complete_task,
    )
    entry = FakeEntry()
    entry.options = {
        **FakeEntry.options,
        CONF_REPLACE_ENTITIES: [],
    }
    review = {
        "creation_pending": False,
        "display_title": "Ineligible Battery",
        "entity_id": entity_id,
        "task_id": 700,
    }
    store = BatteryMaintenanceStore(hass, entry.entry_id, "donetick-entry")
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        "donetick-entry",
    )
    summary = ReconcileSummary(reason="test")

    await coordinator._async_reconcile_review(
        reference,
        review,
        [task],
        {700: task},
        set(),
        summary,
    )

    assert completed_task_ids == []
    assert review["retired_reason"] == "ineligible"
    assert summary.review_retired == 1


async def test_duplicate_review_markers_block_retirement_without_reconcile_error(
    hass: HomeAssistant,
    caplog,
) -> None:
    """Ambiguous task ownership is logged without blocking mapped batteries."""
    entity_id = "sensor.mapped_battery"
    reference = "REVIEW-DUPLICATE"
    marker = review_reference_marker(reference)
    hass.states.async_set(
        entity_id,
        "90",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Mapped Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    tasks = [
        {
            "description": marker,
            "status": "needs_action",
            "summary": "Mapped Battery",
            "uid": f"{task_id}--2026-08-31 00:00:00+00:00",
        }
        for task_id in (600, 601)
    ]
    completed_task_ids: list[int] = []

    async def complete_task(call: ServiceCall) -> None:
        completed_task_ids.append(int(call.data["task_id"]))

    hass.services.async_register(
        "donetick",
        DONETICK_COMPLETE_SERVICE,
        complete_task,
    )
    entry = FakeEntry()
    entry.options = {
        **FakeEntry.options,
        CONF_REPLACE_ENTITIES: [entity_id],
    }
    review = {
        "creation_pending": False,
        "display_title": "Mapped Battery",
        "entity_id": entity_id,
        "task_id": 600,
    }
    store = BatteryMaintenanceStore(hass, entry.entry_id, "donetick-entry")
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        "donetick-entry",
    )
    summary = ReconcileSummary(reason="test")

    await coordinator._async_reconcile_review(
        reference,
        review,
        tasks,
        {600: tasks[0], 601: tasks[1]},
        set(),
        summary,
    )

    assert "retired_at" not in review
    assert completed_task_ids == []
    assert summary.review_retired == 0
    assert "cannot retire safely" in caplog.text


async def test_recovery_completes_active_task(
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    """A recovered battery immediately completes its active managed task."""
    monkeypatch.setattr(
        "custom_components.battery_maintenance.coordinator.TASK_REFRESH_DELAY",
        0,
    )
    donetick_entry = MockConfigEntry(
        domain="donetick",
        data={"auth_type": "jwt"},
        entry_id="donetick-recovery",
    )
    donetick_entry.add_to_hass(hass)
    todo_entity = (
        er.async_get(hass)
        .async_get_or_create(
            "todo",
            "donetick",
            "dt_donetick-recovery_all_tasks_internal",
            config_entry=donetick_entry,
            original_name="All Tasks Internal",
            suggested_object_id="all_tasks_internal",
        )
        .entity_id
    )
    hass.states.async_set(todo_entity, "0")
    hass.states.async_set(
        "sensor.front_yard_battery",
        "20",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Front Yard Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )

    tasks: dict[int, dict[str, Any]] = {}
    complete_calls = 0

    async def get_items(call: ServiceCall) -> dict[str, Any]:
        return {
            todo_entity: {
                "items": [
                    {
                        "description": task["description"],
                        "status": "needs_action",
                        "summary": task["name"],
                        "uid": f"{task_id}--{task.get('next_due_date')}",
                    }
                    for task_id, task in tasks.items()
                    if task["is_active"]
                ]
            }
        }

    async def create_task(call: ServiceCall) -> None:
        tasks[300] = {
            **call.data,
            "id": 300,
            "is_active": True,
            "next_due_date": call.data["due_date"],
        }

    async def update_task(call: ServiceCall) -> None:
        tasks[int(call.data["task_id"])].update(call.data)

    async def complete_task(call: ServiceCall) -> None:
        nonlocal complete_calls
        complete_calls += 1
        tasks[int(call.data["task_id"])]["is_active"] = False

    hass.services.async_register(
        "todo",
        "get_items",
        get_items,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("donetick", "create_task_form", create_task)
    hass.services.async_register("donetick", "update_task_form", update_task)
    hass.services.async_register(
        "donetick",
        DONETICK_COMPLETE_SERVICE,
        complete_task,
    )

    entry = FakeEntry()
    entry.entry_id = "recovery-entry"
    entry.options = {
        **FakeEntry.options,
        CONF_DONETICK_ENTRY_ID: donetick_entry.entry_id,
    }
    store = BatteryMaintenanceStore(hass, entry.entry_id, donetick_entry.entry_id)
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        donetick_entry.entry_id,
    )
    await coordinator.async_initialize()

    created = await coordinator.async_reconcile("low")
    assert created.created == 1
    assert tasks[300]["name"] == "Replace Front Yard Battery \u00b7 20%"

    hass.states.async_set(
        "sensor.front_yard_battery",
        "25",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Front Yard Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    updated = await coordinator.async_reconcile("state_change")
    assert updated.updated == 1
    assert tasks[300]["name"] == "Replace Front Yard Battery \u00b7 25%"

    hass.states.async_set(
        "sensor.front_yard_battery",
        "40",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Front Yard Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    recovered = await coordinator.async_reconcile("state_change")

    assert recovered.completed == 1
    assert recovered.recovered == 1
    assert complete_calls == 1
    assert tasks[300]["is_active"] is False
    assert store.entries == {}
