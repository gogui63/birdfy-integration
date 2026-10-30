"""Birdfy event — fires for every bird seen by the AI."""
from __future__ import annotations

from dataclasses import asdict

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import EVENT_KNOWN_SPECIES, EVENT_NEW_SPECIES, EVENT_UNIDENTIFIED_BIRD
from .coordinator import BirdfyConfigEntry
from .entity import BirdfyAiEntity
from .identification import BirdfyIdentifier
from .species_store import Identification

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: BirdfyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if identifier := entry.runtime_data.identifier:
        async_add_entities([BirdfyBirdDetectedEvent(identifier, entry)])


class BirdfyBirdDetectedEvent(BirdfyAiEntity, EventEntity):
    """One event per clip containing a bird."""

    _attr_event_types = [EVENT_NEW_SPECIES, EVENT_KNOWN_SPECIES, EVENT_UNIDENTIFIED_BIRD]

    def __init__(self, identifier: BirdfyIdentifier, entry: BirdfyConfigEntry) -> None:
        super().__init__(identifier, entry, "bird_detected")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.identifier.signal, self._async_bird_detected
            )
        )

    def _async_store_updated(self) -> None:
        """The state of an event entity only changes when it fires."""

    @callback
    def _async_bird_detected(self, event_type: str, result: Identification) -> None:
        attributes = asdict(result)
        attributes.pop("best_frame")
        attributes.pop("bird_visible")
        attributes["seen_at"] = dt_util.utc_from_timestamp(
            attributes.pop("alert_time") / 1000
        ).isoformat()
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
