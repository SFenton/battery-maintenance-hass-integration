"""Battery Maintenance integration."""

from __future__ import annotations

import logging
from datetime import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_CLASS, PERCENTAGE, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_added_domain,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .compat import compatible_donetick_entry, donetick_internal_todo_entity
from .const import (
    BOOTSTRAP_DELAY_SECONDS,
    CONF_CHARGE_ENTITIES,
    CONF_DONETICK_ENTRY_ID,
    CONF_REPLACE_ENTITIES,
    CONF_SCAN_TIME,
    CONF_UNKNOWN_ENTITIES,
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
    needs_unknown_bootstrap = CONF_UNKNOWN_ENTITIES not in entry.options
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

    async def _async_bootstrap_unknowns(_: Any) -> None:
        await coordinator.async_bootstrap_unknowns_if_needed()
        await _async_background_reconcile("bootstrap")

    async def _async_sensor_added(event: Any) -> None:
        state = event.data.get("new_state")
        if (
            state is not None
            and state.attributes.get(ATTR_DEVICE_CLASS) == "battery"
            and state.attributes.get("unit_of_measurement") == PERCENTAGE
        ):
            await _async_background_reconcile("discovery")

    async def _async_battery_state_changed(event: Any) -> None:
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if new_state is None or (
            old_state is not None and old_state.state == new_state.state
        ):
            return
        await _async_background_reconcile("state_change")

    scan_time = time.fromisoformat(
        str(entry.options.get(CONF_SCAN_TIME, DEFAULT_SCAN_TIME.isoformat()))
    )
    tracked_entities = sorted(
        {
            *entry.options.get(CONF_REPLACE_ENTITIES, []),
            *entry.options.get(CONF_CHARGE_ENTITIES, []),
        }
    )
    entry.async_on_unload(async_at_started(hass, _async_started))
    if needs_unknown_bootstrap:
        entry.async_on_unload(
            async_call_later(
                hass,
                BOOTSTRAP_DELAY_SECONDS,
                _async_bootstrap_unknowns,
            )
        )
    entry.async_on_unload(
        async_track_state_added_domain(hass, "sensor", _async_sensor_added)
    )
    if tracked_entities:
        entry.async_on_unload(
            async_track_state_change_event(
                hass,
                tracked_entities,
                _async_battery_state_changed,
            )
        )
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
