# Kurukin Job Spec

Kurukin job specs are reusable JSON inputs for
`app/custom/kurukin_job_adapter.py`. The adapter validates the spec and returns
a MoneyPrinterTurbo payload compatible with `POST /api/v1/videos`; it does not
enqueue, call the API, render, touch rclone, or read Asset Hub databases.
The Kurukin Render Console also produces this spec shape before delegating to
the same adapter.

Flow:

```text
Kurukin Job Spec JSON
  -> app/custom/kurukin_job_adapter.py
  -> MoneyPrinterTurbo payload
  -> /api/v1/videos or scripts/local_job_wrapper.py queue
```

## Top-level fields

- `job_id`: required string for runner metadata and pending queue filenames.
- `description`: optional human note, kept as runner-filtered metadata.
- `render_quality`: optional profile. Accepts `draft_720p`,
  `standard_1080p`, `premium_2k`, plus aliases `720p`, `1080p`, `2k`,
  `draft`, `standard`, and `premium`.
- `selectedAssets`: optional list of local assets. Required unless an Asset Hub
  renderer manifest is provided.
- `audio`: optional local custom audio contract.
- `subtitles`: optional subtitle mode contract.
- `image_motion`: optional global image motion contract for local images.
- `asset_hub`: optional renderer manifest contract.
- `video`: required MoneyPrinterTurbo video config. `video_subject`,
  `video_script`, and `video_aspect` are required.
- `subtitle_style_preset` and `subtitle_style_overrides`: optional style
  contract resolved before payload creation.

## Local selectedAssets

Example:

```json
{
  "selectedAssets": [
    { "file": "intro.mp4", "label": "intro", "order": 1 },
    { "file": "photo.png", "label": "still", "order": 2 }
  ]
}
```

Files must be filenames only, with no absolute paths or parent traversal. By
default the adapter resolves them under `storage/local_videos`. The output uses:

- `video_source: "local"`
- `video_materials[*].provider: "local"`
- `video_materials[*].url` as the filename
- `runner.selectedAssets` for the original ordered metadata

`selectedAssets` is not kept at payload root.

## Asset Hub Renderer Manifest

Example:

```json
{
  "asset_hub": {
    "renderer_manifest_path": "/data/job-assets/jab_123/manifests/renderer-manifest.json",
    "bundle_uid": "jab_123",
    "scene_mode": "ordered",
    "strict": true
  }
}
```

Minimal Render Console-style example:

```json
{
  "job_id": "render-console-example-001",
  "asset_hub": {
    "renderer_manifest_path": "/data/job-assets/jab_123/manifests/renderer-manifest.json",
    "bundle_uid": "jab_123",
    "scene_mode": "ordered",
    "strict": true
  },
  "render_quality": "draft_720p",
  "subtitles": {
    "mode": "none"
  },
  "video": {
    "video_subject": "Render Console Example",
    "video_script": "Example script.",
    "video_aspect": "9:16",
    "video_concat_mode": "sequential",
    "video_transition_mode": "None",
    "video_clip_duration": 4,
    "video_count": 1,
    "voice_name": "es-MX-DaliaNeural-Female",
    "voice_volume": 1.0,
    "voice_rate": 1.0,
    "bgm_type": "none",
    "subtitle_enabled": false,
    "n_threads": 2,
    "paragraph_number": 1
  }
}
```

The manifest path must stay under `/data/job-assets` unless tests or callers
override the adapter base. The MVP only supports `scene_mode: "ordered"`.

When `asset_hub.renderer_manifest_path` exists, the manifest wins over
`selectedAssets` for core render materials. The adapter removes root
`video_materials` and sets:

- `asset_hub_renderer_manifest_path`
- `asset_hub_bundle_uid`
- `asset_hub_scene_mode`
- `asset_hub_strict`
- `runner.asset_hub`

If `selectedAssets` also exists, it is validated and preserved only in
`runner.selectedAssets` as operator metadata.

Legacy fields under `video` remain supported:

- `video.asset_hub_renderer_manifest_path`
- `video.asset_hub_bundle_uid`
- `video.asset_hub_scene_mode`
- `video.asset_hub_strict`

## Custom Audio

Example:

```json
{
  "audio": { "file": "voiceover.mp3" }
}
```

The adapter resolves `audio.file` under `storage/local_audios` by default and
emits `custom_audio_file`. Metadata is kept in `runner.audio`. Legacy
`video.custom_audio_file` is allowed only if it resolves to the same path.

## Subtitles

Modes:

- `whisper`: sends `subtitle_provider: "whisper"`.
- `edge`: sends `subtitle_provider: "edge"` and requires generated TTS audio.
- `custom_srt`: sends `custom_subtitle_file`, disables correction, and keeps
  optimization enabled unless `optimize: false`.
- `none`: sends `subtitle_enabled: false`.

Examples:

```json
{
  "subtitles": {
    "mode": "whisper",
    "correction_enabled": false,
    "optimize": true
  }
}
```

```json
{
  "subtitles": {
    "mode": "custom_srt",
    "file": "captions.srt",
    "optimize": true
  }
}
```

With custom audio, `mode: "edge"` fails because Edge subtitles depend on the
TTS subtitle maker. Use `whisper`, `custom_srt`, or `none`.

## Image Motion

Global:

```json
{
  "image_motion": {
    "enabled": true,
    "preset": "slow_zoom_in",
    "intensity": 0.06
  }
}
```

Per asset:

```json
{
  "selectedAssets": [
    {
      "file": "photo.png",
      "order": 1,
      "motion": "pan_up",
      "motion_intensity": 0.05
    }
  ]
}
```

Supported presets: `none`, `slow_zoom_in`, `slow_zoom_out`, `pan_left`,
`pan_right`, `pan_up`, `pan_down`, `subtle_pulse`, `handheld_soft`. Aliases:
`zoom_in`, `zoom_out`, `ken_burns`, `pulse`, `handheld`.

Per-asset motion is emitted into `video_materials` only for image files. If a
video asset carries motion fields, the adapter keeps them only under
`runner.selectedAssets`.

## Output and Summary

`build_moneyprinter_payload()` returns the full MoneyPrinterTurbo request
payload. `summarize_payload()` returns a safe log/UI summary with job id,
subject, source, resolution, Asset Hub bundle, custom audio flag, subtitle
state, image motion flag, material count, and runner keys. It does not include
the full payload or secrets.
