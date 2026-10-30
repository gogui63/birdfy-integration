"""Birdfy integration."""
from __future__ import annotations

import logging
import os
import tempfile
import time

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import BirdfyCoordinator
from .http import async_resolve_source, async_write_mp4, register_views

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.CAMERA]

DEFAULT_THUMBNAIL_PATH = "/media/birdfy_thumbnails"
DEFAULT_VIDEO_PATH = "/media/birdfy_videos"
SERVICE_DOWNLOAD_THUMBNAILS = "download_thumbnails"
SERVICE_DOWNLOAD_VIDEOS = "download_videos"

_MEDIA_SCHEMA = vol.Schema({
    vol.Optional("days", default=1): vol.All(int, vol.Range(min=1, max=30)),
    vol.Optional("share"): cv.string,
    vol.Optional("path"): cv.string,
})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = BirdfyCoordinator(
        hass,
        email=entry.data.get(CONF_EMAIL) or entry.data.get("email", ""),
        password=entry.data.get(CONF_PASSWORD) or entry.data.get("password", ""),
        ucid=entry.data.get("ucid", ""),
        udid=entry.data.get("udid", ""),
    )
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    register_views(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_download_thumbnails(call: ServiceCall) -> None:
        days = call.data.get("days", 1)
        share = call.data.get("share")
        base_path = f"/media/{share}" if share else call.data.get("path", DEFAULT_THUMBNAIL_PATH)

        import datetime
        today = datetime.date.today()
        downloaded = 0
        skipped = 0

        coordinators = list(hass.data.get(DOMAIN, {}).values())
        async with aiohttp.ClientSession() as session:
            for coord in coordinators:
                device_id = coord._device_id or "unknown"
                device_path = os.path.join(base_path, device_id) if len(coordinators) > 1 else base_path

                for i in range(days):
                    d = today - datetime.timedelta(days=i)
                    date_str = d.strftime("%Y-%m-%d")
                    events = await coord.fetch_events_for_day(date_str)
                    if not events:
                        continue

                    day_dir = os.path.join(device_path, date_str)
                    os.makedirs(day_dir, exist_ok=True)

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

                        if os.path.exists(filepath):
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

    async def handle_download_videos(call: ServiceCall) -> None:
        days = call.data.get("days", 1)
        share = call.data.get("share")
        base_path = f"/media/{share}" if share else call.data.get("path", DEFAULT_VIDEO_PATH)

        import datetime
        today = datetime.date.today()
        downloaded = 0
        skipped = 0

        coordinators = list(hass.data.get(DOMAIN, {}).values())
        for coord in coordinators:
            device_id = coord._device_id or "unknown"
            device_path = os.path.join(base_path, device_id) if len(coordinators) > 1 else base_path

            for i in range(days):
                d = today - datetime.timedelta(days=i)
                date_str = d.strftime("%Y-%m-%d")
                events = await coord.fetch_events_for_day(date_str)
                if not events:
                    continue

                day_dir = os.path.join(device_path, date_str)
                os.makedirs(day_dir, exist_ok=True)

                for ev in events:
                    alarm_id = ev.get("alarm_id", "")
                    if not alarm_id:
                        continue

                    alert_time = ev.get("alert_time", 0)
                    ts = time.strftime("%H%M%S", time.localtime(alert_time / 1000)) if alert_time else "000000"
                    label = ev.get("label", "unknown").replace(" ", "_").replace("/", "-")
                    filename = f"{ts}_{label}_{alarm_id}.mp4"
                    filepath = os.path.join(day_dir, filename)

                    if os.path.exists(filepath):
                        skipped += 1
                        continue

                    source = await async_resolve_source(coord, alarm_id)
                    if not source:
                        _LOGGER.warning("No video source for %s", alarm_id)
                        continue

                    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
                    os.close(tmp_fd)
                    try:
                        success, stderr = await async_write_mp4(hass, source, tmp_path)
                        if not success:
                            _LOGGER.warning("ffmpeg failed for %s: %s", alarm_id, stderr)
                            continue
                        await hass.async_add_executor_job(_move_file, tmp_path, filepath)
                        downloaded += 1
                    except FileNotFoundError:
                        _LOGGER.error("ffmpeg not found — cannot download videos")
                        return
                    except Exception as e:
                        _LOGGER.warning("Failed to download video %s: %s", alarm_id, e)
                    finally:
                        if os.path.exists(tmp_path):
                            os.unlink(tmp_path)

        _LOGGER.info("Birdfy videos: %d downloaded, %d skipped", downloaded, skipped)

    hass.services.async_register(
        DOMAIN, SERVICE_DOWNLOAD_THUMBNAILS, handle_download_thumbnails, schema=_MEDIA_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DOWNLOAD_VIDEOS, handle_download_videos, schema=_MEDIA_SCHEMA
    )

    return True


def _write_file(filepath: str, content: bytes) -> None:
    with open(filepath, "wb") as f:
        f.write(content)


def _move_file(src: str, dst: str) -> None:
    import shutil
    shutil.move(src, dst)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
