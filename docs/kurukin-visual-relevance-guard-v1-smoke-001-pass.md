# Kurukin visual relevance guard v1 smoke 001 PASS

- task_id: `kurukin-visual-relevance-guard-smoke-001`
- commit probado: `b06504e feat: add visual relevance guard`
- topic usado: `5 errores al comprar una casa usada`
- audio_path: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- visual candidato detectado: `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`
- visual_autofill_source: `local_picker_v1`
- visual_relevance_score: `-45`
- visual_relevance_confidence: `low`
- visual_relevance_reason: `no_topic_keyword_match, no_strong_topic_keyword_match`

## Resultado sin fallback

- status: `NEEDS_INPUT`
- ok: `false`
- reason: `needs_relevant_local_visual_asset`
- queue item creado: no
- render: no

## Resultado con fallback explícito

- allow_low_relevance_visual: `true`
- status: `READY_TO_SUBMIT`
- ok: `true`
- visual_relevance_confidence: `low`
- visual_relevance_warning: `low_relevance_visual_allowed`
- warnings: `low_relevance_visual_allowed`
- queue item creado: `storage/nightly_jobs/pending/20260713-025958-kurukin-visual-relevance-guard-smoke-001.json`
- status en cola: `QUEUED`
- render: no

## Flujo validado

`topic_to_video` + `audio_path` -> topic planner local -> `local_picker_v1` -> Visual Relevance Guard -> `NEEDS_INPUT` si confidence `low` -> fallback explícito -> `READY_TO_SUBMIT` -> enqueue `QUEUED`.

## Confirmaciones

- sin render
- sin submit MPT nativo
- sin runner
- sin `/api/v1/videos`
- sin proveedores externos
- sin OpenAI/TTS real
- sin Asset Hub API
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
