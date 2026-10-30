"""Birdfy sensors — last detection event and identified species."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import BirdfyConfigEntry, BirdfyCoordinator
from .entity import BirdfyAiEntity, BirdfyEntity
from .identification import BirdfyIdentifier

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: BirdfyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        BirdfyLastEventSensor(coordinator, entry),
        BirdfyHighlightsSensor(coordinator, entry),
        BirdfyRecapSensor(coordinator, entry),
    ]
    if identifier := coordinator.identifier:
        entities += [
            BirdfyLastSpeciesSensor(identifier, entry),
            BirdfySpeciesCountSensor(identifier, entry),
            BirdfySpeciesTodaySensor(identifier, entry),
            BirdfyVisitsTodaySensor(identifier, entry),
            BirdfyTopSpeciesSensor(identifier, entry),
        ]
    async_add_entities(entities)


class BirdfyLastEventSensor(BirdfyEntity, SensorEntity):
    """Last detection event from the Birdfy camera."""

    _attr_translation_key = "last_event"
    _unrecorded_attributes = frozenset({"recent_events"})

    def __init__(self, coordinator: BirdfyCoordinator, entry: BirdfyConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_event"

    @property
    def native_value(self) -> str:
        last = self.coordinator.data.get("last_event", {})
        return last.get("label", "unknown")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        last = data.get("last_event", {})
        alert_time = last.get("alert_time", 0)
        return {
            "alarm_id":      last.get("alarm_id"),
            "alert_time":    alert_time,
            "alert_time_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(alert_time / 1000)
            ) if alert_time else None,
            "record_url":    last.get("record_url"),
            "image_url":     self.coordinator.image_url,
            "device_id":     data.get("device_id"),
            "recent_events": data.get("recent_events", []),
        }


class BirdfyHighlightsSensor(BirdfyEntity, SensorEntity):
    """Birdfy highlights URL sensor."""

    _attr_translation_key = "highlights"

    def __init__(self, coordinator: BirdfyCoordinator, entry: BirdfyConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_highlights"

    @property
    def native_value(self) -> str:
        return self.coordinator.highlights_url or None


class BirdfyRecapSensor(BirdfyEntity, SensorEntity):
    """Birdfy recap URL sensor."""

    _attr_translation_key = "recap"

    def __init__(self, coordinator: BirdfyCoordinator, entry: BirdfyConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_recap"

    @property
    def native_value(self) -> str:
        return self.coordinator.recap_url or None


class BirdfyLastSpeciesSensor(BirdfyAiEntity, SensorEntity):
    """Species of the last bird identified."""

    def __init__(self, identifier: BirdfyIdentifier, entry: BirdfyConfigEntry) -> None:
        super().__init__(identifier, entry, "last_species")

    @property
    def native_value(self) -> str | None:
        return self.store.last["species"] if self.store.last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.store.last or {}
        return {
            key: last.get(key)
            for key in ("scientific_name", "confidence", "count", "alarm_id", "seen_at")
        }


class BirdfySpeciesCountSensor(BirdfyAiEntity, SensorEntity):
    """Number of species seen, with the complete list."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"species"})

    def __init__(self, identifier: BirdfyIdentifier, entry: BirdfyConfigEntry) -> None:
        super().__init__(identifier, entry, "species_count")

    @property
    def native_value(self) -> int:
        return len(self.store.species)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"species": self.store.species_list()}


class BirdfySpeciesTodaySensor(BirdfyAiEntity, SensorEntity):
    """Number of species seen today."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, identifier: BirdfyIdentifier, entry: BirdfyConfigEntry) -> None:
        super().__init__(identifier, entry, "species_today")

    @property
    def native_value(self) -> int:
        return len(self.store.today["species"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"species": self.store.species_today()}


class BirdfyVisitsTodaySensor(BirdfyAiEntity, SensorEntity):
    """Number of clips with a bird today."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, identifier: BirdfyIdentifier, entry: BirdfyConfigEntry) -> None:
        super().__init__(identifier, entry, "visits_today")

    @property
    def native_value(self) -> int:
        return self.store.today["visits"]


class BirdfyTopSpeciesSensor(BirdfyAiEntity, SensorEntity):
    """Most frequently seen species."""

    def __init__(self, identifier: BirdfyIdentifier, entry: BirdfyConfigEntry) -> None:
        super().__init__(identifier, entry, "top_species")

    @property
    def native_value(self) -> str | None:
        top = self.store.top_species()
        return top["name"] if top else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        top = self.store.top_species()
        return {"count": top["count"] if top else 0}
