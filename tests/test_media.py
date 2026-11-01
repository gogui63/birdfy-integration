"""Tests of the clip URLs, the media source and the actions around them."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.components import media_source
from homeassistant.components.image import async_get_image
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.core import Context
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.birdfy.const import DOMAIN

from .conftest import make_event

RESOLVE_HTTP = "custom_components.birdfy.http.async_resolve_source"
PLAYLIST = "#EXTM3U\n#EXTINF:2.000,\nhttps://a/slice_1_0_0.ts\n#EXT-X-ENDLIST"
# Already November in UTC, still October in the US/Pacific zone of the tests
UTC_AHEAD_OF_LOCAL_DAY = "2026-11-01T05:40:00+00:00"


async def write_clip(hass, source, dest_path):
    Path(dest_path).write_bytes(b"mp4")
    return True, ""


async def test_mp4_view_needs_no_auth(hass, entry, setup_entry, hass_client_no_auth) -> None:
    """The Telegram automation fetches this URL."""
    await setup_entry(entry)
    client = await hass_client_no_auth()
    with patch(RESOLVE_HTTP, AsyncMock(return_value=PLAYLIST)), patch(
        "custom_components.birdfy.http.async_write_mp4", write_clip
    ):
        response = await client.get("/api/birdfy/mp4/abc")
    assert response.status == 200
    assert response.content_type == "video/mp4"
    assert await response.read() == b"mp4"


async def test_mp4_view_without_clip(hass, entry, setup_entry, hass_client_no_auth) -> None:
    await setup_entry(entry)
    client = await hass_client_no_auth()
    with patch(RESOLVE_HTTP, AsyncMock(return_value="")):
        assert (await client.get("/api/birdfy/mp4/abc")).status == 404


async def test_views_after_unload(hass, entry, setup_entry, hass_client_no_auth) -> None:
    await setup_entry(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    client = await hass_client_no_auth()
    assert (await client.get("/api/birdfy/mp4/abc")).status == 503


async def test_reload_keeps_the_views(hass, entry, setup_entry, hass_client_no_auth) -> None:
    """Views are registered once, so that the options can reload the entry."""
    await setup_entry(entry)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    with patch(RESOLVE_HTTP, AsyncMock(return_value=PLAYLIST)), patch(
        "custom_components.birdfy.http.async_write_mp4", write_clip
    ):
        response = await client.get("/api/birdfy/mp4/abc")
    assert response.status == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/birdfy/m3u8/abc",
        "/api/birdfy/segment/https%253A%252F%252Fnvs-eu-central-1-videomotion.s3%2540127.0.0.1%252Fx",
    ],
)
async def test_only_the_mp4_view_is_exposed(
    hass, entry, setup_entry, hass_client_no_auth, path
) -> None:
    await setup_entry(entry)
    client = await hass_client_no_auth()
    assert (await client.get(path)).status == 404


async def test_resolve_clip_url(hass, entry, setup_entry) -> None:
    await setup_entry(entry)
    media = await media_source.async_resolve_media(
        hass, "media-source://birdfy/20261030190545", None
    )
    assert media.url.startswith("/api/birdfy/mp4/20261030190545")
    assert media.mime_type == "video/mp4"


async def test_unknown_identifier_does_not_resolve(hass, entry, setup_entry) -> None:
    await setup_entry(entry)
    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, "media-source://birdfy/file:abc", None)


@pytest.mark.parametrize("frame", ["unknown/0", "../../etc/passwd/0", "x", ""])
async def test_only_running_frames_resolve(hass, ai_task, ai_entry, setup_entry, frame) -> None:
    await setup_entry(ai_entry)
    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(
            hass, f"media-source://birdfy/frame:{frame}", None
        )


async def test_frames_do_not_resolve_without_ai(hass, entry, setup_entry) -> None:
    await setup_entry(entry)
    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, "media-source://birdfy/frame:a/0", None)


async def test_browse_a_day(hass, entry, setup_entry, cloud_events, freezer) -> None:
    freezer.move_to(UTC_AHEAD_OF_LOCAL_DAY)
    await setup_entry(entry)
    cloud_events.append(make_event("abc") | {"record_url": "https://clip"})
    day = dt_util.now().date().isoformat()
    media = await media_source.async_browse_media(hass, f"media-source://birdfy/day:{day}")
    assert [child.identifier for child in media.children] == ["abc"]
    assert media.children[0].title.startswith("22:30:00")


async def test_browse_root_uses_the_home_assistant_day(hass, entry, setup_entry, freezer) -> None:
    freezer.move_to(UTC_AHEAD_OF_LOCAL_DAY)
    await setup_entry(entry)
    media = await media_source.async_browse_media(hass, "media-source://birdfy")
    assert media.children[0].identifier == "day:2026-10-31"


async def test_image_without_bird(hass, ai_task, ai_entry, setup_entry) -> None:
    await setup_entry(ai_entry)
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, "image.birdfy_last_bird")


async def test_image_of_the_last_bird(hass, ai_task, ai_entry, setup_entry) -> None:
    await setup_entry(ai_entry)
    identifier = ai_entry.runtime_data.identifier
    frame = Path(hass.config.path("frame.jpg"))
    frame.write_bytes(b"jpeg")
    await hass.async_add_executor_job(identifier._save_image, frame)
    identifier.store.last = {"seen_at": "2026-10-30T19:05:45+00:00"}
    image = await async_get_image(hass, "image.birdfy_last_bird")
    assert image.content == b"jpeg"
    assert image.content_type == "image/jpeg"


async def test_diagnostics_are_redacted(
    hass, hass_client, ai_task, ai_entry, setup_entry
) -> None:
    await setup_entry(ai_entry)
    assert await async_setup_component(hass, "diagnostics", {})
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, ai_entry)
    text = str(diagnostics)
    assert "secret" not in text
    assert "user@example.com" not in text
    assert diagnostics["identification"]["analyzed"] == 0


async def test_download_thumbnails(
    hass, entry, setup_entry, cloud_events, tmp_path, aioclient_mock, freezer
) -> None:
    freezer.move_to(UTC_AHEAD_OF_LOCAL_DAY)
    await setup_entry(entry)
    cloud_events.append(make_event("abc"))
    entry.runtime_data.thumbnail_cache["abc"] = "https://thumb/abc.jpg"
    aioclient_mock.get("https://thumb/abc.jpg", content=b"jpeg")
    for _ in range(2):
        await hass.services.async_call(
            DOMAIN, "download_thumbnails", {"path": str(tmp_path)}, blocking=True
        )
    files = list(tmp_path.rglob("*.jpg"))
    assert len(files) == 1
    assert files[0].name.startswith("223000_")
    assert files[0].read_bytes() == b"jpeg"
    assert aioclient_mock.call_count == 1


async def test_download_videos(hass, entry, setup_entry, cloud_events, tmp_path, freezer) -> None:
    freezer.move_to(UTC_AHEAD_OF_LOCAL_DAY)
    await setup_entry(entry)
    cloud_events.append(make_event("abc"))
    with patch(
        "custom_components.birdfy.services.async_resolve_source",
        AsyncMock(return_value=PLAYLIST),
    ), patch("custom_components.birdfy.services.async_write_mp4", write_clip):
        await hass.services.async_call(
            DOMAIN, "download_videos", {"path": str(tmp_path)}, blocking=True
        )
    files = list(tmp_path.rglob("*.mp4"))
    assert len(files) == 1
    assert files[0].name.startswith("223000_")
    assert files[0].read_bytes() == b"mp4"


async def test_saved_clip_is_found_whatever_its_name(
    hass, entry, setup_entry, cloud_events, tmp_path
) -> None:
    """The time and the label in the name can change, the alarm_id cannot."""
    await setup_entry(entry)
    cloud_events.append(make_event("abc"))
    day_dir = tmp_path / dt_util.now().date().isoformat()
    day_dir.mkdir()
    (day_dir / "000000_old_label_abc.mp4").write_bytes(b"mp4")
    resolve = AsyncMock(return_value=PLAYLIST)
    with patch("custom_components.birdfy.services.async_resolve_source", resolve):
        await hass.services.async_call(
            DOMAIN, "download_videos", {"path": str(tmp_path)}, blocking=True
        )
    resolve.assert_not_awaited()
    assert len(list(tmp_path.rglob("*.mp4"))) == 1


async def test_cut_copy_is_not_seen_as_saved(hass, entry, setup_entry, cloud_events, tmp_path) -> None:
    await setup_entry(entry)
    cloud_events.append(make_event("abc"))
    day_dir = tmp_path / dt_util.now().date().isoformat()
    day_dir.mkdir()
    (day_dir / "000000_unknown_abc.mp4.part").write_bytes(b"cut")
    with patch(
        "custom_components.birdfy.services.async_resolve_source",
        AsyncMock(return_value=PLAYLIST),
    ), patch("custom_components.birdfy.services.async_write_mp4", write_clip):
        await hass.services.async_call(
            DOMAIN, "download_videos", {"path": str(tmp_path)}, blocking=True
        )
    assert len(list(tmp_path.rglob("*.mp4"))) == 1


async def test_cloud_error_does_not_stop_the_download(
    hass, entry, setup_entry, cloud_events, tmp_path
) -> None:
    await setup_entry(entry)
    cloud_events.extend([make_event("a"), make_event("b")])
    resolve = AsyncMock(side_effect=[aiohttp.ClientError(), PLAYLIST])
    with patch("custom_components.birdfy.services.async_resolve_source", resolve), patch(
        "custom_components.birdfy.services.async_write_mp4", write_clip
    ):
        await hass.services.async_call(
            DOMAIN, "download_videos", {"path": str(tmp_path)}, blocking=True
        )
    assert len(list(tmp_path.rglob("*.mp4"))) == 1


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("download_thumbnails", {}),
        ("download_videos", {}),
        ("analyze_events", {}),
        ("remove_species", {"species": "x"}),
    ],
)
async def test_actions_are_for_administrators(
    hass, entry, setup_entry, hass_read_only_user, action, data
) -> None:
    await setup_entry(entry)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, action, data, blocking=True, context=Context(user_id=hass_read_only_user.id)
        )


async def test_actions_without_account(hass, entry, setup_entry) -> None:
    await setup_entry(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "download_videos", {}, blocking=True)
