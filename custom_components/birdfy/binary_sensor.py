"""Birdfy binary sensor — was the last identified bird a new species."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import BirdfyConfigEntry
from .entity import BirdfyAiEntity
from .identification import BirdfyIdentifier

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: BirdfyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if identifier := entry.runtime_data.identifier:
        async_add_entities([BirdfyNewSpeciesBinarySensor(identifier, entry)])


class BirdfyNewSpeciesBinarySensor(BirdfyAiEntity, BinarySensorEntity):
    """On when the last identified bird is the first of its species."""

    def __init__(self, identifier: BirdfyIdentifier, entry: BirdfyConfigEntry) -> None:
        super().__init__(identifier, entry, "new_species")

    @property
    def is_on(self) -> bool:
        return self.store.last_is_new

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.store.last or {}
        return {"species": last.get("species"), "alarm_id": last.get("alarm_id")}
