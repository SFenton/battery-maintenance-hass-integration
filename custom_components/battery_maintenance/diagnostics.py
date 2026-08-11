"""Diagnostics for Battery Maintenance."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data


async def async_get_config_entry_diagnostics(
    hass: Any,
    entry: Any,
) -> dict[str, Any]:
    """Return diagnostics for the config entry."""
    return async_redact_data(
        {
            "options": dict(entry.options),
            **entry.runtime_data.diagnostics(),
        },
        {"physical_key"},
    )
