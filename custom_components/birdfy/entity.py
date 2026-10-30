"""Base entities for Birdfy."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BirdfyConfigEntry, BirdfyCoordinator
from .identification import BirdfyIdentifier


def birdfy_device_info(coordinator: BirdfyCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.device_id)},
        manufacturer="Netvue",
        name="Birdfy",
        serial_number=coordinator.device_id,
    )


class BirdfyEntity(CoordinatorEntity[BirdfyCoordinator]):
    """Entity fed by the Netvue polling."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BirdfyCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = birdfy_device_info(coordinator)


class BirdfyAiEntity(Entity):
    """Entity fed by the AI identifications, available even if the cloud is not."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, identifier: BirdfyIdentifier, entry: BirdfyConfigEntry, key: str
    ) -> None:
        self.identifier = identifier
        self.store = identifier.store
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_ai_{key}"
        self._attr_device_info = birdfy_device_info(entry.runtime_data)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.store.async_add_listener(self._async_store_updated))

    def _async_store_updated(self) -> None:
        self.async_write_ha_state()
