"""Tests for removing reviews and entities deleted from Home Assistant."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import (
    CoreState,
    HomeAssistant,
    ServiceCall,
    SupportsResponse,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_maintenance.const import (
    CONF_CHARGE_ENTITIES,
    CONF_DONETICK_ENTRY_ID,
    CONF_REPLACE_ENTITIES,
    CONF_UNKNOWN_ENTITIES,
)
from custom_components.battery_maintenance.coordinator import (
    BatteryMaintenanceCoordinator,
)
from custom_components.battery_maintenance.entity import (
    battery_entity_identity_digest,
    battery_entity_key,
)
from custom_components.battery_maintenance.helpers import (
    review_reference_marker,
    stable_review_reference,
)
from custom_components.battery_maintenance.store import BatteryMaintenanceStore

GONE = "sensor.stephen_location_battery_level"


class Harness:
    """A coordinator wired to fake DoneTick services."""

    def __init__(
        self,
        hass: HomeAssistant,
        options: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> None:
        self.hass = hass
        hass.set_state(CoreState.running)
        self.tasks = tasks
        self.completed: list[int] = []
        self.entry = MockConfigEntry(
            domain="battery_maintenance",
            options={CONF_DONETICK_ENTRY_ID: "donetick-entry", **options},
        )
        self.entry.add_to_hass(hass)
        donetick = MockConfigEntry(
            domain="donetick", data={"auth_type": "jwt"}, entry_id="donetick-entry"
        )
        donetick.add_to_hass(hass)
        self.todo = (
            er.async_get(hass)
            .async_get_or_create(
                "todo",
                "donetick",
                "dt_donetick-entry_all_tasks_internal",
                config_entry=donetick,
                suggested_object_id="all_tasks_internal",
            )
            .entity_id
        )
        hass.states.async_set(self.todo, "0")

        async def get_items(call: ServiceCall) -> dict[str, Any]:
            return {self.todo: {"items": list(self.tasks)}}

        async def complete(call: ServiceCall) -> None:
            task_id = int(call.data["task_id"])
            self.completed.append(task_id)
            self.tasks[:] = [
                t for t in self.tasks if not t["uid"].startswith(f"{task_id}--")
            ]

        async def noop(call: ServiceCall) -> None:
            return None

        hass.services.async_register(
            "todo", "get_items", get_items, supports_response=SupportsResponse.ONLY
        )
        hass.services.async_register("donetick", "complete_task", complete)
        hass.services.async_register("donetick", "create_task_form", noop)
        hass.services.async_register("donetick", "update_task_form", noop)
        self.store = BatteryMaintenanceStore(
            hass, self.entry.entry_id, "donetick-entry"
        )
        self.coordinator = BatteryMaintenanceCoordinator(
            hass, self.entry, self.store, "donetick-entry"
        )

    def age_missing(self) -> None:
        """Pretend every absent entity was first seen two hours ago."""
        past = (dt_util.now() - timedelta(hours=2)).isoformat()
        for entity_id in self.store.missing:
            self.store.missing[entity_id] = past


def review_task(task_id: int, reference: str, summary: str) -> dict[str, Any]:
    return {
        "description": f"Categorize.\n\n{review_reference_marker(reference)}",
        "status": "needs_action",
        "summary": summary,
        "uid": f"{task_id}--2026-11-02 00:00:00+00:00",
    }


def orphan_harness(hass: HomeAssistant) -> tuple[Harness, str]:
    reference = "REVIEW-AAAA1111"
    harness = Harness(
        hass,
        {
            CONF_REPLACE_ENTITIES: [],
            CONF_CHARGE_ENTITIES: [],
            CONF_UNKNOWN_ENTITIES: [GONE, "sensor.other_battery"],
        },
        [review_task(639, reference, "Stephen Location Battery Level")],
    )
    harness.store.reviews[reference] = {
        "creation_pending": False,
        "entity_id": GONE,
        "display_title": "Stephen Location Battery Level",
        "task_id": 639,
    }
    hass.states.async_set(
        "sensor.other_battery",
        "80",
        {"device_class": SensorDeviceClass.BATTERY, "unit_of_measurement": PERCENTAGE},
    )
    return harness, reference


async def test_orphan_removed_with_task_closed(hass: HomeAssistant) -> None:
    """A deleted entity is orphaned on a later reconcile and its task closed."""
    harness, reference = orphan_harness(hass)

    await harness.coordinator.async_reconcile("first")
    assert harness.completed == []
    assert GONE in harness.store.missing
    assert reference in harness.store.reviews

    harness.age_missing()
    summary = await harness.coordinator.async_reconcile("second")

    assert harness.completed == [639]
    assert reference not in harness.store.reviews
    assert GONE not in harness.store.missing
    assert harness.entry.options[CONF_UNKNOWN_ENTITIES] == ["sensor.other_battery"]
    assert summary.orphans_removed == 1


async def test_orphan_mapped_entity_closes_ledger_task(hass: HomeAssistant) -> None:
    """A deleted Replace entity loses its option entry and open episode task."""
    reference = "BATT-1234"
    harness = Harness(
        hass,
        {CONF_REPLACE_ENTITIES: [GONE], CONF_CHARGE_ENTITIES: []},
        [
            {
                "description": f"Low.\n\nMaintenance reference: {reference}.",
                "status": "needs_action",
                "summary": "Replace",
                "uid": "640--2026-11-02 00:00:00+00:00",
            }
        ],
    )
    harness.store.entries[reference] = {"entity_id": GONE, "task_id": 640}
    await harness.coordinator.async_reconcile("first")
    harness.age_missing()
    await harness.coordinator.async_reconcile("second")

    assert harness.completed == [640]
    assert harness.store.entries == {}
    assert harness.entry.options[CONF_REPLACE_ENTITIES] == []


async def test_unavailable_registered_entity_is_kept(hass: HomeAssistant) -> None:
    """Unavailable, or registered without state, is never an orphan."""
    source = MockConfigEntry(domain="mqtt", data={}, entry_id="mqtt-entry")
    source.add_to_hass(hass)
    registered = er.async_get(hass).async_get_or_create(
        "sensor",
        "mqtt",
        "kept",
        config_entry=source,
        original_name="Hallway Battery",
        suggested_object_id="hallway_battery",
    )
    unavailable = "sensor.unavailable_battery"
    hass.states.async_set(unavailable, "unavailable")
    reference = "REVIEW-BBBB2222"
    harness = Harness(
        hass,
        {CONF_UNKNOWN_ENTITIES: [registered.entity_id, unavailable]},
        [review_task(701, reference, "Hallway Battery")],
    )
    harness.store.reviews[reference] = {
        "entity_id": registered.entity_id,
        "display_title": "Hallway Battery",
        "task_id": 701,
    }

    for reason in ("first", "second", "third"):
        harness.age_missing()
        await harness.coordinator.async_reconcile(reason)

    assert harness.completed == []
    assert reference in harness.store.reviews
    assert harness.store.missing == {}
    assert harness.entry.options[CONF_UNKNOWN_ENTITIES] == [
        registered.entity_id,
        unavailable,
    ]


async def test_rebind_by_identity_digest_is_not_an_orphan(
    hass: HomeAssistant,
) -> None:
    """A renamed entity found through its digest keeps its review and task."""
    source = MockConfigEntry(domain="mqtt", data={}, entry_id="mqtt-entry")
    source.add_to_hass(hass)
    renamed = er.async_get(hass).async_get_or_create(
        "sensor",
        "mqtt",
        "renamed",
        config_entry=source,
        original_name="Bed Battery",
        suggested_object_id="bed_battery",
    )
    hass.states.async_set(
        renamed.entity_id,
        "90",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Bed Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    reference = stable_review_reference(battery_entity_key(hass, renamed.entity_id))
    old_id = "sensor.old_bed_battery"
    harness = Harness(
        hass,
        {CONF_UNKNOWN_ENTITIES: [old_id]},
        [review_task(702, reference, "Bed Battery")],
    )
    harness.store.reviews[reference] = {
        "entity_id": old_id,
        "entity_identity_digest": battery_entity_identity_digest(
            hass, renamed.entity_id
        ),
        "task_id": 702,
    }

    for reason in ("first", "second"):
        harness.age_missing()
        await harness.coordinator.async_reconcile(reason)

    assert harness.completed == []
    assert harness.store.missing == {}
    assert harness.store.reviews[reference]["entity_id"] == renamed.entity_id


async def test_grace_period_is_honoured(hass: HomeAssistant) -> None:
    """Absence must span two reconciles and the grace period."""
    harness, reference = orphan_harness(hass)

    await harness.coordinator.async_reconcile("first")
    await harness.coordinator.async_reconcile("immediately after")
    assert harness.completed == []
    assert reference in harness.store.reviews

    # Reappearing resets the clock.
    hass.states.async_set(GONE, "50", {"unit_of_measurement": PERCENTAGE})
    harness.age_missing()
    await harness.coordinator.async_reconcile("back")
    assert harness.store.missing == {}
    assert harness.completed == []

    hass.states.async_remove(GONE)
    await harness.coordinator.async_reconcile("gone again")
    assert harness.completed == []
    harness.age_missing()
    await harness.coordinator.async_reconcile("aged")
    assert harness.completed == [639]


async def test_grace_survives_restart(hass: HomeAssistant) -> None:
    """The first-missing time is persisted in the store."""
    harness, _ = orphan_harness(hass)
    await harness.coordinator.async_reconcile("first")
    reloaded = BatteryMaintenanceStore(hass, harness.entry.entry_id, "donetick-entry")
    await reloaded.async_load()
    assert GONE in reloaded.missing


def _mqtt_sensor(
    hass: HomeAssistant, unique_id: str, object_id: str
) -> er.RegistryEntry:
    source = MockConfigEntry(domain="mqtt", data={}, entry_id=f"mqtt-{unique_id}")
    source.add_to_hass(hass)
    entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "mqtt",
        unique_id,
        config_entry=source,
        original_name="Door Battery",
        suggested_object_id=object_id,
    )
    hass.states.async_set(
        entry.entity_id,
        "50",
        {
            "device_class": SensorDeviceClass.BATTERY,
            "friendly_name": "Door Battery",
            "unit_of_measurement": PERCENTAGE,
        },
    )
    return entry


async def test_renamed_mapped_sensor_is_rebound_by_identity(
    hass: HomeAssistant,
) -> None:
    """A Replace sensor without a review follows a registry rename."""
    entry = _mqtt_sensor(hass, "door", "door_battery")
    harness = Harness(hass, {CONF_REPLACE_ENTITIES: [entry.entity_id]}, [])
    await harness.coordinator.async_reconcile("seen")
    assert entry.entity_id in harness.store.identities

    new_id = "sensor.front_door_battery"
    er.async_get(hass).async_update_entity(entry.entity_id, new_entity_id=new_id)
    hass.states.async_remove(entry.entity_id)
    hass.states.async_set(
        new_id,
        "50",
        {"device_class": SensorDeviceClass.BATTERY, "unit_of_measurement": PERCENTAGE},
    )
    for reason in ("renamed", "later"):
        harness.age_missing()
        await harness.coordinator.async_reconcile(reason)

    assert harness.completed == []
    assert harness.store.missing == {}
    assert harness.entry.options[CONF_REPLACE_ENTITIES] == [new_id]


async def test_renamed_sensor_is_rebound_by_ledger_physical_key(
    hass: HomeAssistant,
) -> None:
    """The ledger's physical key finds the sensor when no identity was stored."""
    entry = _mqtt_sensor(hass, "door2", "new_door_battery")
    state = hass.states.get(entry.entity_id)
    hass.states.async_set(entry.entity_id, "unknown", state.attributes)
    reference = "BATT-9999"
    old_id = "sensor.old_door_battery"
    harness = Harness(
        hass,
        {CONF_CHARGE_ENTITIES: [old_id]},
        [
            {
                "description": f"Low.\n\nMaintenance reference: {reference}.",
                "status": "needs_action",
                "summary": "Charge",
                "uid": "800--2026-11-02 00:00:00+00:00",
            }
        ],
    )
    harness.store.entries[reference] = {
        "entity_id": old_id,
        "physical_key": "entity:mqtt:door2",
        "task_id": 800,
    }
    for reason in ("first", "second"):
        harness.age_missing()
        await harness.coordinator.async_reconcile(reason)

    assert harness.completed == []
    assert harness.entry.options[CONF_CHARGE_ENTITIES] == [entry.entity_id]
    assert harness.store.entries[reference]["entity_id"] == entry.entity_id


async def test_options_saved_during_donetick_call_are_preserved(
    hass: HomeAssistant,
) -> None:
    """Options changed while a task is being completed are not overwritten."""
    harness, _ = orphan_harness(hass)
    await harness.coordinator.async_reconcile("first")
    harness.age_missing()

    async def complete_and_edit(call: ServiceCall) -> None:
        harness.completed.append(int(call.data["task_id"]))
        harness.tasks.clear()
        hass.config_entries.async_update_entry(
            harness.entry,
            options={
                **harness.entry.options,
                CONF_REPLACE_ENTITIES: ["sensor.added_by_user"],
            },
        )

    hass.services.async_remove("donetick", "complete_task")
    hass.services.async_register("donetick", "complete_task", complete_and_edit)
    await harness.coordinator.async_reconcile("second")

    assert harness.completed == [639]
    assert harness.entry.options[CONF_REPLACE_ENTITIES] == ["sensor.added_by_user"]
    assert GONE not in harness.entry.options[CONF_UNKNOWN_ENTITIES]


async def test_markerless_ledger_task_warns_and_is_not_completed(
    hass: HomeAssistant, caplog
) -> None:
    """A foreign task is left open, with a warning naming it."""
    harness = Harness(
        hass,
        {CONF_REPLACE_ENTITIES: [GONE]},
        [
            {
                "description": "Someone else's task",
                "status": "needs_action",
                "summary": "Other",
                "uid": "641--2026-11-02 00:00:00+00:00",
            }
        ],
    )
    harness.store.entries["BATT-1"] = {"entity_id": GONE, "task_id": 641}
    await harness.coordinator.async_reconcile("first")
    harness.age_missing()
    await harness.coordinator.async_reconcile("second")

    assert harness.completed == []
    assert harness.store.entries == {}
    assert "DoneTick task 641" in caplog.text


async def test_no_orphan_detection_before_startup(hass: HomeAssistant) -> None:
    """Nothing is recorded or removed until Home Assistant is running."""
    harness, reference = orphan_harness(hass)
    hass.set_state(CoreState.starting)
    await harness.coordinator.async_reconcile("boot")
    harness.age_missing()
    await harness.coordinator.async_reconcile("boot again")

    assert harness.completed == []
    assert harness.store.missing == {}
    assert reference in harness.store.reviews
