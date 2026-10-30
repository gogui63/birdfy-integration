"""Bird identification of Birdfy clips through an AI Task entity."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
import logging
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any
import uuid

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_MAX_DAILY_ANALYSES,
    DEFAULT_MAX_DAILY_ANALYSES,
    DOMAIN,
)
from .coordinator import BirdfyConfigEntry, BirdfyCoordinator
from .http import async_extract_frames, async_resolve_source
from .species_store import STATUS_DONE, STATUS_FAILED, Identification, SpeciesStore

_LOGGER = logging.getLogger(__name__)

MAX_FRAMES = 6
MAX_ATTEMPTS = 3
CLIP_AVAILABLE_DELAY = 60
ANALYSIS_TIMEOUT = 120
CONFIDENCES = ("high", "medium", "low")
ISSUE_ENTITY_MISSING = "ai_task_entity_missing"

SIGNAL_BIRD_EVENT = f"{DOMAIN}_bird_event_{{}}"


class AnalysisError(Exception):
    """A clip could not be analyzed."""


def build_structure(frames: int) -> dict[str, Any]:
    """Return the structured answer requested from the AI."""
    return {
        "bird_visible": {
            "description": "True if at least one real bird is visible in the images",
            "required": True,
            "selector": {"boolean": {}},
        },
        "species": {
            "description": "Common name of the most visible bird species",
            "selector": {"text": {}},
        },
        "scientific_name": {
            "description": "Scientific (Latin binomial) name of that species",
            "selector": {"text": {}},
        },
        "confidence": {
            "description": "Confidence in the species",
            "selector": {"select": {"options": list(CONFIDENCES)}},
        },
        "count": {
            "description": "Number of birds visible at the same time",
            "selector": {"number": {"min": 1, "max": 50, "mode": "box"}},
        },
        "best_frame": {
            "description": "Number of the image showing the bird best, the first being 1",
            "selector": {"number": {"min": 1, "max": frames, "mode": "box"}},
        },
    }


def build_instructions(
    frames: int, language: str, country: str | None, alert_time: int, known: list[str]
) -> str:
    """Return the prompt for one clip."""
    month = dt_util.utc_from_timestamp(alert_time / 1000).strftime("%B")
    place = f" in {country}" if country else ""
    lines = [
        f"These {frames} images are ordered frames of one clip from a bird feeder "
        f"camera{place}, recorded in {month}.",
        "Tell whether at least one real bird is visible. People, other animals, "
        "shadows, the feeder and the scenery do not count.",
        "If so, identify the most visible bird species from its plumage, shape and "
        "size, taking into account which birds are plausible in that place and "
        "season.",
        f"Write the common name in the language with code '{language}', and give "
        "the scientific name.",
        "Set the confidence to low unless the species can really be told apart on "
        "these images. Do not guess.",
        "Set best_frame to the sharpest image showing the bird.",
    ]
    if known:
        lines.append(
            "Species already seen here, to spell identically if it is one of them "
            "(do not choose one only because it is listed): " + ", ".join(known) + "."
        )
    return "\n".join(lines)


def parse_response(event: dict, response: Any, frames: int) -> Identification:
    """Turn the AI answer into an Identification, whatever its quality."""
    data = response.get("data") if isinstance(response, Mapping) else None
    if not isinstance(data, Mapping) or not isinstance(data.get("bird_visible"), bool):
        raise AnalysisError(f"Unexpected AI answer: {response}")

    def number(key: str, default: int, maximum: int) -> int:
        try:
            return min(max(int(data.get(key, default)), 1), maximum)
        except (TypeError, ValueError):
            return default

    confidence = data.get("confidence")
    return Identification(
        alarm_id=event["alarm_id"],
        alert_time=event["alert_time"],
        bird_visible=data["bird_visible"],
        species=str(data.get("species") or "").strip(),
        scientific_name=str(data.get("scientific_name") or "").strip(),
        confidence=confidence if confidence in CONFIDENCES else "low",
        count=number("count", 1, 50),
        best_frame=number("best_frame", 1, frames),
    )


class BirdfyIdentifier:
    """Sends new clips to the AI, one at a time, and records the results."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BirdfyConfigEntry,
        coordinator: BirdfyCoordinator,
        store: SpeciesStore,
    ) -> None:
        self.hass = hass
        self.store = store
        self._entry = entry
        self._coordinator = coordinator
        self._ai_entity: str = entry.options[CONF_AI_TASK_ENTITY]
        self._max_daily: int = entry.options.get(
            CONF_MAX_DAILY_ANALYSES, DEFAULT_MAX_DAILY_ANALYSES
        )
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._busy: set[str] = set()
        self._failures: dict[str, int] = {}
        self._frames: dict[str, list[Path]] = {}
        self._backfilling = False
        self.image_path = Path(
            hass.config.path(STORAGE_DIR, DOMAIN, f"{entry.entry_id}_last_bird.jpg")
        )

    @property
    def signal(self) -> str:
        return SIGNAL_BIRD_EVENT.format(self._entry.entry_id)

    @callback
    def async_start(self) -> None:
        """Start watching the coordinator. Stops with the config entry."""
        entry = self._entry
        entry.async_create_background_task(
            self.hass, self._async_worker(), f"{DOMAIN}_identifier"
        )
        entry.async_on_unload(
            self._coordinator.async_add_listener(self._async_coordinator_updated)
        )
        entry.async_on_unload(
            async_track_time_change(
                self.hass, self._async_new_day, hour=0, minute=0, second=0
            )
        )
        self._async_coordinator_updated()

    @callback
    def _async_new_day(self, _now: Any) -> None:
        self.store.async_roll_day()

    def frame_path(self, frame_id: str) -> Path | None:
        """Return the path of a frame sent to the AI, None if it does not exist."""
        token, _, index = frame_id.partition("/")
        frames = self._frames.get(token, [])
        if index.isdecimal() and int(index) < len(frames):
            return frames[int(index)]
        return None

    def _eligible(self, event: dict) -> bool:
        alarm_id = event.get("alarm_id")
        return bool(alarm_id) and alarm_id not in self._busy and (
            alarm_id not in self.store.analyzed
        )

    @callback
    def _async_coordinator_updated(self) -> None:
        """Queue the clips not analyzed yet, oldest first."""
        ai_state = self.hass.states.get(self._ai_entity)
        # The AI provider can load after us at startup, this is not an issue
        if ai_state is None and self.hass.is_running:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_ENTITY_MISSING,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_ENTITY_MISSING,
                translation_placeholders={"entity_id": self._ai_entity},
            )
        if ai_state is None or ai_state.state == STATE_UNAVAILABLE:
            return
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_ENTITY_MISSING)

        self.store.async_roll_day()
        budget = self._max_daily - self.store.today["analyses"] - self._queue.qsize()
        events = (self._coordinator.data or {}).get("recent_events", [])
        for event in sorted(events, key=lambda e: e.get("alert_time", 0)):
            if not self._eligible(event):
                continue
            if event.get("alert_time", 0) < self.store.activated_at:
                continue
            if budget <= 0:
                _LOGGER.debug("Daily analysis limit reached")
                break
            budget -= 1
            self._busy.add(event["alarm_id"])
            self._queue.put_nowait(event)

    async def _async_worker(self) -> None:
        while True:
            event = await self._queue.get()
            age = time.time() - event["alert_time"] / 1000
            try:
                if age < CLIP_AVAILABLE_DELAY:
                    await asyncio.sleep(CLIP_AVAILABLE_DELAY - age)
                self.store.async_count_analysis()
                await self._async_process(event, live=True)
            finally:
                self._busy.discard(event["alarm_id"])

    async def _async_process(self, event: dict, live: bool) -> bool:
        """Analyze a clip and record the result. Return False on failure."""
        alarm_id = event["alarm_id"]
        try:
            event_type, result = await self._async_analyze(event)
        except Exception as err:  # noqa: BLE001 - the worker must survive any failure
            attempts = self._failures.get(alarm_id, 0) + 1
            self._failures[alarm_id] = attempts
            _LOGGER.warning(
                "Analysis of %s failed (attempt %d/%d): %r",
                alarm_id, attempts, MAX_ATTEMPTS, err,
            )
            if attempts >= MAX_ATTEMPTS:
                del self._failures[alarm_id]
                self.store.async_mark_analyzed(alarm_id, STATUS_FAILED)
            return False

        self._failures.pop(alarm_id, None)
        if live and event_type:
            async_dispatcher_send(self.hass, self.signal, event_type, result)
        return True

    async def _async_analyze(self, event: dict) -> tuple[str | None, Identification]:
        source = await async_resolve_source(self._coordinator, event["alarm_id"])
        if not source:
            raise AnalysisError("No clip available")

        token = uuid.uuid4().hex
        frames_dir = await self.hass.async_add_executor_job(
            tempfile.mkdtemp, None, "birdfy_"
        )
        try:
            frames, stderr = await async_extract_frames(
                self.hass, source, frames_dir, MAX_FRAMES
            )
            if not frames:
                raise AnalysisError(f"ffmpeg extracted no frame: {stderr[-500:]}")

            self._frames[token] = frames
            response = await self._async_ask_ai(event, token, len(frames))
            result = parse_response(event, response, len(frames))

            last = self.store.last
            if result.identified and (
                last is None or result.alert_time >= last["alert_time"]
            ):
                await self.hass.async_add_executor_job(
                    self._save_image, frames[result.best_frame - 1]
                )
            return self.store.async_record(result), result
        finally:
            self._frames.pop(token, None)
            await self.hass.async_add_executor_job(shutil.rmtree, frames_dir, True)

    async def _async_ask_ai(self, event: dict, token: str, frames: int) -> Any:
        async with asyncio.timeout(ANALYSIS_TIMEOUT):
            return await self.hass.services.async_call(
                "ai_task",
                "generate_data",
                {
                    "task_name": "birdfy_identification",
                    "entity_id": self._ai_entity,
                    "instructions": build_instructions(
                        frames,
                        self.hass.config.language,
                        self.hass.config.country,
                        event["alert_time"],
                        self.store.known_names(),
                    ),
                    "structure": build_structure(frames),
                    "attachments": [
                        {
                            "media_content_id": f"media-source://{DOMAIN}/frame:{token}/{i}",
                            "media_content_type": "image/jpeg",
                        }
                        for i in range(frames)
                    ],
                },
                blocking=True,
                return_response=True,
            )

    def _save_image(self, frame: Path) -> None:
        self.image_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(frame, self.image_path)

    @callback
    def async_start_backfill(self, days: int) -> None:
        """Analyze past clips in the background."""
        if self._backfilling:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="backfill_running"
            )
        self._backfilling = True
        self._entry.async_create_background_task(
            self.hass, self._async_backfill(days), f"{DOMAIN}_backfill"
        )

    async def _async_backfill(self, days: int) -> None:
        today = dt_util.now().date()
        events: list[dict] = []
        try:
            for i in range(days):
                day = today - timedelta(days=i)
                events += await self._coordinator.fetch_events_for_day(day.isoformat())
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Backfill could not list the events: %s", err)
            self._backfilling = False
            return

        done = 0
        failed = 0
        try:
            for event in sorted(events, key=lambda e: e.get("alert_time", 0)):
                alarm_id = event.get("alarm_id")
                if (
                    not alarm_id
                    or alarm_id in self._busy
                    or self.store.analyzed.get(alarm_id) == STATUS_DONE
                    or time.time() - event["alert_time"] / 1000 < CLIP_AVAILABLE_DELAY
                ):
                    continue
                self._busy.add(alarm_id)
                try:
                    if await self._async_process(event, live=False):
                        done += 1
                    else:
                        failed += 1
                finally:
                    self._busy.discard(alarm_id)
        finally:
            self._backfilling = False
        _LOGGER.info("Birdfy backfill: %d analyzed, %d failed", done, failed)
