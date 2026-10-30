"""Tests of the video helpers, with the real ffmpeg when it is available."""
from __future__ import annotations

import shutil
import subprocess

import pytest
from homeassistant.core import HomeAssistant

from custom_components.birdfy.http import async_extract_frames, async_write_mp4
from custom_components.birdfy.media_source import FRAME_PREFIX, FILE_PREFIX

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


def test_prefixes_are_distinct() -> None:
    assert FILE_PREFIX == "file:"
    assert FRAME_PREFIX != FILE_PREFIX
