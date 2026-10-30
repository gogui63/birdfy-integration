"""Persistent list of the species seen at the feeder."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
import unicodedata

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_KNOWN_SPECIES, EVENT_NEW_SPECIES, EVENT_UNIDENTIFIED_BIRD

STORAGE_VERSION = 1
SAVE_DELAY = 10
MAX_ANALYZED = 2000

STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass(slots=True)
class Identification:
    """Parsed answer of the AI for one clip."""

    alarm_id: str
    alert_time: int
    bird_visible: bool
    species: str = ""
    scientific_name: str = ""
    confidence: str = "low"
    count: int = 1
    best_frame: int = 1

    @property
    def identified(self) -> bool:
        return self.bird_visible and bool(self.species) and self.confidence != "low"


def species_key(species: str, scientific_name: str = "") -> str:
    """Return a stable key, so that spelling variants are the same species."""
    name = scientific_name or species
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(ascii_name.casefold().split())


def _iso(alert_time: int) -> str:
    return dt_util.utc_from_timestamp(alert_time / 1000).isoformat()


def _empty_today() -> dict[str, Any]:
    return {
        "date": dt_util.now().date().isoformat(),
        "visits": 0,
        "species": {},
        "analyses": 0,
    }


class SpeciesStore:
    """Species list, daily counters and last sighting, saved across restarts."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self.activated_at = 0
        self.analyzed: dict[str, str] = {}
        self.species: dict[str, dict[str, Any]] = {}
        self.today = _empty_today()
        self.last: dict[str, Any] | None = None
        self.last_is_new = False
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data is None:
            self.activated_at = int(dt_util.utcnow().timestamp() * 1000)
            self._async_save()
            return
        self.activated_at = data["activated_at"]
        self.analyzed = data["analyzed"]
        self.species = data["species"]
        self.today = data["today"]
        self.last = data["last"]
        self.last_is_new = data["last_is_new"]
        self.async_roll_day()

    async def async_remove(self) -> None:
        await self._store.async_remove()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def _async_changed(self) -> None:
        self._async_save()
        for listener in list(self._listeners):
            listener()

    @callback
    def _async_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "activated_at": self.activated_at,
            "analyzed": self.analyzed,
            "species": self.species,
            "today": self.today,
            "last": self.last,
            "last_is_new": self.last_is_new,
        }

    @callback
    def async_mark_analyzed(self, alarm_id: str, status: str) -> None:
        self.analyzed[alarm_id] = status
        # dicts keep insertion order, the oldest entries go first
        while len(self.analyzed) > MAX_ANALYZED:
            del self.analyzed[next(iter(self.analyzed))]
        self._async_save()

    @callback
    def async_roll_day(self) -> None:
        """Reset the daily counters when the local day has changed."""
        if self.today["date"] != dt_util.now().date().isoformat():
            self.today = _empty_today()
            self._async_changed()

    @callback
    def async_count_analysis(self) -> None:
        self.async_roll_day()
        self.today["analyses"] += 1
        self._async_save()

    @callback
    def async_record(self, result: Identification) -> str | None:
        """Add an identification and return its event type, None if no bird."""
        self.async_mark_analyzed(result.alarm_id, STATUS_DONE)
        if not result.bird_visible:
            return None

        self.async_roll_day()
        same_day = (
            dt_util.as_local(dt_util.utc_from_timestamp(result.alert_time / 1000))
            .date()
            .isoformat()
            == self.today["date"]
        )
        if same_day:
            self.today["visits"] += 1

        if not result.identified:
            self._async_changed()
            return EVENT_UNIDENTIFIED_BIRD

        key = species_key(result.species, result.scientific_name)
        seen_at = _iso(result.alert_time)
        known = self.species.get(key)
        is_new = known is None
        if known is None:
            self.species[key] = {
                "name": result.species,
                "scientific_name": result.scientific_name,
                "first_seen": seen_at,
                "last_seen": seen_at,
                "count": 1,
            }
        else:
            known["count"] += 1
            known["first_seen"] = min(known["first_seen"], seen_at)
            known["last_seen"] = max(known["last_seen"], seen_at)

        if same_day:
            self.today["species"][key] = result.species

        # Backfilled clips must not replace a more recent sighting
        if self.last is None or result.alert_time >= self.last["alert_time"]:
            self.last = {
                "species": result.species,
                "scientific_name": result.scientific_name,
                "confidence": result.confidence,
                "count": result.count,
                "alarm_id": result.alarm_id,
                "alert_time": result.alert_time,
                "seen_at": seen_at,
            }
            self.last_is_new = is_new

        self._async_changed()
        return EVENT_NEW_SPECIES if is_new else EVENT_KNOWN_SPECIES

    @callback
    def async_remove_species(self, species: str) -> bool:
        """Forget a species, by common or scientific name."""
        wanted = species_key(species)
        for key, entry in self.species.items():
            names = (key, species_key(entry["name"]))
            if wanted not in names:
                continue
            del self.species[key]
            self.today["species"].pop(key, None)
            if self.last and species_key(
                self.last["species"], self.last["scientific_name"]
            ) == key:
                self.last = None
                self.last_is_new = False
            self._async_changed()
            return True
        return False

    def species_list(self) -> list[dict[str, Any]]:
        """Return every species, most recently seen first."""
        return sorted(
            self.species.values(), key=lambda s: s["last_seen"], reverse=True
        )

    def species_today(self) -> list[str]:
        return list(self.today["species"].values())

    def top_species(self) -> dict[str, Any] | None:
        return max(self.species.values(), key=lambda s: s["count"], default=None)

    def known_names(self) -> list[str]:
        """Return the names to reuse in the prompt, for a consistent spelling."""
        return [
            f"{s['name']} ({s['scientific_name']})" if s["scientific_name"] else s["name"]
            for s in self.species.values()
        ]
