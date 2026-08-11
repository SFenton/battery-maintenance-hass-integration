"""Compatibility checks for dependent integrations."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import (
    DONETICK_AUTH_TYPE_KEY,
    DONETICK_DOMAIN,
    DONETICK_JWT_AUTH_TYPE,
)


@callback
def compatible_donetick_entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    """Return the selected full-featured DoneTick entry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or entry.domain != DONETICK_DOMAIN
        or entry.data.get(DONETICK_AUTH_TYPE_KEY) != DONETICK_JWT_AUTH_TYPE
    ):
        return None
    return entry


@callback
def default_donetick_entry_id(hass: HomeAssistant) -> str | None:
    """Return the sole compatible DoneTick entry ID, when unambiguous."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DONETICK_DOMAIN)
        if entry.data.get(DONETICK_AUTH_TYPE_KEY) == DONETICK_JWT_AUTH_TYPE
    ]
    return entries[0].entry_id if len(entries) == 1 else None


@callback
def donetick_internal_todo_entity(hass: HomeAssistant, entry_id: str) -> str | None:
    """Resolve the selected DoneTick entry's internal task list."""
    registry = er.async_get(hass)
    for entity in registry.entities.values():
        if (
            entity.config_entry_id == entry_id
            and entity.platform == DONETICK_DOMAIN
            and entity.domain == "todo"
            and entity.unique_id.endswith("_all_tasks_internal")
        ):
            return entity.entity_id
    return None
