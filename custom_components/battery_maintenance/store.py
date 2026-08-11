"""Persistent episode ledger for Battery Maintenance."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORE_MINOR_VERSION, STORE_VERSION


class BatteryMaintenanceStore:
    """Persist task pointers and low-battery episode state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        donetick_entry_id: str,
    ) -> None:
        """Initialize the store."""
        self.donetick_entry_id = donetick_entry_id
        self._store = Store[dict[str, Any]](
            hass,
            STORE_VERSION,
            f"{DOMAIN}.{entry_id}.ledger",
            private=True,
            atomic_writes=True,
            minor_version=STORE_MINOR_VERSION,
        )
        self.entries: dict[str, dict[str, Any]] = {}
        self.reviews: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Load persisted ledger state."""
        data = await self._store.async_load()
        if isinstance(data, dict) and data.get("donetick_entry_id") not in (
            None,
            self.donetick_entry_id,
        ):
            self.entries = {}
            self.reviews = {}
            await self.async_save()
            return
        entries = data.get("entries", {}) if isinstance(data, dict) else {}
        reviews = data.get("reviews", {}) if isinstance(data, dict) else {}
        self.entries = {
            str(reference): dict(value)
            for reference, value in entries.items()
            if isinstance(value, dict)
        }
        self.reviews = {
            str(reference): dict(value)
            for reference, value in reviews.items()
            if isinstance(value, dict)
        }

    async def async_save(self) -> None:
        """Persist the current ledger state immediately."""
        await self._store.async_save(
            {
                "donetick_entry_id": self.donetick_entry_id,
                "entries": self.entries,
                "reviews": self.reviews,
            }
        )

    async def async_remove(self) -> None:
        """Remove persisted ledger state."""
        await self._store.async_remove()

    def diagnostics(self) -> dict[str, Any]:
        """Return a serializable copy for diagnostics."""
        return {
            "donetick_entry_id": self.donetick_entry_id,
            "entries": {key: dict(value) for key, value in self.entries.items()},
            "reviews": {key: dict(value) for key, value in self.reviews.items()},
        }
