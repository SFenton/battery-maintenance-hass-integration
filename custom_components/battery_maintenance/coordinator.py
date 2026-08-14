"""DoneTick reconciliation for Battery Maintenance."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .compat import donetick_internal_todo_entity
from .const import (
    ACTION_CHARGE,
    ACTION_REPLACE,
    CONF_CHARGE_ENTITIES,
    CONF_LOW_THRESHOLD,
    CONF_RECOVERY_THRESHOLD,
    CONF_REPLACE_ENTITIES,
    CONF_UNKNOWN_ENTITIES,
    DEFAULT_LOW_THRESHOLD,
    DEFAULT_RECOVERY_THRESHOLD,
    DONETICK_COMPLETE_SERVICE,
    DONETICK_CREATE_SERVICE,
    DONETICK_DOMAIN,
    DONETICK_UPDATE_SERVICE,
    PENDING_RETRY_SECONDS,
    REVIEW_ASSIGNEE_ID,
    TASK_REFRESH_DELAY,
    TASK_REFRESH_TIMEOUT,
)
from .entity import (
    BatteryEntityMetadata,
    battery_entity_key,
    battery_entity_metadata,
)
from .helpers import (
    due_at_five,
    parse_task_id,
    reference_marker,
    review_reference_marker,
    review_task_description,
    review_task_name,
    stable_reference,
    stable_review_reference,
    task_description,
    task_name,
)
from .store import BatteryMaintenanceStore

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ReconcileSummary:
    """Summary of one reconciliation run."""

    reason: str
    checked: int = 0
    low: int = 0
    created: int = 0
    adopted: int = 0
    updated: int = 0
    completed: int = 0
    suppressed: int = 0
    recovered: int = 0
    unavailable: int = 0
    discovered: int = 0
    review_created: int = 0
    review_updated: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class BatteryMapping:
    """One configured battery entity and maintenance action."""

    action: str
    metadata: BatteryEntityMetadata


class BatteryMaintenanceCoordinator:
    """Coordinate mapped batteries with DoneTick."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,
        store: BatteryMaintenanceStore,
        donetick_entry_id: str,
    ) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.entry = entry
        self.store = store
        self.donetick_entry_id = donetick_entry_id
        self._lock = asyncio.Lock()
        self.last_summary: ReconcileSummary | None = None

    async def async_initialize(self) -> None:
        """Load persisted state."""
        await self.store.async_load()

    async def async_bootstrap_unknowns_if_needed(self) -> None:
        """Mark existing unconfigured batteries Unknown without review tasks."""
        if CONF_UNKNOWN_ENTITIES in self.entry.options:
            return
        options = dict(self.entry.options)
        options[CONF_UNKNOWN_ENTITIES] = self._unmapped_battery_entities()
        self.hass.config_entries.async_update_entry(
            self.entry,
            options=options,
        )

    async def async_shutdown(self) -> None:
        """Flush persisted state before unload."""
        async with self._lock:
            await self.store.async_save()

    def diagnostics(self) -> dict[str, Any]:
        """Return coordinator diagnostics."""
        return {
            "last_summary": (
                asdict(self.last_summary) if self.last_summary is not None else None
            ),
            "store": self.store.diagnostics(),
        }

    async def async_reconcile(self, reason: str) -> ReconcileSummary:
        """Reconcile all configured mappings."""
        async with self._lock:
            self._require_services()
            summary = ReconcileSummary(reason=reason)
            new_unknowns = await self._async_discover_unknown_entities()
            summary.discovered = len(new_unknowns)
            active_tasks = await self._async_active_tasks()
            active_by_id = {
                task_id: item
                for item in active_tasks
                if (task_id := parse_task_id(str(item.get("uid", "")))) is not None
            }
            claimed_task_ids: set[int] = set()

            errors: list[str] = []
            for reference, review in list(self.store.reviews.items()):
                try:
                    await self._async_reconcile_review(
                        reference,
                        review,
                        active_tasks,
                        active_by_id,
                        claimed_task_ids,
                        summary,
                    )
                except HomeAssistantError as err:
                    summary.errors += 1
                    errors.append(f"{review.get('entity_id', reference)}: {err}")
                    _LOGGER.error(
                        "Battery review reconciliation failed for %s: %s",
                        review.get("entity_id", reference),
                        err,
                    )

            for mapping in self._mappings():
                summary.checked += 1
                try:
                    await self._async_reconcile_mapping(
                        mapping,
                        active_tasks,
                        active_by_id,
                        claimed_task_ids,
                        summary,
                    )
                except HomeAssistantError as err:
                    summary.errors += 1
                    errors.append(f"{mapping.metadata.entity_id}: {err}")
                    _LOGGER.error(
                        "Battery reconciliation failed for %s: %s",
                        mapping.metadata.entity_id,
                        err,
                    )

            await self.store.async_save()
            self.last_summary = summary

            if errors:
                raise HomeAssistantError(
                    f"{len(errors)} battery mapping(s) failed; see the Home Assistant "
                    "log for details"
                )
            return summary

    def _battery_entities(self) -> list[str]:
        """Return enabled percentage battery sensor entities."""
        return sorted(
            state.entity_id
            for state in self.hass.states.async_all("sensor")
            if state.attributes.get(ATTR_DEVICE_CLASS) == "battery"
            and state.attributes.get("unit_of_measurement") == PERCENTAGE
        )

    def _unmapped_battery_entities(self) -> list[str]:
        """Return current batteries outside Replace and Charge."""
        mapped = {
            *self.entry.options.get(CONF_REPLACE_ENTITIES, []),
            *self.entry.options.get(CONF_CHARGE_ENTITIES, []),
        }
        return [
            entity_id
            for entity_id in self._battery_entities()
            if entity_id not in mapped
        ]

    async def _async_discover_unknown_entities(self) -> list[str]:
        """Add newly available battery sensors to Unknown and review state."""
        if CONF_UNKNOWN_ENTITIES not in self.entry.options:
            return []
        known = {
            *self.entry.options.get(CONF_REPLACE_ENTITIES, []),
            *self.entry.options.get(CONF_CHARGE_ENTITIES, []),
            *self.entry.options.get(CONF_UNKNOWN_ENTITIES, []),
        }
        discovered = [
            entity_id
            for entity_id in self._battery_entities()
            if entity_id not in known
        ]
        if not discovered:
            return []

        options = dict(self.entry.options)
        options[CONF_UNKNOWN_ENTITIES] = sorted(
            {
                *options.get(CONF_UNKNOWN_ENTITIES, []),
                *discovered,
            }
        )
        self.hass.config_entries.async_update_entry(
            self.entry,
            options=options,
        )

        for entity_id in discovered:
            reference = stable_review_reference(
                battery_entity_key(self.hass, entity_id)
            )
            self.store.reviews.setdefault(
                reference,
                {
                    "creation_pending": False,
                    "entity_id": entity_id,
                    "first_detected": dt_util.now().isoformat(),
                    "task_id": 0,
                },
            )
        await self.store.async_save()
        return discovered

    async def _async_reconcile_review(
        self,
        reference: str,
        review: dict[str, Any],
        active_tasks: list[dict[str, Any]],
        active_by_id: dict[int, dict[str, Any]],
        claimed_task_ids: set[int],
        summary: ReconcileSummary,
    ) -> None:
        """Create or maintain one battery categorization task."""
        entity_id = str(review.get("entity_id", ""))
        state = self.hass.states.get(entity_id)
        entity_name = state.name if state else entity_id
        current_state = state.state if state else "unavailable"
        if (
            state
            and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
            and state.attributes.get("unit_of_measurement") == PERCENTAGE
        ):
            current_state = f"{current_state}%"

        expected_name = review_task_name(entity_name)
        expected_description = review_task_description(
            entity_name,
            current_state,
            reference,
        )
        marker = review_reference_marker(reference)

        if review.get("creation_pending"):
            pending_task = self._find_marker_candidate(
                active_tasks,
                marker,
                claimed_task_ids,
            )
            if pending_task is None:
                pending_task = await self._async_wait_for_marker(marker)
            if pending_task is None:
                if not self._pending_retry_due(review):
                    raise HomeAssistantError(
                        "DoneTick review task creation is still pending for "
                        f"{entity_name}"
                    )
                review["creation_pending"] = False
                review.pop("pending_since", None)
                await self.store.async_save()
            else:
                pending_task_id = parse_task_id(str(pending_task.get("uid", "")))
                if pending_task_id is None:
                    raise HomeAssistantError(
                        "DoneTick returned an invalid review task identifier for "
                        f"{entity_name}"
                    )
                review["task_id"] = pending_task_id
                review["creation_pending"] = False
                review.pop("pending_since", None)
                active_by_id[pending_task_id] = pending_task
                await self.store.async_save()

        task_id = int(review.get("task_id", 0))
        if task_id > 0:
            if task_id in claimed_task_ids:
                raise HomeAssistantError(
                    f"DoneTick task {task_id} is linked to more than one battery"
                )
            task = active_by_id.get(task_id)
            if task is None:
                return
            if marker not in str(task.get("description", "")):
                review["task_id"] = 0
                task_id = 0
            else:
                claimed_task_ids.add(task_id)
                if (
                    task.get("summary") != expected_name
                    or task.get("description", "") != expected_description
                ):
                    await self._async_update_task(
                        task_id,
                        expected_name,
                        expected_description,
                    )
                    summary.review_updated += 1
                return

        adopted = self._find_marker_candidate(
            active_tasks,
            marker,
            claimed_task_ids,
        )
        if adopted is not None:
            adopted_id = parse_task_id(str(adopted.get("uid", "")))
            if adopted_id is None:
                raise HomeAssistantError(
                    "DoneTick returned an invalid review task identifier for "
                    f"{entity_name}"
                )
            review["task_id"] = adopted_id
            claimed_task_ids.add(adopted_id)
            await self.store.async_save()
            return

        review["creation_pending"] = True
        review["pending_since"] = dt_util.now().isoformat()
        await self.store.async_save()
        try:
            await self._async_create_review_task(
                expected_name,
                expected_description,
            )
        except HomeAssistantError:
            review["creation_pending"] = False
            review.pop("pending_since", None)
            await self.store.async_save()
            raise

        created = await self._async_wait_for_marker(marker)
        if created is None:
            raise HomeAssistantError(
                f"Could not identify the DoneTick review task created for {entity_name}"
            )
        created_id = parse_task_id(str(created.get("uid", "")))
        if created_id is None:
            raise HomeAssistantError(
                f"DoneTick returned an invalid review task identifier for {entity_name}"
            )
        review["task_id"] = created_id
        review["creation_pending"] = False
        review.pop("pending_since", None)
        claimed_task_ids.add(created_id)
        await self.store.async_save()
        summary.review_created += 1

    @staticmethod
    def _pending_retry_due(record: dict[str, Any]) -> bool:
        """Return whether a missing pending task can be safely retried."""
        pending_since = dt_util.parse_datetime(str(record.get("pending_since", "")))
        if pending_since is None:
            return True
        return (dt_util.now() - pending_since).total_seconds() >= PENDING_RETRY_SECONDS

    def _require_services(self) -> None:
        """Ensure the required DoneTick services are available."""
        required = (
            (DONETICK_DOMAIN, DONETICK_COMPLETE_SERVICE),
            (DONETICK_DOMAIN, DONETICK_CREATE_SERVICE),
            (DONETICK_DOMAIN, DONETICK_UPDATE_SERVICE),
            ("todo", "get_items"),
        )
        missing = [
            f"{domain}.{service}"
            for domain, service in required
            if not self.hass.services.has_service(domain, service)
        ]
        if missing:
            raise HomeAssistantError(
                "Required services are unavailable: " + ", ".join(missing)
            )

    def _mappings(self) -> list[BatteryMapping]:
        """Build mappings from the current options."""
        options = self.entry.options
        mappings: list[BatteryMapping] = []
        for action, option_key in (
            (ACTION_REPLACE, CONF_REPLACE_ENTITIES),
            (ACTION_CHARGE, CONF_CHARGE_ENTITIES),
        ):
            for entity_id in options.get(option_key, []):
                mappings.append(
                    BatteryMapping(
                        action=action,
                        metadata=battery_entity_metadata(self.hass, entity_id),
                    )
                )
        return mappings

    async def _async_reconcile_mapping(
        self,
        mapping: BatteryMapping,
        active_tasks: list[dict[str, Any]],
        active_by_id: dict[int, dict[str, Any]],
        claimed_task_ids: set[int],
        summary: ReconcileSummary,
    ) -> None:
        """Reconcile one mapped battery."""
        percentage = self._percentage(mapping.metadata.entity_id)
        if percentage is None:
            summary.unavailable += 1
            return

        low_threshold = float(
            self.entry.options.get(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD)
        )
        recovery_threshold = float(
            self.entry.options.get(CONF_RECOVERY_THRESHOLD, DEFAULT_RECOVERY_THRESHOLD)
        )
        if percentage <= low_threshold:
            summary.low += 1

        reference = stable_reference(mapping.metadata.physical_key)
        migrated_reference: str | None = None
        expected_name = task_name(
            mapping.action,
            mapping.metadata.device_name,
            percentage,
        )
        expected_description = task_description(
            mapping.action,
            mapping.metadata.device_name,
            percentage,
            mapping.metadata.area_name,
            reference,
        )
        ledger = self.store.entries.get(reference)
        if ledger is None:
            matching_ledger = [
                (stored_reference, stored)
                for stored_reference, stored in self.store.entries.items()
                if stored.get("entity_id") == mapping.metadata.entity_id
            ]
            if len(matching_ledger) > 1:
                raise HomeAssistantError(
                    f"Multiple ledger entries exist for {mapping.metadata.entity_id}"
                )
            if matching_ledger:
                migrated_reference, ledger = matching_ledger[0]
                if migrated_reference != reference:
                    self.store.entries.pop(migrated_reference, None)
                    self.store.entries[reference] = ledger

        if ledger and ledger.get("creation_pending"):
            pending_task = self._find_reference_candidate(active_tasks, reference)
            if pending_task is None:
                pending_task = await self._async_wait_for_reference(reference)
            if pending_task is None:
                if percentage >= recovery_threshold:
                    self.store.entries.pop(reference, None)
                    summary.recovered += 1
                    return
                pending_since = dt_util.parse_datetime(
                    str(ledger.get("pending_since", ""))
                )
                pending_age = (
                    (dt_util.now() - pending_since).total_seconds()
                    if pending_since is not None
                    else PENDING_RETRY_SECONDS
                )
                if pending_age < PENDING_RETRY_SECONDS:
                    raise HomeAssistantError(
                        f"DoneTick task creation is still pending for {expected_name}"
                    )
                self.store.entries.pop(reference, None)
                ledger = None
                await self.store.async_save()
            else:
                pending_task_id = parse_task_id(str(pending_task.get("uid", "")))
                if pending_task_id is None:
                    raise HomeAssistantError(
                        "DoneTick returned an invalid task identifier for "
                        f"{expected_name}"
                    )
                ledger["task_id"] = pending_task_id
                ledger["creation_pending"] = False
                ledger.pop("pending_since", None)
                active_by_id[pending_task_id] = pending_task
                await self.store.async_save()

        task: dict[str, Any] | None = None
        task_id = int(ledger.get("task_id", 0)) if ledger else 0
        if task_id > 0:
            if task_id in claimed_task_ids:
                raise HomeAssistantError(
                    f"DoneTick task {task_id} is linked to more than one battery"
                )
            claimed_task_ids.add(task_id)
            task = active_by_id.get(task_id)
            if task is None:
                if percentage >= recovery_threshold:
                    self.store.entries.pop(reference, None)
                    summary.recovered += 1
                else:
                    summary.suppressed += 1
                return
            accepted_markers = {reference_marker(reference)}
            if migrated_reference:
                accepted_markers.add(reference_marker(migrated_reference))
            if not any(
                marker in str(task.get("description", ""))
                for marker in accepted_markers
            ):
                unowned_task_id = task_id
                self.store.entries.pop(reference, None)
                ledger = None
                task = None
                task_id = 0
                claimed_task_ids.discard(unowned_task_id)

        if task is not None:
            if percentage >= recovery_threshold:
                await self._async_complete_task(task_id)
                self.store.entries.pop(reference, None)
                summary.completed += 1
                summary.recovered += 1
                return
            if (
                task.get("summary") != expected_name
                or task.get("description", "") != expected_description
            ):
                await self._async_update_task(
                    task_id,
                    expected_name,
                    expected_description,
                )
                summary.updated += 1
            self._record_ledger(
                reference,
                mapping,
                task_id,
                percentage,
                ledger,
            )
            return

        adopted = self._find_adoption_candidate(
            active_tasks,
            reference,
            expected_name,
            mapping.metadata.device_name,
            claimed_task_ids,
        )
        if adopted is not None:
            adopted_id = parse_task_id(str(adopted.get("uid", "")))
            if adopted_id is None:
                raise HomeAssistantError(
                    f"DoneTick returned an invalid task identifier for {expected_name}"
                )
            claimed_task_ids.add(adopted_id)
            if percentage >= recovery_threshold:
                await self._async_complete_task(adopted_id)
                self.store.entries.pop(reference, None)
                summary.completed += 1
                summary.recovered += 1
                return
            if (
                adopted.get("summary") != expected_name
                or adopted.get("description", "") != expected_description
            ):
                await self._async_update_task(
                    adopted_id,
                    expected_name,
                    expected_description,
                )
                summary.updated += 1
            self._record_ledger(
                reference,
                mapping,
                adopted_id,
                percentage,
                None,
            )
            await self.store.async_save()
            summary.adopted += 1
            return

        if percentage > low_threshold:
            return

        self._record_pending_ledger(
            reference,
            mapping,
            percentage,
        )
        await self.store.async_save()
        try:
            await self._async_create_task(expected_name, expected_description)
        except HomeAssistantError:
            self.store.entries.pop(reference, None)
            await self.store.async_save()
            raise

        created = await self._async_wait_for_reference(reference)
        if created is None:
            raise HomeAssistantError(
                f"Could not identify the DoneTick task created for {expected_name}"
            )
        created_id = parse_task_id(str(created.get("uid", "")))
        if created_id is None:
            raise HomeAssistantError(
                f"DoneTick returned an invalid task identifier for {expected_name}"
            )
        claimed_task_ids.add(created_id)

        self._record_ledger(
            reference,
            mapping,
            created_id,
            percentage,
            None,
        )
        await self.store.async_save()
        summary.created += 1

    async def _async_wait_for_reference(self, reference: str) -> dict[str, Any] | None:
        """Poll through DoneTick's refresh debounce for a created task."""
        return await self._async_wait_for_marker(reference_marker(reference))

    async def _async_wait_for_marker(self, marker: str) -> dict[str, Any] | None:
        """Poll through DoneTick's refresh debounce for a task marker."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TASK_REFRESH_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(TASK_REFRESH_DELAY)
            matching = self._find_marker_candidate(
                await self._async_active_tasks(),
                marker,
                set(),
            )
            if matching is not None:
                return matching
        return None

    def _percentage(self, entity_id: str) -> float | None:
        """Return a valid percentage or None."""
        state = self.hass.states.get(entity_id)
        if (
            state is None
            or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)
            or state.attributes.get(ATTR_DEVICE_CLASS) != "battery"
            or state.attributes.get("unit_of_measurement") != PERCENTAGE
        ):
            return None
        try:
            percentage = float(state.state)
        except ValueError:
            return None
        if not 0 <= percentage <= 100:
            return None
        return percentage

    def _record_ledger(
        self,
        reference: str,
        mapping: BatteryMapping,
        task_id: int,
        percentage: float,
        previous: dict[str, Any] | None,
    ) -> None:
        """Record or update one open episode."""
        self.store.entries[reference] = {
            "action": mapping.action,
            "entity_id": mapping.metadata.entity_id,
            "first_detected": (
                previous.get("first_detected")
                if previous
                else dt_util.now().isoformat()
            ),
            "last_percent": percentage,
            "name": mapping.metadata.device_name,
            "physical_key": mapping.metadata.physical_key,
            "task_id": task_id,
        }

    def _record_pending_ledger(
        self,
        reference: str,
        mapping: BatteryMapping,
        percentage: float,
    ) -> None:
        """Persist creation intent before calling DoneTick."""
        self.store.entries[reference] = {
            "action": mapping.action,
            "creation_pending": True,
            "entity_id": mapping.metadata.entity_id,
            "first_detected": dt_util.now().isoformat(),
            "last_percent": percentage,
            "name": mapping.metadata.device_name,
            "pending_since": dt_util.now().isoformat(),
            "physical_key": mapping.metadata.physical_key,
            "task_id": 0,
        }

    def _find_adoption_candidate(
        self,
        tasks: list[dict[str, Any]],
        reference: str,
        expected_name: str,
        device_name: str,
        claimed_task_ids: set[int],
    ) -> dict[str, Any] | None:
        """Find one existing task by reference or exact title."""
        referenced = [
            item
            for item in tasks
            if reference_marker(reference) in str(item.get("description", ""))
            and parse_task_id(str(item.get("uid", ""))) not in claimed_task_ids
        ]
        if len(referenced) > 1:
            raise HomeAssistantError(
                f"Multiple DoneTick tasks use maintenance reference {reference}"
            )
        if referenced:
            return referenced[0]

        matching_names = [
            item
            for item in tasks
            if item.get("summary") == expected_name
            and parse_task_id(str(item.get("uid", ""))) not in claimed_task_ids
            and self._is_legacy_managed_task(item, device_name)
        ]
        if len(matching_names) > 1:
            raise HomeAssistantError(
                f"Multiple DoneTick tasks are named {expected_name}"
            )
        return matching_names[0] if matching_names else None

    @staticmethod
    def _is_legacy_managed_task(item: dict[str, Any], device_name: str) -> bool:
        """Recognize the temporary pre-integration task format."""
        description = str(item.get("description", ""))
        return (
            "Maintenance reference:" not in description
            and description.startswith(f"The {device_name} battery is at ")
            and (
                "Home Assistant updates this task when the reported percentage "
                "changes. Complete it after the battery has been replaced or "
                "recharged and the device is reporting normally."
            )
            in description
        )

    def _find_reference_candidate(
        self,
        tasks: list[dict[str, Any]],
        reference: str,
    ) -> dict[str, Any] | None:
        """Find exactly one active task by its managed reference."""
        return self._find_marker_candidate(
            tasks,
            reference_marker(reference),
            set(),
        )

    @staticmethod
    def _find_marker_candidate(
        tasks: list[dict[str, Any]],
        marker: str,
        claimed_task_ids: set[int],
    ) -> dict[str, Any] | None:
        """Find exactly one unclaimed active task by a managed marker."""
        matching = [
            item
            for item in tasks
            if marker in str(item.get("description", ""))
            and parse_task_id(str(item.get("uid", ""))) not in claimed_task_ids
        ]
        if len(matching) > 1:
            raise HomeAssistantError(
                f"Multiple DoneTick tasks use managed marker {marker}"
            )
        return matching[0] if matching else None

    async def _async_active_tasks(self) -> list[dict[str, Any]]:
        """Fetch active DoneTick tasks from its internal todo entity."""
        todo_entity = donetick_internal_todo_entity(self.hass, self.donetick_entry_id)
        if todo_entity is None:
            raise HomeAssistantError(
                "The selected DoneTick entry has no internal task list"
            )
        todo_state = self.hass.states.get(todo_entity)
        if todo_state is None or todo_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            raise HomeAssistantError(
                "The selected DoneTick internal task list is unavailable"
            )
        response = await self.hass.services.async_call(
            "todo",
            "get_items",
            {"status": ["needs_action"]},
            target={"entity_id": todo_entity},
            blocking=True,
            return_response=True,
        )
        if not isinstance(response, dict) or todo_entity not in response:
            raise HomeAssistantError(
                "DoneTick's internal todo list returned an invalid response"
            )
        items = response[todo_entity].get("items", [])
        if not isinstance(items, list):
            raise HomeAssistantError(
                "DoneTick's internal todo list returned an invalid response"
            )
        return [dict(item) for item in items if isinstance(item, dict)]

    async def _async_create_task(
        self,
        name: str,
        description: str,
    ) -> None:
        """Create a managed DoneTick task."""
        local_now: datetime = dt_util.now()
        await self.hass.services.async_call(
            DONETICK_DOMAIN,
            DONETICK_CREATE_SERVICE,
            {
                "assignees": "",
                "description": description,
                "due_date": due_at_five(local_now).isoformat(),
                "hide_on_vacation": False,
                "name": name,
                "notification": False,
                "priority": "high",
                "recurrence": "no_repeat",
                "config_entry_id": self.donetick_entry_id,
            },
            blocking=True,
        )

    async def _async_complete_task(self, task_id: int) -> None:
        """Complete a managed DoneTick task after battery recovery."""
        await self.hass.services.async_call(
            DONETICK_DOMAIN,
            DONETICK_COMPLETE_SERVICE,
            {
                "task_id": task_id,
                "config_entry_id": self.donetick_entry_id,
            },
            blocking=True,
        )

    async def _async_create_review_task(
        self,
        name: str,
        description: str,
    ) -> None:
        """Create a Stephen-owned battery categorization task."""
        local_now: datetime = dt_util.now()
        await self.hass.services.async_call(
            DONETICK_DOMAIN,
            DONETICK_CREATE_SERVICE,
            {
                "assignees": str(REVIEW_ASSIGNEE_ID),
                "config_entry_id": self.donetick_entry_id,
                "description": description,
                "due_date": due_at_five(local_now).isoformat(),
                "hide_on_vacation": False,
                "name": name,
                "notification": False,
                "priority": "medium",
                "recurrence": "no_repeat",
            },
            blocking=True,
        )

    async def _async_update_task(
        self,
        task_id: int,
        name: str,
        description: str,
    ) -> None:
        """Update managed fields without moving the due date."""
        await self.hass.services.async_call(
            DONETICK_DOMAIN,
            DONETICK_UPDATE_SERVICE,
            {
                "description": description,
                "name": name,
                "task_id": task_id,
                "config_entry_id": self.donetick_entry_id,
            },
            blocking=True,
        )
