"""Tests of the video helpers, with the real ffmpeg when it is available."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.birdfy.http import (
    _run_ffmpeg,
    async_extract_frames,
    async_write_mp4,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture
def playlist(tmp_path) -> str:
    """Return an in-memory playlist of 6 segments of 2 s, like downloadLink gives."""
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
         "testsrc2=size=1920x1080:rate=10:duration=12", "-c:v", "libx264", "-g", "20",
         "-f", "segment", "-segment_time", "2", str(tmp_path / "s%d.ts")],
        check=True,
    )
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:3"]
    for i in range(6):
        lines += ["#EXTINF:2.000,", f"file://{tmp_path}/s{i}.ts"]
    return "\n".join([*lines, "#EXT-X-ENDLIST"])


async def test_extract_frames(hass: HomeAssistant, tmp_path, playlist: str) -> None:
    out = tmp_path / "frames"
    out.mkdir()
    frames, stderr = await async_extract_frames(hass, playlist, str(out), 6)
    assert [f.name for f in frames] == [f"frame_{i}.jpg" for i in range(1, 7)], stderr
    assert all(f.stat().st_size > 1000 for f in frames)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(frames[0])],
        check=True, capture_output=True, text=True,
    )
    assert probe.stdout.strip() == "1280,720"


async def test_extract_frames_is_ordered_past_nine(hass, tmp_path, playlist) -> None:
    out = tmp_path / "frames"
    out.mkdir()
    for i in range(1, 12):
        (out / f"frame_{i}.jpg").write_bytes(b"x")
    frames, _ = await async_extract_frames(hass, "not a clip", str(out), 6)
    assert [f.name for f in frames][:3] == ["frame_1.jpg", "frame_2.jpg", "frame_3.jpg"]
    assert frames[-1].name == "frame_11.jpg"


async def test_extract_frames_of_a_broken_clip(hass, tmp_path) -> None:
    frames, stderr = await async_extract_frames(hass, "#EXTM3U\nnope", str(tmp_path), 6)
    assert frames == []
    assert stderr


async def test_write_mp4_still_works(hass, tmp_path, playlist) -> None:
    dest = tmp_path / "clip.mp4"
    success, stderr = await async_write_mp4(hass, playlist, str(dest))
    assert success, stderr
    assert dest.stat().st_size > 0


async def test_write_mp4_of_a_broken_clip_without_output(hass, tmp_path) -> None:
    success, stderr = await async_write_mp4(hass, "#EXTM3U\nnope", str(tmp_path / "clip.mp4"))
    assert not success
    assert stderr


@pytest.fixture
def started() -> list:
    """Keep the processes started by _run_ffmpeg."""
    processes = []
    create = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        processes.append(await create(*args, **kwargs))
        return processes[-1]

    with patch("asyncio.create_subprocess_exec", spy):
        yield processes


STALLED = ("-re", "-f", "lavfi", "-i", "testsrc2=duration=60", "-f", "null", "-")


async def test_stalled_ffmpeg_is_killed(started) -> None:
    with patch("custom_components.birdfy.http.FFMPEG_TIMEOUT", 0.2):
        returncode, stderr = await _run_ffmpeg(*STALLED)
    assert returncode is None
    assert stderr == "ffmpeg timed out"
    assert started[0].returncode is not None


async def test_frames_of_a_timed_out_ffmpeg_are_dropped(hass, tmp_path) -> None:
    (tmp_path / "frame_1.jpg").write_bytes(b"cut")
    with patch(
        "custom_components.birdfy.http._run_ffmpeg",
        AsyncMock(return_value=(None, "ffmpeg timed out")),
    ):
        frames, stderr = await async_extract_frames(hass, "#EXTM3U", str(tmp_path), 6)
    assert frames == []
    assert stderr == "ffmpeg timed out"


async def test_cancelled_ffmpeg_is_killed(started) -> None:
    task = asyncio.create_task(_run_ffmpeg(*STALLED))
    while not started:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert started[0].returncode is not None
