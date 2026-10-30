"""Fixtures for the Birdfy tests."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.ai_task import (
    AITaskEntity,
    AITaskEntityFeature,
    GenDataTask,
    GenDataTaskResult,
)
from homeassistant.components.ai_task.const import DATA_COMPONENT
from homeassistant.components.conversation import ChatLog
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.birdfy.const import CONF_AI_TASK_ENTITY, DOMAIN
from custom_components.birdfy.coordinator import BirdfyCoordinator

DEVICE_ID = "4473555153700840"
AI_ENTITY = "ai_task.fake"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def make_event(alarm_id: str, age: int = 600) -> dict[str, Any]:
    return {
        "alarm_id": alarm_id,
        "alert_time": int((time.time() - age) * 1000),
        "label": "unknown",
        "record_url": "",
        "record_dir": "",
        "pic": "",
    }


class FakeAITask(AITaskEntity):
    """Real AI Task entity, answering from a list."""

    _attr_name = "Fake"
    _attr_supported_features = (
        AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS
    )

    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.tasks: list[GenDataTask] = []
        self.frames_existed: list[bool] = []

    async def _async_generate_data(
        self, task: GenDataTask, chat_log: ChatLog
    ) -> GenDataTaskResult:
        self.tasks.append(task)
        self.frames_existed.append(all(a.path.exists() for a in task.attachments))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return GenDataTaskResult(conversation_id=chat_log.conversation_id, data=answer)


@pytest.fixture
async def ai_task(hass: HomeAssistant) -> FakeAITask:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "ai_task", {})
    await hass.async_block_till_done()
    entity = FakeAITask()
    await hass.data[DATA_COMPONENT].async_add_entities([entity])
    assert hass.states.get(AI_ENTITY)
    return entity


@pytest.fixture
def cloud_events() -> list[dict[str, Any]]:
    """Events returned by the fake Netvue cloud, most recent first."""
    return []


@pytest.fixture(autouse=True)
def fake_cloud(request: pytest.FixtureRequest, cloud_events: list[dict[str, Any]]):
    if request.node.get_closest_marker("real_cloud"):
        yield
        return

    async def update(self: BirdfyCoordinator) -> dict[str, Any]:
        self._device_id = DEVICE_ID
        return {
            "last_event": cloud_events[0] if cloud_events else {},
            "recent_events": list(cloud_events),
            "device_id": DEVICE_ID,
        }

    async def for_day(self: BirdfyCoordinator, date_str: str) -> list[dict[str, Any]]:
        return list(cloud_events) if date_str == dt_util.now().date().isoformat() else []

    with patch.object(BirdfyCoordinator, "_async_update_data", update), patch.object(
        BirdfyCoordinator, "fetch_events_for_day", for_day
    ):
        yield


@pytest.fixture(autouse=True)
def fake_clip():
    """Replace the cloud clip and ffmpeg by small files."""

    async def resolve(coordinator: BirdfyCoordinator, alarm_id: str) -> str:
        return "#EXTM3U"

    async def extract(hass, source, dest_dir, max_frames):
        frames = [Path(dest_dir) / f"frame_{i}.jpg" for i in range(1, 4)]
        for i, frame in enumerate(frames):
            frame.write_bytes(bytes([i]) * 4)
        return frames, ""

    with patch(
        "custom_components.birdfy.identification.async_resolve_source", resolve
    ), patch("custom_components.birdfy.identification.async_extract_frames", extract):
        yield


@pytest.fixture(autouse=True)
def no_clip_delay():
    with patch("custom_components.birdfy.identification.CLIP_AVAILABLE_DELAY", 0):
        yield


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id="user@example.com",
        data={
            "email": "user@example.com",
            "password": "secret",
            "ucid": "ucid",
            "udid": "udid",
        },
    )


@pytest.fixture
def ai_entry(entry: MockConfigEntry) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=entry.title,
        unique_id=entry.unique_id,
        data=entry.data,
        options={CONF_AI_TASK_ENTITY: AI_ENTITY},
    )


@pytest.fixture
def setup_entry(hass: HomeAssistant) -> Callable:
    async def _setup(config_entry: MockConfigEntry) -> None:
        config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    return _setup
