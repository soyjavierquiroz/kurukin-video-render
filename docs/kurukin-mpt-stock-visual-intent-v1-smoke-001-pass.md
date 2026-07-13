# Kurukin MPT stock visual intent v1 smoke 001 PASS

- task_id: `kurukin-mpt-stock-visual-intent-smoke-001`
- commit probado: `1dfabe5 feat: prepare mpt stock visuals for topic intents`
- mode: `topic_to_video`
- topic: `5 errores al comprar una casa usada`
- audio_path: `storage/local_audios/mpt_native_console_smoke_002.mp3`
- allow_mpt_stock_visuals: `true`
- preferred_stock_source: `pexels`
- visual local candidato: `storage/local_videos/_aroll_broll_smoke_001/broll_visual_fixture_2s.mp4`
- visual_relevance_confidence: `low`
- visual_relevance_score: `-45`
- visual_source_mode: `mpt_stock`
- stock_source: `pexels`
- stock_terms: `5 errores al comprar una casa usada`, `checklist`, `detalle`, `explicacion`, `errores`, `comprar`, `casa`, `usada`
- status compilado: `READY_TO_SUBMIT`
- queue item: `storage/nightly_jobs/pending/20260713-035423-kurukin-mpt-stock-visual-intent-smoke-001.json`
- status en cola: `QUEUED`

## Spec MPT validada

- `compiled_mpt_spec.mpt_params.video_source`: `pexels`
- `compiled_mpt_spec.mpt_params.video_terms`: stock terms derivados del topic/topic_plan
- `compiled_mpt_spec.mpt_params.video_materials`: `[]`
- `compiled_mpt_spec.warnings`: `mpt_stock_visuals_allowed_provider_on_render`

## Flujo validado

`topic_to_video` + `audio_path` -> topic planner local -> `local_picker_v1` -> visual relevance low -> `allow_mpt_stock_visuals` -> `visual_source_mode=mpt_stock` -> MPT stock spec `pexels` -> enqueue `QUEUED`.

## Confirmaciones

- sin render
- sin submit MPT nativo
- sin runner
- sin `/api/v1/videos`
- sin proveedores externos reales
- sin llamadas a Pexels/Pixabay/Coverr
- sin descargas
- sin OpenAI/TTS real
- sin Asset Hub API
- sin `config.toml`
- sin `resource/fonts`
- sin storage stageado
