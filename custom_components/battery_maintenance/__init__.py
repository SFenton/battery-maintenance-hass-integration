"""Battery Maintenance integration."""

from __future__ import annotations

import logging
from datetime import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .compat import compatible_donetick_entry, donetick_internal_todo_entity
from .const import (
    CONF_DONETICK_ENTRY_ID,
    CONF_SCAN_TIME,
    DEFAULT_SCAN_TIME,
    DOMAIN,
    SERVICE_SYNC,
)
from .coordinator import BatteryMaintenanceCoordinator
from .store import BatteryMaintenanceStore

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration-level services."""

    async def _async_sync(call: ServiceCall) -> None:
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not entries:
            raise ServiceValidationError(
                "Battery Maintenance is not configured or loaded"
            )
        await entries[0].runtime_data.async_reconcile("service")

    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC,
        _async_sync,
        schema=vol.Schema({}),
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
) -> bool:
    """Set up one Battery Maintenance config entry."""
    donetick_entry_id = str(entry.options.get(CONF_DONETICK_ENTRY_ID, ""))
    if compatible_donetick_entry(hass, donetick_entry_id) is None:
        raise ConfigEntryNotReady(
            "DoneTick must use Username & Password (Full Features) authentication"
        )
    if donetick_internal_todo_entity(hass, donetick_entry_id) is None:
        raise ConfigEntryNotReady(
            "The selected DoneTick entry has no internal task list"
        )

    store = BatteryMaintenanceStore(hass, entry.entry_id, donetick_entry_id)
    coordinator = BatteryMaintenanceCoordinator(
        hass,
        entry,
        store,
        donetick_entry_id,
    )
    await coordinator.async_initialize()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)

    async def _async_background_reconcile(reason: str) -> None:
        try:
            await coordinator.async_reconcile(reason)
        except HomeAssistantError:
            _LOGGER.exception("Battery Maintenance %s reconciliation failed", reason)

    async def _async_started(_: HomeAssistant) -> None:
        await _async_background_reconcile("startup")

    async def _async_daily(_: Any) -> None:
        await _async_background_reconcile("daily")

    scan_time = time.fromisoformat(
        str(entry.options.get(CONF_SCAN_TIME, DEFAULT_SCAN_TIME.isoformat()))
    )
    entry.async_on_unload(async_at_started(hass, _async_started))
    entry.async_on_unload(
        async_track_time_change(
            hass,
            _async_daily,
            hour=scan_time.hour,
            minute=scan_time.minute,
            second=scan_time.second,
        )
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: Any,
) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant,
    entry: Any,
) -> None:
    """Delete persisted state when the config entry is removed."""
    await BatteryMaintenanceStore(
        hass,
        entry.entry_id,
        str(entry.options.get(CONF_DONETICK_ENTRY_ID, "")),
    ).async_remove()
