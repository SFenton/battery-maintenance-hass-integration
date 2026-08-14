"""Pure helpers for Battery Maintenance."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from .const import (
    ACTION_CHARGE,
    REFERENCE_PREFIX,
    REVIEW_REFERENCE_PREFIX,
    TASK_DUE_HOUR,
)


def stable_reference(physical_key: str) -> str:
    """Return a short stable task reference without exposing the hardware ID."""
    digest = sha256(physical_key.encode()).hexdigest()[:8].upper()
    return f"{REFERENCE_PREFIX}{digest}"


def stable_review_reference(entity_key: str) -> str:
    """Return a stable reference for one discovered battery entity."""
    digest = sha256(entity_key.encode()).hexdigest()[:8].upper()
    return f"{REVIEW_REFERENCE_PREFIX}{digest}"


def format_percent(value: float) -> str:
    """Format a percentage without unnecessary decimal places."""
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}"


def task_name(action: str, device_name: str, percentage: float) -> str:
    """Build the managed DoneTick task name."""
    if action == ACTION_CHARGE:
        base_name = f"Charge {device_name}"
    else:
        suffix = "" if device_name.lower().endswith(" battery") else " battery"
        base_name = f"Replace {device_name}{suffix}"
    return f"{base_name} \u00b7 {format_percent(percentage)}%"


def task_description(
    action: str,
    device_name: str,
    percentage: float,
    area_name: str | None,
    reference: str,
) -> str:
    """Build the managed DoneTick task description."""
    action_text = "charged" if action == ACTION_CHARGE else "replaced"
    area = f"\n\nArea: {area_name}." if area_name else ""
    return (
        f"The {device_name} battery is at {format_percent(percentage)}%."
        f"{area}\n\n"
        "Home Assistant updates this task when the reported percentage changes. "
        f"Complete it after the battery has been {action_text} and the device is "
        f"reporting normally.\n\nMaintenance reference: {reference}."
    )


def due_at_five(local_now: datetime) -> datetime:
    """Return 5:00 PM on the detection date in the supplied timezone."""
    return local_now.replace(hour=TASK_DUE_HOUR, minute=0, second=0, microsecond=0)


def parse_task_id(uid: str) -> int | None:
    """Extract the stable DoneTick task ID from a todo item UID."""
    task_id, _, _ = uid.partition("--")
    try:
        parsed = int(task_id)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def reference_marker(reference: str) -> str:
    """Return the marker used to adopt an existing managed task."""
    return f"Maintenance reference: {reference}."


def review_reference_marker(reference: str) -> str:
    """Return the marker used to deduplicate categorization tasks."""
    return f"Battery review reference: {reference}."


def review_task_name(entity_name: str) -> str:
    """Build the categorization task title."""
    return f"Categorize battery: {entity_name}"


def review_task_description(
    entity_name: str,
    current_state: str,
    reference: str,
) -> str:
    """Build instructions for categorizing a discovered battery entity."""
    return (
        f"Home Assistant discovered a new battery entity: {entity_name}.\n\n"
        f"Current reading: {current_state}.\n\n"
        "Open Settings > Devices & services > Battery Maintenance > Configure. "
        "Move this entity from Unknown to Replace or Charge, or leave it in "
        "Unknown to ignore it. Then complete this task.\n\n"
        f"{review_reference_marker(reference)}"
    )
