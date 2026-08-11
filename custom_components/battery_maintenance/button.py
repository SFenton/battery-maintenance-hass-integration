"""Button platform for Battery Maintenance."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory

from .coordinator import BatteryMaintenanceCoordinator


async def async_setup_entry(
    hass: Any,
    entry: Any,
    async_add_entities: Any,
) -> None:
    """Set up the manual sync button."""
    async_add_entities([BatteryMaintenanceSyncButton(entry.runtime_data)])


class BatteryMaintenanceSyncButton(ButtonEntity):
    """Run Battery Maintenance reconciliation now."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_icon = "mdi:battery-sync"
    _attr_translation_key = "sync"

    def __init__(self, coordinator: BatteryMaintenanceCoordinator) -> None:
        """Initialize the button."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_sync"

    async def async_press(self) -> None:
        """Run a manual reconciliation."""
        await self._coordinator.async_reconcile("button")
