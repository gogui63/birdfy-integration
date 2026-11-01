"""Birdfy Media Source — browse and play recorded events by day."""
from __future__ import annotations

import datetime

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.models import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import BirdfyCoordinator, alert_datetime, loaded_coordinator

DAYS_BACK = 30
FRAME_PREFIX = "frame:"


async def async_get_media_source(hass: HomeAssistant) -> BirdfyMediaSource:
    return BirdfyMediaSource(hass)


class BirdfyMediaSource(MediaSource):
    """Birdfy recorded events as a browsable media source."""

    name = "Birdfy"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _get_coordinator(self) -> BirdfyCoordinator | None:
        return loaded_coordinator(self.hass)

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Return the proxied HLS URL for a given alarm_id."""
        coordinator = self._get_coordinator()
        if coordinator is None:
            raise Unresolvable("Birdfy coordinator not found")

        alarm_id = item.identifier
        if alarm_id.startswith(FRAME_PREFIX):
            return self._resolve_frame(coordinator, alarm_id.removeprefix(FRAME_PREFIX))

        # Not necessarily in recent_events — http.py will fetch it
        if alarm_id.isdecimal():
            return PlayMedia(
                url=f"/api/birdfy/mp4/{alarm_id}",
                mime_type="video/mp4",
            )
        raise Unresolvable(f"Event {alarm_id} not found")

    def _resolve_frame(self, coordinator: BirdfyCoordinator, frame_id: str) -> PlayMedia:
        """Return a frame of a running analysis, the only files exposed here."""
        identifier = coordinator.identifier
        path = identifier.frame_path(frame_id) if identifier else None
        if path is None:
            raise Unresolvable(f"Frame {frame_id} not found")
        return PlayMedia(url="", mime_type="image/jpeg", path=path)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        coordinator = self._get_coordinator()

        identifier = item.identifier or ""

        # Root level — show last 30 days as folders
        if not identifier:
            return self._browse_root(coordinator)

        # Day level — show clips for that day
        if identifier.startswith("day:"):
            date_str = identifier[4:]
            return await self._browse_day(coordinator, date_str)

        # Should not happen
        raise Unresolvable(f"Unknown identifier: {identifier}")

    def _browse_root(self, coordinator: BirdfyCoordinator | None) -> BrowseMediaSource:
        today = dt_util.now().date()
        children = []
        for i in range(DAYS_BACK):
            d = today - datetime.timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")
            label = d.strftime("%d %b %Y")
            if i == 0:
                label = f"Today — {label}"
            elif i == 1:
                label = f"Yesterday — {label}"
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"day:{date_str}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.VIDEO,
                    title=label,
                    can_play=False,
                    can_expand=True,
                )
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier="",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title="Birdfy",
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def _browse_day(self, coordinator: BirdfyCoordinator | None, date_str: str) -> BrowseMediaSource:
        if coordinator is None:
            events = []
        else:
            events = await coordinator.fetch_events_for_day(date_str)

        children = []
        for ev in events:
            if not ev.get("record_url") and not coordinator.segments_cache.get(ev.get("alarm_id")):
                continue
            alarm_id = ev["alarm_id"]
            alert_time = ev.get("alert_time", 0)
            ts = alert_datetime(alert_time).strftime("%H:%M:%S") if alert_time else "?"
            label = ev.get("label", "unknown")
            title = f"{ts} — {label}"
            thumbnail = coordinator.thumbnail_cache.get(alarm_id) if coordinator else None
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=alarm_id,
                    media_class=MediaClass.VIDEO,
                    media_content_type=MediaType.VIDEO,
                    title=title,
                    can_play=True,
                    can_expand=False,
                    thumbnail=thumbnail,
                )
            )

        d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        title = d.strftime("%d %B %Y") + f" ({len(children)} clips)"

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"day:{date_str}",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
        )
