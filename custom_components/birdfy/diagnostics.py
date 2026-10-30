"""Diagnostics for Birdfy."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .coordinator import BirdfyConfigEntry

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, "ucid", "udid", "title", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BirdfyConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    identifier = coordinator.identifier
    diagnostics: dict[str, Any] = {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
    }
    if identifier:
        store = identifier.store
        diagnostics["identification"] = {
            "analyzed": len(store.analyzed),
            "failed": sum(s == "failed" for s in store.analyzed.values()),
            "species": store.species_list(),
            "today": store.today,
            "last": store.last,
        }
    return diagnostics
