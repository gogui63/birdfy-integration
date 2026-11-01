"""Birdfy clips: the mp4 view and the ffmpeg helpers."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import re
import tempfile

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .coordinator import loaded_coordinator

_LOGGER = logging.getLogger(__name__)

_SEGMENT_DURATION = 2.0
_MAX_FRAME_WIDTH = 1280
FFMPEG_TIMEOUT = 120


def register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(BirdfyMp4ProxyView(hass))


def _segment_sort_key(url: str) -> tuple[int, int]:
    m = re.search(r"slice_\d+_(\d+)_(\d+)\.ts", url)
    if m:
        return int(m.group(2)), int(m.group(1))  # (gop, fragment_index)
    return (0, 0)


def _build_m3u8_from_segments(segments: list[str]) -> str:
    """Build a valid HLS playlist from segment URLs."""
    sorted_segments = sorted(segments, key=_segment_sort_key)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{int(_SEGMENT_DURATION) + 1}",
    ]
    prev_gop: int | None = None
    for url in sorted_segments:
        m = re.search(r"slice_\d+_(\d+)_(\d+)\.ts", url)
        gop = int(m.group(2)) if m else prev_gop
        if prev_gop is not None and gop != prev_gop:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{_SEGMENT_DURATION:.3f},")
        lines.append(url)
        prev_gop = gop
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


async def async_resolve_source(coordinator, alarm_id: str) -> str:
    """Return a record URL, or an M3U8 playlist built from segment URLs."""
    record_url = coordinator.record_url_cache.get(alarm_id)
    if not record_url and coordinator.data:
        for ev in coordinator.data.get("recent_events", []):
            if ev["alarm_id"] == alarm_id:
                record_url = ev["record_url"]
                break
    if record_url:
        return record_url

    # Netvue no longer returns recordUrl: use the downloadLink segments
    segments = await coordinator.fetch_segments(alarm_id)
    if segments:
        return _build_m3u8_from_segments(segments)
    return await coordinator.fetch_fresh_record_url(alarm_id)


def temp_file(suffix: str, content: str | None = None) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    if content is not None:
        with open(path, "w") as f:
            f.write(content)
    return path


def remove_file(path: str) -> None:
    os.unlink(path)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _is_written(path: str) -> bool:
    return os.path.exists(path) and os.path.getsize(path) > 0


@asynccontextmanager
async def _hls_input_args(hass: HomeAssistant, source: str) -> AsyncIterator[list[str]]:
    """Yield the ffmpeg input arguments of a URL or of an in-memory playlist."""
    if not source.startswith("#EXTM3U"):
        yield ["-f", "hls", "-i", source]
        return
    playlist_path = await hass.async_add_executor_job(temp_file, ".m3u8", source)
    try:
        yield [
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-f", "hls",
            "-i", playlist_path,
        ]
    finally:
        await hass.async_add_executor_job(remove_file, playlist_path)


async def _run_ffmpeg(*args: str) -> tuple[int | None, str]:
    """Run ffmpeg. Return (exit code, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(FFMPEG_TIMEOUT):
            _, stderr_data = await proc.communicate()
    except TimeoutError:
        return None, "ffmpeg timed out"
    finally:
        # Also on cancellation, so that ffmpeg never outlives its caller
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return proc.returncode, stderr_data.decode(errors="replace")


async def async_write_mp4(hass: HomeAssistant, source: str, dest_path: str) -> tuple[bool, str]:
    """Remux an HLS source to dest_path. Return (success, ffmpeg stderr)."""
    async with _hls_input_args(hass, source) as input_args:
        returncode, stderr = await _run_ffmpeg(
            *input_args,
            "-c", "copy",
            "-movflags", "faststart",
            "-f", "mp4",
            dest_path,
        )
    written = await hass.async_add_executor_job(_is_written, dest_path)
    return returncode == 0 and written, stderr


async def async_extract_frames(
    hass: HomeAssistant, source: str, dest_dir: str, max_frames: int
) -> tuple[list[Path], str]:
    """Write evenly spaced JPEG frames of an HLS source. Return (frames, ffmpeg stderr)."""
    segments = source.count("#EXTINF") or max_frames
    interval = max(segments * _SEGMENT_DURATION / max_frames, 1)
    async with _hls_input_args(hass, source) as input_args:
        returncode, stderr = await _run_ffmpeg(
            *input_args,
            "-vf", f"fps=1/{interval:g},scale='min({_MAX_FRAME_WIDTH},iw)':-2",
            "-frames:v", str(max_frames),
            "-q:v", "2",
            os.path.join(dest_dir, "frame_%d.jpg"),
        )
    # After a timeout the frames only cover a part of the clip, the last one may be cut
    if returncode is None:
        return [], stderr
    frames = await hass.async_add_executor_job(_sorted_frames, dest_dir)
    return frames, stderr


def _sorted_frames(dest_dir: str) -> list[Path]:
    return sorted(
        Path(dest_dir).glob("frame_*.jpg"), key=lambda p: int(p.stem.removeprefix("frame_"))
    )


class BirdfyMp4ProxyView(HomeAssistantView):
    """Transcodes HLS segments to fMP4 via ffmpeg and streams to browser."""

    url = "/api/birdfy/mp4/{alarm_id}"
    name = "api:birdfy:mp4"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, alarm_id: str) -> web.StreamResponse:
        coordinator = loaded_coordinator(self.hass)
        if coordinator is None:
            return web.Response(status=503, text="Birdfy not ready")

        source = await async_resolve_source(coordinator, alarm_id)

        if not source:
            return web.Response(status=404, text="Event not found")

        tmp_path = await self.hass.async_add_executor_job(temp_file, ".mp4")
        try:
            try:
                success, stderr = await async_write_mp4(self.hass, source, tmp_path)
            except FileNotFoundError:
                _LOGGER.error("ffmpeg not found — cannot transcode HLS to MP4")
                return web.Response(status=500, text="ffmpeg not available")

            if not success:
                _LOGGER.error("ffmpeg failed for %s: %s", alarm_id, stderr)
                return web.Response(status=502, text="ffmpeg failed")

            mp4_data = await self.hass.async_add_executor_job(_read_bytes, tmp_path)
        finally:
            await self.hass.async_add_executor_job(remove_file, tmp_path)

        return web.Response(
            body=mp4_data,
            content_type="video/mp4",
            headers={"Access-Control-Allow-Origin": "*"},
        )
