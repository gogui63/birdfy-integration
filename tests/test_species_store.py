"""Tests of the species list."""
from __future__ import annotations

import time

from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.birdfy.const import (
    EVENT_KNOWN_SPECIES,
    EVENT_NEW_SPECIES,
    EVENT_UNIDENTIFIED_BIRD,
)
from custom_components.birdfy.species_store import (
    Identification,
    SpeciesStore,
    species_key,
)


def tit(alarm_id: str = "a", alert_time: int | None = None, **kwargs) -> Identification:
    values = {
        "alarm_id": alarm_id,
        "alert_time": alert_time or int(time.time() * 1000),
        "bird_visible": True,
        "species": "Mésange charbonnière",
        "scientific_name": "Parus major",
        "confidence": "high",
    } | kwargs
    return Identification(**values)


async def store(hass: HomeAssistant) -> SpeciesStore:
    species_store = SpeciesStore(hass, "test")
    await species_store.async_load()
    return species_store


def test_species_key_ignores_case_accents_and_spaces() -> None:
    assert species_key("Mésange  Charbonnière") == species_key("mesange charbonniere")
    assert species_key("Great tit", "Parus major") == species_key("Mésange", "PARUS MAJOR")


async def test_new_then_known_species(hass: HomeAssistant) -> None:
    s = await store(hass)
    assert s.async_record(tit("a")) == EVENT_NEW_SPECIES
    assert s.last_is_new
    assert s.async_record(tit("b", species="mésange charbonniere")) == EVENT_KNOWN_SPECIES
    assert not s.last_is_new
    assert len(s.species) == 1
    assert s.species_list()[0]["count"] == 2
    assert s.today["visits"] == 2


async def test_no_bird_is_only_marked_analyzed(hass: HomeAssistant) -> None:
    s = await store(hass)
    assert s.async_record(tit(bird_visible=False)) is None
    assert s.analyzed == {"a": "done"}
    assert s.today["visits"] == 0
    assert not s.species


async def test_low_confidence_counts_a_visit_only(hass: HomeAssistant) -> None:
    s = await store(hass)
    assert s.async_record(tit(confidence="low")) == EVENT_UNIDENTIFIED_BIRD
    assert s.today["visits"] == 1
    assert not s.species
    assert s.last is None


async def test_bird_without_species_is_unidentified(hass: HomeAssistant) -> None:
    s = await store(hass)
    assert s.async_record(tit(species="", scientific_name="")) == EVENT_UNIDENTIFIED_BIRD


async def test_older_clip_does_not_replace_last(hass: HomeAssistant) -> None:
    s = await store(hass)
    now = int(time.time() * 1000)
    s.async_record(tit("recent", now))
    s.async_record(
        tit("old", now - 3_600_000, species="Rouge-gorge", scientific_name="Erithacus rubecula")
    )
    assert s.last["alarm_id"] == "recent"
    assert not s.last_is_new or s.last["species"] != "Rouge-gorge"
    assert len(s.species) == 2


async def test_older_clip_updates_first_seen(hass: HomeAssistant) -> None:
    s = await store(hass)
    now = int(time.time() * 1000)
    s.async_record(tit("recent", now))
    first_seen = s.species_list()[0]["first_seen"]
    s.async_record(tit("old", now - 86_400_000))
    assert s.species_list()[0]["first_seen"] < first_seen


async def test_day_rollover_resets_counters_only(hass: HomeAssistant) -> None:
    with freeze_time("2026-10-30 10:00:00"):
        s = await store(hass)
        s.async_record(tit())
        s.async_count_analysis()
    with freeze_time("2026-10-31 10:00:00"):
        s.async_roll_day()
    assert s.today["visits"] == 0
    assert s.today["analyses"] == 0
    assert s.species_today() == []
    assert len(s.species) == 1


async def test_clip_of_another_day_is_not_counted_today(hass: HomeAssistant) -> None:
    s = await store(hass)
    s.async_record(tit(alert_time=int((time.time() - 3 * 86400) * 1000)))
    assert s.today["visits"] == 0
    assert s.species_today() == []
    assert len(s.species) == 1


async def test_remove_species_by_either_name(hass: HomeAssistant) -> None:
    s = await store(hass)
    s.async_record(tit())
    assert not s.async_remove_species("Moineau")
    assert s.async_remove_species("mesange charbonniere")
    assert not s.species
    assert s.last is None
    assert s.species_today() == []

    s.async_record(tit("b"))
    assert s.async_remove_species("Parus major")


async def test_survives_a_restart(hass: HomeAssistant, hass_storage) -> None:
    s = await store(hass)
    s.async_record(tit())
    hass_storage["birdfy.test"] = {
        "version": 1,
        "minor_version": 1,
        "key": "birdfy.test",
        "data": s._data_to_save(),
    }
    reloaded = await store(hass)
    assert reloaded.species == s.species
    assert reloaded.last == s.last
    assert reloaded.analyzed == s.analyzed
    assert reloaded.activated_at == s.activated_at


async def test_analyzed_is_bounded(hass: HomeAssistant) -> None:
    s = await store(hass)
    with_limit = 2000
    for i in range(with_limit + 5):
        s.async_mark_analyzed(str(i), "done")
    assert len(s.analyzed) == with_limit
    assert "0" not in s.analyzed
