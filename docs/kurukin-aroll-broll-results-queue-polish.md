# A-roll / B-roll results and queue polish

## Current state

- The real runner E2E PASS is recorded as `aroll-broll-runner-smoke-003`.
- The expected output stays at `storage/tasks/<task_id>/final-1.mp4`.
- Render Console reads existing queue/results artifacts only.
- Listing Cola or Resultados does not execute runner, renderer, ffmpeg or API calls.

## Visible metadata

For `render_mode=aroll_broll`, Cola and Resultados show:

- `Tipo: Presentador + B-roll`
- `Layout: alternating_fullscreen`
- `Audio: A-roll original`
- `B-roll muted`
- `Task ID`

Render mode detection is read-only and can use:

- pending job JSON
- completed `job.json`
- completed `submit-response.json`
- completed `final-task.json`
- `task_id` fallback for ids starting with `aroll-broll`

## Guardrails

- No pending job is created by visibility checks.
- No task directory is created by visibility checks.
- Existing MP4 files under `storage/tasks` are not deleted or moved.
- Storage artifacts are not staged.
- Asset Hub code/API, DB, rclone, credentials, `config.toml` and `resource/fonts`
  remain out of scope.
