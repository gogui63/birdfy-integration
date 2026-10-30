"""Birdfy actions."""
from __future__ import annotations

import datetime
import logging
import os
import shutil
import time

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .coordinator import BirdfyCoordinator, loaded_coordinators
from .http import async_resolve_source, async_write_mp4, remove_file, temp_file
from .identification import BirdfyIdentifier

_LOGGER = logging.getLogger(__name__)

DEFAULT_THUMBNAIL_PATH = "/media/birdfy_thumbnails"
DEFAULT_VIDEO_PATH = "/media/birdfy_videos"
SERVICE_DOWNLOAD_THUMBNAILS = "download_thumbnails"
SERVICE_DOWNLOAD_VIDEOS = "download_videos"
SERVICE_ANALYZE_EVENTS = "analyze_events"
SERVICE_REMOVE_SPECIES = "remove_species"

_MEDIA_SCHEMA = vol.Schema({
    vol.Optional("days", default=1): vol.All(int, vol.Range(min=1, max=30)),
    vol.Optional("share"): cv.string,
    vol.Optional("path"): cv.string,
})
_ANALYZE_SCHEMA = vol.Schema({
    vol.Optional("days", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
})
_REMOVE_SCHEMA = vol.Schema({vol.Required("species"): cv.string})


def _coordinators(hass: HomeAssistant) -> list[BirdfyCoordinator]:
    coordinators = loaded_coordinators(hass)
    if not coordinators:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_loaded"
        )
    return coordinators


def _identifiers(hass: HomeAssistant) -> list[BirdfyIdentifier]:
    identifiers = [c.identifier for c in _coordinators(hass) if c.identifier]
    if not identifiers:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="ai_not_configured"
        )
    return identifiers


async def _async_events_for_day(coord: BirdfyCoordinator, date_str: str) -> list:
    try:
        return await coord.fetch_events_for_day(date_str)
    except Exception as e:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_error",
            translation_placeholders={"error": str(e)},
        ) from e


async def _handle_download_thumbnails(call: ServiceCall) -> None:
    hass = call.hass
    days = call.data.get("days", 1)
    share = call.data.get("share")
    base_path = f"/media/{share}" if share else call.data.get("path", DEFAULT_THUMBNAIL_PATH)

    today = datetime.date.today()
    downloaded = 0
    skipped = 0

    coordinators = _coordinators(hass)
    session = async_get_clientsession(hass)
    for coord in coordinators:
        device_id = coord.device_id or "unknown"
        device_path = os.path.join(base_path, device_id) if len(coordinators) > 1 else base_path

        for i in range(days):
            d = today - datetime.timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")
            events = await _async_events_for_day(coord, date_str)
            if not events:
                continue

            day_dir = os.path.join(device_path, date_str)
            await hass.async_add_executor_job(_make_dir, day_dir)

            for ev in events:
                alarm_id = ev.get("alarm_id", "")
                if not alarm_id:
                    continue

                thumbnail_url = coord.thumbnail_cache.get(alarm_id)
                if not thumbnail_url:
                    continue

                alert_time = ev.get("alert_time", 0)
                ts = time.strftime("%H%M%S", time.localtime(alert_time / 1000)) if alert_time else "000000"
                label = ev.get("label", "unknown").replace(" ", "_").replace("/", "-")
                filename = f"{ts}_{label}_{alarm_id}.jpg"
                filepath = os.path.join(day_dir, filename)

                if await hass.async_add_executor_job(os.path.exists, filepath):
                    skipped += 1
                    continue

                try:
                    async with session.get(thumbnail_url) as r:
                        if r.status == 200:
                            content = await r.read()
                            await hass.async_add_executor_job(_write_file, filepath, content)
                            downloaded += 1
                except Exception as e:
                    _LOGGER.warning("Failed to download thumbnail %s: %s", alarm_id, e)

    _LOGGER.info("Birdfy thumbnails: %d downloaded, %d skipped", downloaded, skipped)


async def _handle_download_videos(call: ServiceCall) -> None:
    hass = call.hass
    days = call.data.get("days", 1)
    share = call.data.get("share")
    base_path = f"/media/{share}" if share else call.data.get("path", DEFAULT_VIDEO_PATH)

    today = datetime.date.today()
    downloaded = 0
    skipped = 0

    coordinators = _coordinators(hass)
    for coord in coordinators:
        device_id = coord.device_id or "unknown"
        device_path = os.path.join(base_path, device_id) if len(coordinators) > 1 else base_path

        for i in range(days):
            d = today - datetime.timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")
            events = await _async_events_for_day(coord, date_str)
            if not events:
                continue

            day_dir = os.path.join(device_path, date_str)
            await hass.async_add_executor_job(_make_dir, day_dir)

            for ev in events:
                alarm_id = ev.get("alarm_id", "")
                if not alarm_id:
                    continue

                alert_time = ev.get("alert_time", 0)
                ts = time.strftime("%H%M%S", time.localtime(alert_time / 1000)) if alert_time else "000000"
                label = ev.get("label", "unknown").replace(" ", "_").replace("/", "-")
                filename = f"{ts}_{label}_{alarm_id}.mp4"
                filepath = os.path.join(day_dir, filename)

                if await hass.async_add_executor_job(os.path.exists, filepath):
                    skipped += 1
                    continue

                source = await async_resolve_source(coord, alarm_id)
                if not source:
                    _LOGGER.warning("No video source for %s", alarm_id)
                    continue

                tmp_path = await hass.async_add_executor_job(temp_file, ".mp4")
                try:
                    success, stderr = await async_write_mp4(hass, source, tmp_path)
                    if not success:
                        _LOGGER.warning("ffmpeg failed for %s: %s", alarm_id, stderr)
                        continue
                    await hass.async_add_executor_job(shutil.move, tmp_path, filepath)
                    downloaded += 1
                except FileNotFoundError as e:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="ffmpeg_not_found"
                    ) from e
                except Exception as e:
                    _LOGGER.warning("Failed to download video %s: %s", alarm_id, e)
                finally:
                    await hass.async_add_executor_job(_remove_if_exists, tmp_path)

    _LOGGER.info("Birdfy videos: %d downloaded, %d skipped", downloaded, skipped)


async def _handle_analyze_events(call: ServiceCall) -> None:
    for identifier in _identifiers(call.hass):
        identifier.async_start_backfill(call.data["days"])


async def _handle_remove_species(call: ServiceCall) -> None:
    species = call.data["species"]
    removed = [i.store.async_remove_species(species) for i in _identifiers(call.hass)]
    if not any(removed):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_species",
            translation_placeholders={"species": species},
        )


def _make_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_file(filepath: str, content: bytes) -> None:
    with open(filepath, "wb") as f:
        f.write(content)


def _remove_if_exists(path: str) -> None:
    if os.path.exists(path):
        remove_file(path)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Birdfy actions."""
    hass.services.async_register(
        DOMAIN, SERVICE_DOWNLOAD_THUMBNAILS, _handle_download_thumbnails, schema=_MEDIA_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DOWNLOAD_VIDEOS, _handle_download_videos, schema=_MEDIA_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ANALYZE_EVENTS, _handle_analyze_events, schema=_ANALYZE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_SPECIES, _handle_remove_species, schema=_REMOVE_SCHEMA
    )
