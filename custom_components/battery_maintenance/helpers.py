"""Pure helpers for Battery Maintenance."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from .const import ACTION_CHARGE, REFERENCE_PREFIX, TASK_DUE_HOUR


def stable_reference(physical_key: str) -> str:
    """Return a short stable task reference without exposing the hardware ID."""
    digest = sha256(physical_key.encode()).hexdigest()[:8].upper()
    return f"{REFERENCE_PREFIX}{digest}"


def format_percent(value: float) -> str:
    """Format a percentage without unnecessary decimal places."""
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}"


def task_name(action: str, device_name: str) -> str:
    """Build the managed DoneTick task name."""
    if action == ACTION_CHARGE:
        return f"Charge {device_name}"
    return f"Replace {device_name} battery"


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
