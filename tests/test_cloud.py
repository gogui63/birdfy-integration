"""Tests of the Netvue polling, on a mocked HTTP session."""
from __future__ import annotations

import json

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.birdfy.coordinator import API_BASE, LOGIN_URL
from custom_components.birdfy.http import async_resolve_source

from .conftest import DEVICE_ID

pytestmark = pytest.mark.real_cloud

DEVICES_URL = "https://localweb.nvts.co/v1/devices/v3"
EVENTS_URL = f"{API_BASE}/devices/{DEVICE_ID}/events"
DYNAMIC_URL = "https://api2.nvts.co/users/dynamicSetting"
EVENT = {
    "alarmId": "20261030190545",
    "alertTime": 1793387145395,
    "label": "unknown",
    "pic": "pic",
    "description": json.dumps({"recordUrl": "", "recordDir": "dir"}),
}


def mock_cloud(aioclient_mock: AiohttpClientMocker, events_status: int = 200) -> None:
    aioclient_mock.post(LOGIN_URL, json={"ret": 0, "token": "token", "userID": 1})
    aioclient_mock.get(DEVICES_URL, json={"devices": [{"serialNumber": DEVICE_ID}]})
    aioclient_mock.get(EVENTS_URL, status=events_status, json={"events": [EVENT]})
    aioclient_mock.get(f"{EVENTS_URL}/{EVENT['alarmId']}/pic", json={"url": "https://img"})
    aioclient_mock.get(
        DYNAMIC_URL, json={"highlightsUrl": "https://high", "recapUrl": "https://recap"}
    )


async def test_polling(hass: HomeAssistant, entry, aioclient_mock) -> None:
    mock_cloud(aioclient_mock)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.birdfy_last_event")
    assert state.state == "unknown"
    assert state.attributes["alarm_id"] == EVENT["alarmId"]
    assert state.attributes["alert_time"] == EVENT["alertTime"]
    assert state.attributes["alert_time_iso"] == "2026-10-30T12:05:45"
    assert state.attributes["image_url"] == "https://img"
    assert state.attributes["device_id"] == DEVICE_ID
    assert state.attributes["recent_events"][0]["record_dir"] == "dir"
    assert hass.states.get("sensor.birdfy_highlights").state == "https://high"
    assert hass.states.get("sensor.birdfy_recap").state == "https://recap"


async def test_rejected_password_starts_reauth(hass, entry, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, json={"ret": 1, "msg": "bad password"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_network_error_retries_setup(hass, entry, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, exc=aiohttp.ClientError())
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_expired_token_is_dropped(hass, entry, aioclient_mock) -> None:
    mock_cloud(aioclient_mock, events_status=401)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_segments_come_from_download_link(hass, entry, aioclient_mock) -> None:
    mock_cloud(aioclient_mock)
    aioclient_mock.post(
        "https://localapi2.nvts.co/event/downloadLink",
        json={
            "eventList": [
                {
                    "alarmId": EVENT["alarmId"],
                    "videoFileList": ["https://a/slice_1_0_0.ts", "https://a/slice_1_1_0.ts"],
                    "picList": ["https://thumb"],
                }
            ]
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    source = await async_resolve_source(coordinator, EVENT["alarmId"])
    assert source.startswith("#EXTM3U")
    assert source.count("#EXTINF") == 2
    assert coordinator.thumbnail_cache[EVENT["alarmId"]] == "https://thumb"

    events = await coordinator.fetch_events_for_day("2026-10-30")
    assert [e["alarm_id"] for e in events] == [EVENT["alarmId"]]


async def test_day_follows_the_home_assistant_time_zone(
    hass: HomeAssistant, entry, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_cloud(aioclient_mock)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    aioclient_mock.clear_requests()
    aioclient_mock.get(EVENTS_URL, json={"events": []})
    await entry.runtime_data.fetch_events_for_day("2026-10-30")

    params = aioclient_mock.mock_calls[0][1].query
    # Midnight to 23:59:59 in US/Pacific (UTC-7), whatever the time zone of the system
    assert int(params["startTime"]) == 1793343600000
    assert int(params["endTime"]) == 1793429999999


async def test_segments_of_a_clip_on_the_previous_local_day(
    hass: HomeAssistant, entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """In US/Pacific, this UTC alarm_id belongs to the day before its date."""
    alarm_id = "20261031020000"
    aioclient_mock.get(
        EVENTS_URL,
        params={"startTime": 1793343600000},
        json={"events": [EVENT | {"alarmId": alarm_id}]},
    )
    mock_cloud(aioclient_mock)
    aioclient_mock.post(
        "https://localapi2.nvts.co/event/downloadLink",
        json={"eventList": [{"alarmId": alarm_id, "videoFileList": ["https://a/slice_1_0_0.ts"]}]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    aioclient_mock.mock_calls.clear()
    assert await entry.runtime_data.fetch_segments(alarm_id) == ["https://a/slice_1_0_0.ts"]
    lists = [call for call in aioclient_mock.mock_calls if call[1].path.endswith("/events")]
    assert len(lists) == 1


@pytest.mark.parametrize("alarm_id", ["abc", "00010101000000", "99991231235959"])
async def test_segments_of_an_impossible_clip(
    hass: HomeAssistant, entry, aioclient_mock: AiohttpClientMocker, alarm_id
) -> None:
    """The mp4 view hands over any id."""
    mock_cloud(aioclient_mock)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await entry.runtime_data.fetch_segments(alarm_id) == []


async def test_refused_token_is_renewed_for_a_day(
    hass: HomeAssistant, entry, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_cloud(aioclient_mock)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    statuses = [401, 200]

    async def answer(method, url, data):
        return AiohttpClientMockResponse(
            method, url, status=statuses.pop(0), json={"events": [EVENT]}
        )

    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, json={"ret": 0, "token": "new", "userID": 1})
    aioclient_mock.post("https://localapi2.nvts.co/event/downloadLink", json={})
    aioclient_mock.get(EVENTS_URL, side_effect=answer)

    events = await entry.runtime_data.fetch_events_for_day("2026-10-30")
    assert [e["alarm_id"] for e in events] == [EVENT["alarmId"]]
    assert entry.runtime_data._token == "new"


async def test_refused_token_is_renewed_for_the_download_link(
    hass: HomeAssistant, entry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The mp4 view of a known clip goes straight to this request."""
    mock_cloud(aioclient_mock)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    statuses = [401, 200]

    async def answer(method, url, data):
        return AiohttpClientMockResponse(
            method,
            url,
            status=statuses.pop(0),
            json={"eventList": [{"alarmId": EVENT["alarmId"], "videoFileList": ["https://a"]}]},
        )

    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, json={"ret": 0, "token": "new", "userID": 1})
    aioclient_mock.post("https://localapi2.nvts.co/event/downloadLink", side_effect=answer)

    assert await entry.runtime_data.fetch_segments(EVENT["alarmId"]) == ["https://a"]


async def test_poll_recovers_from_a_refused_token(
    hass: HomeAssistant, entry, aioclient_mock: AiohttpClientMocker
) -> None:
    statuses = [401, 200]

    async def answer(method, url, data):
        return AiohttpClientMockResponse(
            method, url, status=statuses.pop(0), json={"events": [EVENT]}
        )

    aioclient_mock.post(LOGIN_URL, json={"ret": 0, "token": "token", "userID": 1})
    aioclient_mock.get(DEVICES_URL, json={"devices": [{"serialNumber": DEVICE_ID}]})
    aioclient_mock.get(EVENTS_URL, side_effect=answer)
    aioclient_mock.get(f"{EVENTS_URL}/{EVENT['alarmId']}/pic", json={"url": "https://img"})
    aioclient_mock.get(DYNAMIC_URL, json={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
