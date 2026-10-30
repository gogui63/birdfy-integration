# Birdfy Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Home Assistant custom integration for [Birdfy](https://www.birdfy.com/) smart bird feeder cameras. Browse and play SD card recordings directly from the HA Media Browser, download thumbnails or videos to local or network storage, and identify the birds with the AI already configured in Home Assistant.

## Features

- **Media Browser**: browse recordings by day and play clips directly in HA
- **Sensors**: last detection event with timestamp and attributes, highlights and recap links
- **Camera**: thumbnail of the last detection
- **Bird identification** (optional): species of each visitor, list of all the species seen, new species detection and daily statistics, through any AI Task provider
- **Download thumbnails**: save event thumbnails to local or network storage (delta mode)
- **Download videos**: save event videos as MP4 to local or network storage (delta mode)
- Polls the Netvue cloud API every 5 minutes
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
| `sensor.birdfy_highlights`  | Link to the Birdfy highlights page     |
| `sensor.birdfy_recap`       | Link to the Birdfy recap page          |
| `camera.birdfy_thumbnail`   | Thumbnail image of the last detection  |

See [Bird identification](#bird-identification) for the entities added when AI is enabled.

### Sensor attributes

| Attribute    | Description                          |
| ------------ | ------------------------------------ |
| `label`      | Detection label (e.g. `bird`, `unknown`) |
| `alert_time` | Timestamp of the detection           |
| `alarm_id`   | Unique event identifier              |
| `image_url`  | Direct URL of the event thumbnail    |

## Bird identification

The integration can identify the bird in each new clip. It is off by default: nothing is sent to an AI, and no AI entity exists, until you choose an AI Task entity.

It does not ask for an API key. It uses the [AI Task](https://www.home-assistant.io/integrations/ai_task/) entity of a provider already set up in Home Assistant (Google Generative AI, OpenAI, Anthropic, Ollama and others), so the key and the model are managed in one place.

### Setup

1. Add an AI provider in **Settings → Devices & Services** and create its AI Task entity. It must support attachments and a vision model.
2. Go to **Settings → Devices & Services → Birdfy → Configure**.
3. Choose the **AI Task entity**, and adjust the daily limit if needed.

| Option                               | Description                                                                    | Default |
| ------------------------------------ | ------------------------------------------------------------------------------ | ------- |
| AI Task entity                       | Entity that analyzes the clips. Leave empty to disable identification          | —       |
| Maximum automatic analyses per day   | Clips over the limit are skipped. Protects against cost surprises on windy days | `100`   |

### How it works

- Clips recorded after the first activation are analyzed automatically. Use [`birdfy.analyze_events`](#birdfyanalyze_events) for older clips.
- New clips are found by the 5-minute polling, then analyzed at least 60 seconds after the detection, once the clip is available. Expect a result up to about 6 minutes after the visit.
- Up to 6 images are extracted from the clip with ffmpeg and sent to the AI, because most providers do not accept videos. The country, the month and the species already seen are added to the request. No precise location is sent.
- A clip without a bird changes nothing.
- A bird the AI cannot identify confidently counts as a visit, but its species is not added to the list.
- A clip that fails is retried at the next polls, 3 times at most.
- Each analysis, retries included, is billed by your AI provider.
- The list survives restarts. Disabling AI removes the entities but keeps the list. Removing the integration deletes it.

### AI entities

These entities only exist while an AI Task entity is selected.

| Entity                             | State                                                        | Attributes |
| ---------------------------------- | ------------------------------------------------------------ | ---------- |
| `sensor.birdfy_last_species`       | Last identified species                                      | `scientific_name`, `confidence` (`high`, `medium`), `count` (birds in the clip), `alarm_id`, `seen_at` |
| `sensor.birdfy_species_count`      | Number of species seen                                       | `species`: the complete list, most recent first. Each item has `name`, `scientific_name`, `first_seen`, `last_seen`, `count` |
| `sensor.birdfy_species_today`      | Number of species seen today                                 | `species`: names of today's species |
| `sensor.birdfy_visits_today`       | Clips containing a bird today, unidentified birds included. Reset at midnight | — |
| `sensor.birdfy_top_species`        | Most frequently seen species                                 | `count` |
| `binary_sensor.birdfy_new_species` | `on` if the last identified bird is the first of its species | `species`, `alarm_id` |
| `image.birdfy_last_bird`           | Best image of the last identified bird, chosen by the AI     | — |
| `event.birdfy_bird_detected`       | Time of the last event                                       | See below |

The `species` attribute of `sensor.birdfy_species_count` is not saved in the recorder, to keep the database small.

### Event

`event.birdfy_bird_detected` fires once per clip containing a bird. Prefer it to the binary sensor in automations: the binary sensor does not change state when two new species follow each other.

| `event_type`        | Meaning                                      |
| ------------------- | -------------------------------------------- |
| `new_species`       | First sighting of this species               |
| `known_species`     | Species already in the list                  |
| `unidentified_bird` | A bird is visible, the species is uncertain  |

| Event attribute   | Description                                      |
| ----------------- | ------------------------------------------------ |
| `species`         | Common name, in the language of Home Assistant   |
| `scientific_name` | Scientific name                                  |
| `confidence`      | `high`, `medium` or `low`                        |
| `count`           | Number of birds visible                          |
| `alarm_id`        | Unique event identifier                          |
| `seen_at`         | Time of the detection                            |

Example automation:

```yaml
triggers:
  - trigger: event.received
    target:
      entity_id: event.birdfy_bird_detected
    options:
      event_type:
        - new_species
actions:
  - action: notify.persistent_notification
    data:
      title: New species at the feeder
      message: "{{ state_attr('event.birdfy_bird_detected', 'species') }}"
```

On Home Assistant versions without the `event.received` trigger, use a `state` trigger on the entity, with a condition on its `event_type` attribute.

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

### `birdfy.analyze_events`

Identifies the birds in past clips, oldest first, in the background. Requires an AI Task entity. Clips already analyzed are skipped, and the daily limit does not apply. Each analysis is billed by your AI provider, so start with a small number of days.

Past clips update the species list, `first_seen`, and today's counters when they are from today. They only replace the last species if they are more recent, and they do not fire `event.birdfy_bird_detected`.

| Parameter | Description                     | Default |
| --------- | ------------------------------- | ------- |
| `days`    | Number of days to analyze (1–30) | `1`     |

Example:

```yaml
action: birdfy.analyze_events
data:
  days: 7
```

### `birdfy.remove_species`

Removes a species added by mistake. The next sighting of that species counts as new.

| Parameter | Description                                  |
| --------- | -------------------------------------------- |
| `species` | Common or scientific name, as in the list    |

Example:

```yaml
action: birdfy.remove_species
data:
  species: Parus major
```

## Network Storage

To use a NAS or network share:

1. In Home Assistant, go to **Settings → System → Storage**
2. Add your network share (SMB or NFS)
3. The share will be mounted under `/media/<share_name>`
4. Use `share: <share_name>` in the service call

## Notes

- The `label` field is always `unknown` if your camera model does not return AI classification. Enable [Bird identification](#bird-identification) to get the species
- SD card recordings are stored as HLS streams on Netvue S3 servers, valid for approximately 1 hour after fetch
- Video download requires ffmpeg (available by default on HA OS)
- Only the first camera of the account is used
- Species identification depends on the AI model and on the images: distant or blurred birds, and species that look alike, can be wrong. Use `birdfy.remove_species` to fix the list
- Requires Home Assistant 2025.8 or newer
- For live RTSP streaming, see [birdfy-rtsp](https://github.com/sebsst/birdfy-rtsp)
