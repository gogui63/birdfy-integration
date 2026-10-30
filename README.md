# Birdfy Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Home Assistant custom integration for [Birdfy](https://www.birdfy.com/) smart bird feeder cameras. Browse and play SD card recordings directly from the HA Media Browser, and download thumbnails or videos to local or network storage.

## Features

- **Media Browser**: browse recordings by day and play clips directly in HA
- **Sensor**: last detection event with timestamp and attributes
- **Camera**: thumbnail of the last detection
- **Download thumbnails**: save event thumbnails to local or network storage (delta mode)
- **Download videos**: save event videos as MP4 to local or network storage (delta mode)
- Polls the Netvue cloud API every 5 minutes
- Multi-device support
- No local hardware required

## Installation via HACS

1. In Home Assistant, go to **HACS → Integrations**
2. Click the three-dot menu → **Custom repositories**
3. Add `https://github.com/gogui63/birdfy-integration` with category **Integration**
4. Install **Birdfy Integration**
5. Restart Home Assistant
6. Go to **Settings → Devices & Services → Add Integration** → search for **Birdfy**
7. Enter your Netvue/Birdfy account email and password

## Entities

| Entity                      | Description                            |
| --------------------------- | -------------------------------------- |
| `sensor.birdfy_last_event`  | Label and attributes of the last detection |
| `camera.birdfy_thumbnail`   | Thumbnail image of the last detection  |

### Sensor attributes

| Attribute    | Description                          |
| ------------ | ------------------------------------ |
| `label`      | Detection label (e.g. `bird`, `unknown`) |
| `alert_time` | Timestamp of the detection           |
| `alarm_id`   | Unique event identifier              |
| `image_url`  | Direct URL of the event thumbnail    |

## Media Browser

Open the **Media Browser** in the HA sidebar → **Birdfy** → select a day → play a clip.

Clips are streamed as MP4 via ffmpeg (HLS → MP4 transcoding). ffmpeg must be available on the Home Assistant host (included by default in HA OS).

## Services

### `birdfy.download_thumbnails`

Downloads event thumbnails (JPG) to local or network storage. Only downloads files that don't already exist (delta mode). Defaults to today only.

| Parameter | Description                                                              | Default                   |
| --------- | ------------------------------------------------------------------------ | ------------------------- |
| `days`    | Number of days to fetch (1–30)                                           | `1`                       |
| `share`   | Network share name mounted in HA under `/media/` (takes priority over `path`) | —                    |
| `path`    | Full path where thumbnails will be saved                                 | `/media/birdfy_thumbnails` |

Example automation:

```yaml
action:
  - service: birdfy.download_thumbnails
    data:
      days: 1
      share: my_nas
```

With multiple cameras, a subfolder per device serial number is created automatically under the base path.

### `birdfy.download_videos`

Downloads event videos (MP4) to local or network storage. Only downloads files that don't already exist (delta mode). Requires ffmpeg on the HA host. Defaults to today only.

| Parameter | Description                                                              | Default              |
| --------- | ------------------------------------------------------------------------ | -------------------- |
| `days`    | Number of days to fetch (1–30)                                           | `1`                  |
| `share`   | Network share name mounted in HA under `/media/` (takes priority over `path`) | —               |
| `path`    | Full path where videos will be saved                                     | `/media/birdfy_videos` |

Example automation:

```yaml
action:
  - service: birdfy.download_videos
    data:
      days: 1
      share: my_nas
```

## Network Storage

To use a NAS or network share:

1. In Home Assistant, go to **Settings → System → Storage**
2. Add your network share (SMB or NFS)
3. The share will be mounted under `/media/<share_name>`
4. Use `share: <share_name>` in the service call

## Notes

- The `label` field is always `unknown` if your camera model does not return AI classification
- SD card recordings are stored as HLS streams on Netvue S3 servers, valid for approximately 1 hour after fetch
- Video download requires ffmpeg (available by default on HA OS)
- For live RTSP streaming, see [birdfy-rtsp](https://github.com/sebsst/birdfy-rtsp)
