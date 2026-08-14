"""Tests for Battery Maintenance event subscriptions."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_maintenance import async_setup_entry
from custom_components.battery_maintenance.const import (
    CONF_CHARGE_ENTITIES,
    CONF_DONETICK_ENTRY_ID,
    CONF_LOW_THRESHOLD,
    CONF_RECOVERY_THRESHOLD,
    CONF_REPLACE_ENTITIES,
    CONF_SCAN_TIME,
    CONF_UNKNOWN_ENTITIES,
    DOMAIN,
)
from custom_components.battery_maintenance.coordinator import (
    BatteryMaintenanceCoordinator,
    ReconcileSummary,
)


async def test_mapped_battery_state_change_reconciles_immediately(
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    """A mapped battery state change triggers reconciliation without polling."""
    donetick_entry = MockConfigEntry(
        domain="donetick",
        data={"auth_type": "jwt"},
        entry_id="donetick-state-change",
    )
    donetick_entry.add_to_hass(hass)
    (
        er.async_get(hass).async_get_or_create(
            "todo",
            "donetick",
            "dt_donetick-state-change_all_tasks_internal",
            config_entry=donetick_entry,
            original_name="All Tasks Internal",
            suggested_object_id="all_tasks_internal",
        )
    )
    hass.states.async_set(
        "sensor.lock_battery",
        "20",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Lock Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        entry_id="battery-state-change",
        options={
            CONF_DONETICK_ENTRY_ID: donetick_entry.entry_id,
            CONF_REPLACE_ENTITIES: [],
            CONF_CHARGE_ENTITIES: ["sensor.lock_battery"],
            CONF_UNKNOWN_ENTITIES: [],
            CONF_LOW_THRESHOLD: 20,
            CONF_RECOVERY_THRESHOLD: 40,
            CONF_SCAN_TIME: "08:00:00",
        },
    )
    entry.add_to_hass(hass)

    reasons: list[str] = []

    async def reconcile(
        self: BatteryMaintenanceCoordinator,
        reason: str,
    ) -> ReconcileSummary:
        reasons.append(reason)
        return ReconcileSummary(reason=reason)

    monkeypatch.setattr(BatteryMaintenanceCoordinator, "async_reconcile", reconcile)
    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(),
    )

    try:
        assert await async_setup_entry(hass, entry)
        await hass.async_block_till_done()
        reasons.clear()

        hass.states.async_set(
            "sensor.lock_battery",
            "98",
            {
                "device_class": SensorDeviceClass.BATTERY,
                "friendly_name": "Lock Battery",
                "unit_of_measurement": PERCENTAGE,
            },
        )
        await hass.async_block_till_done()

        assert reasons == ["state_change"]
    finally:
        await entry._async_process_on_unload(hass)
