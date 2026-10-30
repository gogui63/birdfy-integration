"""Birdfy camera — thumbnail of last detection."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import BirdfyConfigEntry, BirdfyCoordinator
from .entity import BirdfyEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: BirdfyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([BirdfyThumbnailCamera(entry.runtime_data, entry)])


class BirdfyThumbnailCamera(BirdfyEntity, Camera):
    """Shows the thumbnail image of the last Birdfy detection event."""

    _attr_supported_features = CameraEntityFeature(0)
    _attr_is_streaming = False
    _attr_translation_key = "thumbnail"

    def __init__(self, coordinator: BirdfyCoordinator, entry: BirdfyConfigEntry) -> None:
        BirdfyEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = f"{entry.entry_id}_thumbnail"
        self._cached_image: bytes | None = None
        self._cached_url: str = ""

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        url = self.coordinator.image_url
        if not url:
            return self._cached_image
        if url == self._cached_url and self._cached_image:
            return self._cached_image
        try:
            async with async_get_clientsession(self.hass).get(url) as r:
                if r.status == 200:
                    self._cached_image = await r.read()
                    self._cached_url = url
                    return self._cached_image
        except Exception as e:
            _LOGGER.warning("Failed to fetch Birdfy thumbnail: %s", e)
        return self._cached_image
