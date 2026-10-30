"""Birdfy integration."""
from __future__ import annotations

from pathlib import Path

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.helpers.typing import ConfigType

from .const import CONF_AI_TASK_ENTITY, DOMAIN
from .coordinator import BirdfyConfigEntry, BirdfyCoordinator
from .http import register_views
from .identification import BirdfyIdentifier
from .services import async_setup_services
from .species_store import SpeciesStore

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    register_views(hass)
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BirdfyConfigEntry) -> bool:
    coordinator = BirdfyCoordinator(
        hass,
        entry,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        ucid=entry.data.get("ucid", ""),
        udid=entry.data.get("udid", ""),
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    if entry.options.get(CONF_AI_TASK_ENTITY):
        store = SpeciesStore(hass, entry.entry_id)
        await store.async_load()
        coordinator.identifier = BirdfyIdentifier(hass, entry, coordinator, store)
    else:
        _async_remove_ai_entities(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if coordinator.identifier:
        coordinator.identifier.async_start()
    return True


@callback
def _async_remove_ai_entities(hass: HomeAssistant, entry: BirdfyConfigEntry) -> None:
    """Drop the AI entities once AI is disabled. The species list is kept."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id.startswith(f"{entry.entry_id}_ai_"):
            registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: BirdfyConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: BirdfyConfigEntry) -> None:
    """Delete the species list and the saved image with the integration."""
    await SpeciesStore(hass, entry.entry_id).async_remove()
    image_path = Path(
        hass.config.path(STORAGE_DIR, DOMAIN, f"{entry.entry_id}_last_bird.jpg")
    )
    await hass.async_add_executor_job(image_path.unlink, True)
