"""Birdfy image — best frame of the last identified bird."""
from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import BirdfyConfigEntry
from .entity import BirdfyAiEntity
from .identification import BirdfyIdentifier

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: BirdfyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if identifier := entry.runtime_data.identifier:
        async_add_entities([BirdfyLastBirdImage(hass, identifier, entry)])


class BirdfyLastBirdImage(BirdfyAiEntity, ImageEntity):
    """Image saved for the last identified bird."""

    _attr_content_type = "image/jpeg"

    def __init__(
        self, hass: HomeAssistant, identifier: BirdfyIdentifier, entry: BirdfyConfigEntry
    ) -> None:
        ImageEntity.__init__(self, hass)
        BirdfyAiEntity.__init__(self, identifier, entry, "last_bird")
        self._set_last_updated()

    def _set_last_updated(self) -> None:
        last = self.store.last
        self._attr_image_last_updated = (
            dt_util.parse_datetime(last["seen_at"]) if last else None
        )

    def _async_store_updated(self) -> None:
        self._set_last_updated()
        self.async_write_ha_state()

    def _read_image(self) -> bytes | None:
        path = self.identifier.image_path
        return path.read_bytes() if path.exists() else None

    async def async_image(self) -> bytes | None:
        if self.store.last is None:
            return None
        return await self.hass.async_add_executor_job(self._read_image)
