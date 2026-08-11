"""Tests for the Battery Maintenance config flow."""

from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_maintenance.const import (
    CONF_CHARGE_ENTITIES,
    CONF_DONETICK_ENTRY_ID,
    CONF_LOW_THRESHOLD,
    CONF_RECOVERY_THRESHOLD,
    CONF_REPLACE_ENTITIES,
    CONF_SCAN_TIME,
    DOMAIN,
)


def _battery_attributes() -> dict[str, str]:
    return {
        "device_class": SensorDeviceClass.BATTERY,
        "unit_of_measurement": PERCENTAGE,
    }


async def test_config_flow_creates_single_entry(hass: HomeAssistant) -> None:
    """A valid Replace/Charge mapping creates one options-backed entry."""
    donetick_entry = MockConfigEntry(
        domain="donetick",
        data={"auth_type": "jwt"},
    )
    donetick_entry.add_to_hass(hass)
    hass.states.async_set("sensor.replace_battery", "13", _battery_attributes())
    hass.states.async_set("sensor.charge_battery", "19", _battery_attributes())

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={
            CONF_DONETICK_ENTRY_ID: donetick_entry.entry_id,
            CONF_REPLACE_ENTITIES: ["sensor.replace_battery"],
            CONF_CHARGE_ENTITIES: ["sensor.charge_battery"],
            CONF_LOW_THRESHOLD: 20,
            CONF_RECOVERY_THRESHOLD: 40,
            CONF_SCAN_TIME: "08:00:00",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    assert result["options"][CONF_REPLACE_ENTITIES] == ["sensor.replace_battery"]
    assert result["options"][CONF_CHARGE_ENTITIES] == ["sensor.charge_battery"]


async def test_config_flow_rejects_overlap(hass: HomeAssistant) -> None:
    """One entity cannot be mapped to both actions."""
    donetick_entry = MockConfigEntry(
        domain="donetick",
        data={"auth_type": "jwt"},
    )
    donetick_entry.add_to_hass(hass)
    hass.states.async_set("sensor.shared_battery", "13", _battery_attributes())

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={
            CONF_DONETICK_ENTRY_ID: donetick_entry.entry_id,
            CONF_REPLACE_ENTITIES: ["sensor.shared_battery"],
            CONF_CHARGE_ENTITIES: ["sensor.shared_battery"],
            CONF_LOW_THRESHOLD: 20,
            CONF_RECOVERY_THRESHOLD: 40,
            CONF_SCAN_TIME: "08:00:00",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "entity_in_both_sets"}


async def test_config_flow_requires_full_donetick_auth(
    hass: HomeAssistant,
) -> None:
    """DoneTick API-key mode cannot support managed task updates."""
    donetick_entry = MockConfigEntry(
        domain="donetick",
        data={"auth_type": "api_key"},
    )
    donetick_entry.add_to_hass(hass)
    hass.states.async_set("sensor.replace_battery", "13", _battery_attributes())

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={
            CONF_DONETICK_ENTRY_ID: donetick_entry.entry_id,
            CONF_REPLACE_ENTITIES: ["sensor.replace_battery"],
            CONF_CHARGE_ENTITIES: [],
            CONF_LOW_THRESHOLD: 20,
            CONF_RECOVERY_THRESHOLD: 40,
            CONF_SCAN_TIME: "08:00:00",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "donetick_jwt_required"}
