"""Birdfy actions."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
import os
from pathlib import Path
import shutil

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import BirdfyCoordinator, alert_datetime, loaded_coordinator
from .http import async_resolve_source, async_write_mp4, remove_file, temp_file
from .identification import BirdfyIdentifier

_LOGGER = logging.getLogger(__name__)

type SaveClip = Callable[[BirdfyCoordinator, str, str], Awaitable[bool]]

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


def _coordinator(hass: HomeAssistant) -> BirdfyCoordinator:
    coordinator = loaded_coordinator(hass)
    if coordinator is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_loaded"
        )
    return coordinator


def _identifier(hass: HomeAssistant) -> BirdfyIdentifier:
    identifier = _coordinator(hass).identifier
    if identifier is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="ai_not_configured"
        )
    return identifier


async def _async_events_for_day(coord: BirdfyCoordinator, date_str: str) -> list:
    try:
        return await coord.fetch_events_for_day(date_str)
    except Exception as e:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cloud_error",
            translation_placeholders={"error": str(e)},
        ) from e


async def _async_download(
    call: ServiceCall, default_path: str, extension: str, save: SaveClip
) -> None:
    """Save the events of the last days that are not on disk yet."""
    hass = call.hass
    days = call.data.get("days", 1)
    share = call.data.get("share")
    base_path = f"/media/{share}" if share else call.data.get("path", default_path)

    today = dt_util.now().date()
    downloaded = 0
    skipped = 0

    coord = _coordinator(hass)
    for i in range(days):
        date_str = (today - timedelta(days=i)).isoformat()
        events = await _async_events_for_day(coord, date_str)
        if not events:
            continue

        day_dir = os.path.join(base_path, date_str)
        await hass.async_add_executor_job(_make_dir, day_dir)
        saved = await hass.async_add_executor_job(_saved_alarm_ids, day_dir, extension)

        for ev in events:
            alarm_id = ev.get("alarm_id", "")
            if not alarm_id:
                continue

            if alarm_id in saved:
                skipped += 1
                continue

            alert_time = ev.get("alert_time", 0)
            ts = alert_datetime(alert_time).strftime("%H%M%S") if alert_time else "000000"
            label = ev.get("label", "unknown").replace(" ", "_").replace("/", "-")
            filename = f"{ts}_{label}_{alarm_id}{extension}"
            if await save(coord, alarm_id, os.path.join(day_dir, filename)):
                downloaded += 1

    _LOGGER.info("Birdfy downloads: %d new, %d skipped", downloaded, skipped)


async def _async_save_thumbnail(
    coord: BirdfyCoordinator, alarm_id: str, filepath: str
) -> bool:
    thumbnail_url = coord.thumbnail_cache.get(alarm_id)
    if not thumbnail_url:
        return False

    hass = coord.hass
    try:
        async with async_get_clientsession(hass).get(thumbnail_url) as r:
            if r.status != 200:
                return False
            content = await r.read()
        await hass.async_add_executor_job(_write_file, filepath, content)
    except Exception as e:
        _LOGGER.warning("Failed to download thumbnail %s: %s", alarm_id, e)
        return False
    return True


async def _async_save_video(
    coord: BirdfyCoordinator, alarm_id: str, filepath: str
) -> bool:
    hass = coord.hass
    try:
        source = await async_resolve_source(coord, alarm_id)
    except Exception as e:
        _LOGGER.warning("Failed to find video %s: %s", alarm_id, e)
        return False
    if not source:
        _LOGGER.warning("No video source for %s", alarm_id)
        return False

    tmp_path = await hass.async_add_executor_job(temp_file, ".mp4")
    try:
        success, stderr = await async_write_mp4(hass, source, tmp_path)
        if not success:
            _LOGGER.warning("ffmpeg failed for %s: %s", alarm_id, stderr)
            return False
        await hass.async_add_executor_job(_move_file, tmp_path, filepath)
    except FileNotFoundError as e:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="ffmpeg_not_found"
        ) from e
    except Exception as e:
        _LOGGER.warning("Failed to download video %s: %s", alarm_id, e)
        return False
    finally:
        await hass.async_add_executor_job(_remove_if_exists, tmp_path)
    return True


async def _handle_download_thumbnails(call: ServiceCall) -> None:
    await _async_download(call, DEFAULT_THUMBNAIL_PATH, ".jpg", _async_save_thumbnail)


async def _handle_download_videos(call: ServiceCall) -> None:
    await _async_download(call, DEFAULT_VIDEO_PATH, ".mp4", _async_save_video)


async def _handle_analyze_events(call: ServiceCall) -> None:
    _identifier(call.hass).async_start_backfill(call.data["days"])


async def _handle_remove_species(call: ServiceCall) -> None:
    species = call.data["species"]
    if not _identifier(call.hass).store.async_remove_species(species):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_species",
            translation_placeholders={"species": species},
        )


def _saved_alarm_ids(day_dir: str, extension: str) -> set[str]:
    """Return the events on disk, whatever the time and label in their name."""
    return {
        path.stem.rpartition("_")[2] for path in Path(day_dir).glob(f"*{extension}")
    }


def _make_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_file(filepath: str, content: bytes) -> None:
    part_path = f"{filepath}.part"
    with open(part_path, "wb") as f:
        f.write(content)
    os.replace(part_path, filepath)


def _move_file(source: str, filepath: str) -> None:
    """Move a file, across file systems too, without leaving a cut file at filepath."""
    part_path = f"{filepath}.part"
    shutil.move(source, part_path)
    os.replace(part_path, filepath)


def _remove_if_exists(path: str) -> None:
    if os.path.exists(path):
        remove_file(path)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Birdfy actions, which write to disk or spend AI credits."""
    async_register_admin_service(
        hass, DOMAIN, SERVICE_DOWNLOAD_THUMBNAILS, _handle_download_thumbnails, _MEDIA_SCHEMA
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_DOWNLOAD_VIDEOS, _handle_download_videos, _MEDIA_SCHEMA
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_ANALYZE_EVENTS, _handle_analyze_events, _ANALYZE_SCHEMA
    )
    async_register_admin_service(
        hass, DOMAIN, SERVICE_REMOVE_SPECIES, _handle_remove_species, _REMOVE_SCHEMA
    )
