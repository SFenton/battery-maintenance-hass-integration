"""Config flow for Battery Maintenance."""

from __future__ import annotations

from typing import Any, override

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import ATTR_DEVICE_CLASS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .compat import compatible_donetick_entry, default_donetick_entry_id
from .const import (
    CONF_CHARGE_ENTITIES,
    CONF_DONETICK_ENTRY_ID,
    CONF_LOW_THRESHOLD,
    CONF_RECOVERY_THRESHOLD,
    CONF_REPLACE_ENTITIES,
    CONF_SCAN_TIME,
    DEFAULT_LOW_THRESHOLD,
    DEFAULT_RECOVERY_THRESHOLD,
    DEFAULT_SCAN_TIME,
    DOMAIN,
)
from .entity import battery_entity_metadata


def _schema(hass: HomeAssistant, defaults: dict[str, Any]) -> vol.Schema:
    """Build the shared configuration schema."""
    battery_selector = selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain="sensor",
            device_class=SensorDeviceClass.BATTERY,
            multiple=True,
        )
    )
    donetick_default = defaults.get(
        CONF_DONETICK_ENTRY_ID, default_donetick_entry_id(hass)
    )
    donetick_key = (
        vol.Required(CONF_DONETICK_ENTRY_ID, default=donetick_default)
        if donetick_default
        else vol.Required(CONF_DONETICK_ENTRY_ID)
    )
    return vol.Schema(
        {
            donetick_key: selector.ConfigEntrySelector(
                selector.ConfigEntrySelectorConfig(integration="donetick")
            ),
            vol.Required(
                CONF_REPLACE_ENTITIES,
                default=defaults.get(CONF_REPLACE_ENTITIES, []),
            ): battery_selector,
            vol.Required(
                CONF_CHARGE_ENTITIES,
                default=defaults.get(CONF_CHARGE_ENTITIES, []),
            ): battery_selector,
            vol.Required(
                CONF_LOW_THRESHOLD,
                default=defaults.get(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=99,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="%",
                )
            ),
            vol.Required(
                CONF_RECOVERY_THRESHOLD,
                default=defaults.get(
                    CONF_RECOVERY_THRESHOLD, DEFAULT_RECOVERY_THRESHOLD
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=2,
                    max=100,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="%",
                )
            ),
            vol.Required(
                CONF_SCAN_TIME,
                default=defaults.get(CONF_SCAN_TIME, DEFAULT_SCAN_TIME.isoformat()),
            ): selector.TimeSelector(),
        }
    )


def _validate(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Validate and normalize submitted mappings."""
    donetick_entry_id = str(user_input.get(CONF_DONETICK_ENTRY_ID, ""))
    if compatible_donetick_entry(hass, donetick_entry_id) is None:
        return user_input, "donetick_jwt_required"

    replace_entities = list(user_input.get(CONF_REPLACE_ENTITIES, []))
    charge_entities = list(user_input.get(CONF_CHARGE_ENTITIES, []))

    if not replace_entities and not charge_entities:
        return user_input, "at_least_one_entity"
    if set(replace_entities) & set(charge_entities):
        return user_input, "entity_in_both_sets"

    low_threshold = int(user_input[CONF_LOW_THRESHOLD])
    recovery_threshold = int(user_input[CONF_RECOVERY_THRESHOLD])
    if recovery_threshold <= low_threshold:
        return user_input, "recovery_not_above_low"

    seen_keys: set[str] = set()
    for entity_id in [*replace_entities, *charge_entities]:
        state = hass.states.get(entity_id)
        if (
            state is None
            or state.attributes.get(ATTR_DEVICE_CLASS) != SensorDeviceClass.BATTERY
            or state.attributes.get("unit_of_measurement") != "%"
        ):
            return user_input, "invalid_battery_entity"
        physical_key = battery_entity_metadata(hass, entity_id).physical_key
        if physical_key in seen_keys:
            return user_input, "duplicate_physical_device"
        seen_keys.add(physical_key)

    return (
        {
            CONF_DONETICK_ENTRY_ID: donetick_entry_id,
            CONF_REPLACE_ENTITIES: replace_entities,
            CONF_CHARGE_ENTITIES: charge_entities,
            CONF_LOW_THRESHOLD: low_threshold,
            CONF_RECOVERY_THRESHOLD: recovery_threshold,
            CONF_SCAN_TIME: str(user_input[CONF_SCAN_TIME]),
        },
        None,
    )


class BatteryMaintenanceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Battery Maintenance config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    @override
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Set up Battery Maintenance."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            normalized, error = _validate(self.hass, user_input)
            if error is None:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Battery Maintenance",
                    data={},
                    options=normalized,
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(self.hass, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> BatteryMaintenanceOptionsFlow:
        """Return the options flow."""
        return BatteryMaintenanceOptionsFlow()


class BatteryMaintenanceOptionsFlow(OptionsFlowWithReload):
    """Manage Battery Maintenance options."""

    @override
    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage mappings and thresholds."""
        errors: dict[str, str] = {}
        if user_input is not None:
            normalized, error = _validate(self.hass, user_input)
            if error is None:
                return self.async_create_entry(title="", data=normalized)
            errors["base"] = error

        defaults = (
            dict(user_input)
            if user_input is not None
            else dict(self.config_entry.options)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(self.hass, defaults),
            errors=errors,
        )
