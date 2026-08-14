"""Tests for pure helper functions."""

from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.battery_maintenance.const import (
    ACTION_CHARGE,
    ACTION_REPLACE,
)
from custom_components.battery_maintenance.helpers import (
    due_at_five,
    format_percent,
    parse_task_id,
    stable_reference,
    task_description,
    task_name,
)


def test_task_copy_and_reference_are_stable() -> None:
    """Task copy reflects the selected maintenance action."""
    reference = stable_reference("mac:446755525e61")

    assert reference == stable_reference("mac:446755525e61")
    assert reference.startswith("BATT-")
    assert (
        task_name(ACTION_REPLACE, "Front Yard", 13)
        == "Replace Front Yard battery \u00b7 13%"
    )
    assert (
        task_name(ACTION_CHARGE, "Smart Lock", 19.5)
        == "Charge Smart Lock \u00b7 19.5%"
    )
    assert "13%" in task_description(
        ACTION_REPLACE,
        "Front Yard",
        13,
        "Entryway",
        reference,
    )
    assert "charged" in task_description(
        ACTION_CHARGE,
        "Smart Lock",
        19.5,
        None,
        reference,
    )


def test_percentage_due_time_and_uid_parsing() -> None:
    """Formatting and task identity preserve required semantics."""
    local_now = datetime(2026, 8, 11, 18, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

    assert format_percent(13.0) == "13"
    assert format_percent(19.5) == "19.5"
    assert due_at_five(local_now) == datetime(
        2026,
        8,
        11,
        17,
        0,
        tzinfo=ZoneInfo("America/Los_Angeles"),
    )
    assert parse_task_id("515--2026-08-12 00:00:00+00:00") == 515
    assert parse_task_id("bad--value") is None
