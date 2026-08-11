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
)
from custom_components.battery_maintenance.coordinator import (
    BatteryMaintenanceCoordinator,
)
from custom_components.battery_maintenance.store import BatteryMaintenanceStore


class FakeEntry:
    """Minimal config entry used by the coordinator."""

    entry_id = "test-entry"
    options = {
        CONF_DONETICK_ENTRY_ID: "donetick-entry",
        CONF_REPLACE_ENTITIES: ["sensor.front_yard_battery"],
        CONF_CHARGE_ENTITIES: [],
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

    hass.services.async_register(
        "todo",
        "get_items",
        get_items,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("donetick", "create_task_form", create_task)
    hass.services.async_register("donetick", "update_task_form", update_task)
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

    hass.services.async_register(
        "todo",
        "get_items",
        get_items,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("donetick", "create_task_form", create_task)
    hass.services.async_register("donetick", "update_task_form", update_task)

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
