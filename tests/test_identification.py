"""Tests of the identification, through the real ai_task component."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import time
from unittest.mock import patch

import pytest
from homeassistant.components.ai_task.services import _validate_structure_fields
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.birdfy.const import CONF_AI_TASK_ENTITY, DOMAIN
from custom_components.birdfy.identification import (
    AnalysisError,
    build_instructions,
    build_structure,
    parse_response,
)
from custom_components.birdfy.species_store import SpeciesStore

from .conftest import AI_ENTITY, make_event

TIT = {
    "bird_visible": True,
    "species": "Mésange charbonnière",
    "scientific_name": "Parus major",
    "confidence": "high",
    "count": 2,
    "best_frame": 2,
}
ROBIN = TIT | {"species": "Rouge-gorge", "scientific_name": "Erithacus rubecula"}
NO_BIRD = {"bird_visible": False}

AI_ENTITIES = [
    "sensor.birdfy_last_species",
    "sensor.birdfy_species_count",
    "sensor.birdfy_species_today",
    "sensor.birdfy_visits_today",
    "sensor.birdfy_top_species",
    "binary_sensor.birdfy_new_species",
    "event.birdfy_bird_detected",
    "image.birdfy_last_bird",
]


async def poll(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6), fire_all=True)
    await hass.async_block_till_done(wait_background_tasks=False)
    await drain(hass)


async def drain(hass: HomeAssistant) -> None:
    for _ in range(20):
        await hass.async_block_till_done(wait_background_tasks=False)
        await asyncio.sleep(0)


@pytest.fixture
def seeded_store(hass_storage, ai_entry):
    """Activate the AI one hour ago, so that recent events are analyzed."""
    key = f"birdfy.{ai_entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "activated_at": int((time.time() - 3600) * 1000),
            "analyzed": {},
            "species": {},
            "today": {
                "date": dt_util.now().date().isoformat(),
                "visits": 0,
                "species": {},
                "analyses": 0,
            },
            "last": None,
            "last_is_new": False,
        },
    }


def test_structure_is_valid_for_the_service() -> None:
    assert _validate_structure_fields(build_structure(1))
    assert _validate_structure_fields(build_structure(6))


def test_prompt_contains_the_context() -> None:
    text = build_instructions(3, "fr", "FR", 1793387145395, ["Rouge-gorge (Erithacus rubecula)"])
    assert "in FR" in text
    assert "'fr'" in text
    assert "October" in text
    assert "Erithacus rubecula" in text
    assert "already seen" not in build_instructions(3, "fr", None, 1793387145395, [])


@pytest.mark.parametrize("response", [None, {}, {"data": "text"}, {"data": {"species": "x"}}])
def test_parse_refuses_a_broken_answer(response) -> None:
    with pytest.raises(AnalysisError):
        parse_response(make_event("a"), response, 3)


def test_parse_clamps_and_defaults() -> None:
    result = parse_response(
        make_event("a"),
        {"data": {"bird_visible": True, "species": " Pie ", "confidence": "sure",
                  "count": "many", "best_frame": 99.0}},
        3,
    )
    assert result.species == "Pie"
    assert result.confidence == "low"
    assert result.count == 1
    assert result.best_frame == 3


async def test_no_ai_entities_without_option(hass, entry, setup_entry) -> None:
    await setup_entry(entry)
    assert hass.states.get("sensor.birdfy_last_event") is not None
    for entity_id in AI_ENTITIES:
        assert hass.states.get(entity_id) is None


async def test_existing_entities_keep_their_ids_and_names(hass, entry, setup_entry) -> None:
    await setup_entry(entry)
    assert hass.states.get("sensor.birdfy_last_event").name == "Birdfy Last Event"
    assert hass.states.get("sensor.birdfy_highlights").name == "Birdfy Highlights"
    assert hass.states.get("sensor.birdfy_recap").name == "Birdfy Recap"
    assert hass.states.get("camera.birdfy_thumbnail").name == "Birdfy Thumbnail"


async def test_ai_entities_are_created(hass, ai_task, ai_entry, setup_entry) -> None:
    await setup_entry(ai_entry)
    for entity_id in AI_ENTITIES:
        assert hass.states.get(entity_id) is not None, entity_id


@pytest.mark.usefixtures("seeded_store")
async def test_new_clip_goes_through_ai_task(
    hass, ai_task, ai_entry, setup_entry, cloud_events
) -> None:
    cloud_events.append(make_event("a"))
    ai_task.answers = [TIT]
    await setup_entry(ai_entry)
    await drain(hass)

    assert len(ai_task.tasks) == 1
    task = ai_task.tasks[0]
    assert len(task.attachments) == 3
    assert ai_task.frames_existed == [True]
    assert all(a.mime_type == "image/jpeg" for a in task.attachments)

    assert hass.states.get("sensor.birdfy_last_species").state == "Mésange charbonnière"
    attributes = hass.states.get("sensor.birdfy_last_species").attributes
    assert attributes["scientific_name"] == "Parus major"
    assert attributes["confidence"] == "high"
    assert attributes["count"] == 2
    assert attributes["alarm_id"] == "a"
    count = hass.states.get("sensor.birdfy_species_count")
    assert count.state == "1"
    assert count.attributes["species"][0]["name"] == "Mésange charbonnière"
    assert hass.states.get("sensor.birdfy_species_today").state == "1"
    assert hass.states.get("sensor.birdfy_visits_today").state == "1"
    assert hass.states.get("sensor.birdfy_top_species").state == "Mésange charbonnière"
    assert hass.states.get("binary_sensor.birdfy_new_species").state == "on"
    event = hass.states.get("event.birdfy_bird_detected")
    assert event.attributes["event_type"] == "new_species"
    assert event.attributes["species"] == "Mésange charbonnière"
    assert event.attributes["alarm_id"] == "a"

    # The second frame is the one the AI chose
    image_path = ai_entry.runtime_data.identifier.image_path
    assert image_path.read_bytes() == bytes([1]) * 4
    assert not task.attachments[0].path.exists()


@pytest.mark.usefixtures("seeded_store")
async def test_each_clip_is_analyzed_once(
    hass, ai_task, ai_entry, setup_entry, cloud_events
) -> None:
    cloud_events.append(make_event("a"))
    ai_task.answers = [TIT, ROBIN]
    await setup_entry(ai_entry)
    await drain(hass)
    await poll(hass)
    assert len(ai_task.tasks) == 1

    cloud_events.insert(0, make_event("b", age=300))
    await poll(hass)
    assert len(ai_task.tasks) == 2
    assert hass.states.get("sensor.birdfy_species_count").state == "2"
    assert "Mésange charbonnière" in ai_task.tasks[1].instructions


@pytest.mark.usefixtures("seeded_store")
async def test_no_bird_changes_nothing(
    hass, ai_task, ai_entry, setup_entry, cloud_events
) -> None:
    cloud_events.append(make_event("a"))
    ai_task.answers = [NO_BIRD]
    await setup_entry(ai_entry)
    await drain(hass)
    assert hass.states.get("sensor.birdfy_visits_today").state == "0"
    assert hass.states.get("event.birdfy_bird_detected").state == "unknown"
    assert ai_entry.runtime_data.identifier.store.analyzed == {"a": "done"}


async def test_events_before_activation_are_ignored(
    hass, ai_task, ai_entry, setup_entry, cloud_events
) -> None:
    cloud_events.append(make_event("old"))
    await setup_entry(ai_entry)
    await drain(hass)
    assert ai_task.tasks == []


@pytest.mark.usefixtures("seeded_store")
async def test_failures_are_retried_then_given_up(
    hass, ai_task, ai_entry, setup_entry, cloud_events
) -> None:
    cloud_events.append(make_event("a"))
    ai_task.answers = [HomeAssistantError("quota")] * 3
    await setup_entry(ai_entry)
    await drain(hass)
    identifier = ai_entry.runtime_data.identifier
    assert identifier.store.analyzed == {}

    await poll(hass)
    await poll(hass)
    assert len(ai_task.tasks) == 3
    assert identifier.store.analyzed == {"a": "failed"}

    await poll(hass)
    assert len(ai_task.tasks) == 3
    assert hass.states.get("sensor.birdfy_species_count").state == "0"


@pytest.mark.usefixtures("seeded_store")
async def test_worker_survives_a_broken_answer(
    hass, ai_task, ai_entry, setup_entry, cloud_events
) -> None:
    cloud_events.append(make_event("a"))
    ai_task.answers = ["free text", TIT]
    await setup_entry(ai_entry)
    await drain(hass)
    await poll(hass)
    assert hass.states.get("sensor.birdfy_species_count").state == "1"


@pytest.mark.usefixtures("seeded_store")
async def test_daily_limit(hass, ai_task, ai_entry, setup_entry, cloud_events) -> None:
    ai_entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ai_entry.entry_id,
        title=ai_entry.title,
        unique_id=ai_entry.unique_id,
        data=ai_entry.data,
        options={CONF_AI_TASK_ENTITY: AI_ENTITY, "max_daily_analyses": 2},
    )
    cloud_events.extend(make_event(i, age=600) for i in "abc")
    ai_task.answers = [TIT, TIT]
    await setup_entry(ai_entry)
    await drain(hass)
    await poll(hass)
    assert len(ai_task.tasks) == 2


async def test_missing_ai_entity_raises_a_repair(hass, ai_entry, setup_entry) -> None:
    await setup_entry(ai_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_entity_missing")


async def test_no_repair_while_home_assistant_starts(hass, ai_entry, setup_entry) -> None:
    hass.set_state(CoreState.not_running)
    await setup_entry(ai_entry)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_entity_missing")


@pytest.mark.usefixtures("seeded_store")
async def test_unavailable_ai_entity_spends_no_attempt(
    hass, ai_task, ai_entry, setup_entry, cloud_events
) -> None:
    cloud_events.append(make_event("a"))
    hass.states.async_set(AI_ENTITY, "unavailable")
    await setup_entry(ai_entry)
    await drain(hass)
    assert ai_task.tasks == []
    assert not ir.async_get(hass).async_get_issue(DOMAIN, "ai_task_entity_missing")


@pytest.mark.usefixtures("seeded_store")
async def test_backfill(hass, ai_task, ai_entry, setup_entry, cloud_events) -> None:
    await setup_entry(ai_entry)
    cloud_events.extend(
        [make_event("recent", age=100), make_event("old", age=3000), make_event("oldest", age=5000)]
    )
    ai_entry.runtime_data.identifier.store.async_mark_analyzed("old", "done")
    ai_task.answers = [ROBIN, NO_BIRD]

    await hass.services.async_call(DOMAIN, "analyze_events", {"days": 1}, blocking=True)
    await drain(hass)

    # Oldest first, skipping what was analyzed
    assert len(ai_task.tasks) == 2
    assert hass.states.get("sensor.birdfy_last_species").state == "Rouge-gorge"
    # Backfill never fires the event
    assert hass.states.get("event.birdfy_bird_detected").state == "unknown"


async def test_analyze_needs_ai(hass, entry, setup_entry) -> None:
    await setup_entry(entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "analyze_events", {"days": 1}, blocking=True)


@pytest.mark.usefixtures("seeded_store")
async def test_remove_species_action(
    hass, ai_task, ai_entry, setup_entry, cloud_events
) -> None:
    cloud_events.append(make_event("a"))
    ai_task.answers = [TIT]
    await setup_entry(ai_entry)
    await drain(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "remove_species", {"species": "Moineau"}, blocking=True
        )
    await hass.services.async_call(
        DOMAIN, "remove_species", {"species": "Parus major"}, blocking=True
    )
    assert hass.states.get("sensor.birdfy_species_count").state == "0"
    assert hass.states.get("sensor.birdfy_last_species").state == "unknown"


@pytest.mark.usefixtures("seeded_store")
async def test_disabling_ai_removes_entities_and_keeps_data(
    hass, hass_storage, ai_task, ai_entry, setup_entry, cloud_events
) -> None:
    cloud_events.append(make_event("a"))
    ai_task.answers = [TIT]
    await setup_entry(ai_entry)
    await drain(hass)
    await ai_entry.runtime_data.identifier.store._store.async_save(
        ai_entry.runtime_data.identifier.store._data_to_save()
    )

    hass.config_entries.async_update_entry(ai_entry, options={})
    await hass.config_entries.async_reload(ai_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get("sensor.birdfy_last_species") is None
    assert registry.async_get("sensor.birdfy_last_event") is not None
    assert hass_storage[f"birdfy.{ai_entry.entry_id}"]["data"]["species"]


async def test_remove_entry_deletes_data(hass, hass_storage, ai_task, ai_entry, setup_entry) -> None:
    await setup_entry(ai_entry)
    store = ai_entry.runtime_data.identifier.store
    await store._store.async_save(store._data_to_save())
    assert f"birdfy.{ai_entry.entry_id}" in hass_storage
    await hass.config_entries.async_remove(ai_entry.entry_id)
    await hass.async_block_till_done()
    assert f"birdfy.{ai_entry.entry_id}" not in hass_storage


async def test_midnight_resets_the_counters(hass, ai_task, ai_entry, setup_entry) -> None:
    await setup_entry(ai_entry)
    store: SpeciesStore = ai_entry.runtime_data.identifier.store
    store.today["date"] = "2000-01-01"
    store.today["visits"] = 5
    with patch.object(store, "async_roll_day", wraps=store.async_roll_day) as roll:
        ai_entry.runtime_data.identifier._async_new_day(None)
    roll.assert_called_once()
    assert store.today["visits"] == 0
