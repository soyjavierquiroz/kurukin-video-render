# Current production pipeline baseline

Audit date: 2026-08-22 UTC. This describes the current working tree (including uncommitted code). No render was run.

## 1. Current Git state

Branch: feature/subtitle-alignment-hyperframes-v1

Latest 15 commits:
1ef1cbd fix: enforce approved asset selection during production materialization
a011536 feat: preserve Asset Hub metadata in human review
b55d34f feat: harden resumable production pipeline
611604b fix: resolve Asset Hub renderer paths from bundle-relative paths
7049415 fix: enforce scoped review coverage and editorial constraints
5725efc feat: add editorial gender profiles and review backups
ba75f68 fix: balance short semantic subtitle cues
6623044 fix: segment subtitles into short semantic cues
a9b05c1 fix: restore orientation compatibility filtering
af30409 feat: add asset flipping and semantic SRT wrapping
ea26392 fix: derive Asset Hub review queries from scene text
cc5a452 feat: improve Asset Hub visual search queries
f49d6ef feat: harden unattended nightly production
beeb5b2 fix: align Whisper compound tokens to canonical script
5351071 fix: route subtitle review jobs outside failed queue

| Working-tree file | State | Classification |
| --- | --- | --- |
| app/custom/human_review.py | modified | human review |
| app/custom/kurukin_asset_hub_wiring.py | modified | Asset Hub |
| app/services/subtitle.py | modified | subtitles |
| scripts/batch_mpt_worker.py | modified | production pipeline |
| scripts/produce_batch.py | modified | production pipeline / HyperFrames / registry-cache |
| tests/custom/test_human_review.py | modified | tests |
| tests/custom/test_kurukin_asset_hub_wiring.py | modified | tests |
| tests/custom/test_production_pipeline.py | modified | tests |
| 0 | untracked, empty | unrelated |
| scripts/production_registry.py | untracked | registry/cache |
| tests/custom/test_subtitle_semantic_regression.py | untracked | tests |

## 2. Actual production entrypoint

Command: python3 scripts/produce_batch.py --production --approved-plan <plan>.

scripts/produce_batch.py:main() requires both flags, then calls process_approved_review_plan(). It returns 0 only for "completed"; exceptions print PRODUCTION FAILED and return 1. CLI defaults passed are preset=editorial-gold and position=bottom. The frozen plan visual_style is used; CLI style is not passed through this branch.

process_approved_review_plan() reads the supplied plan; requires review_status=approved, a nonempty audio file, a script file, and human_review.validate_approved_plan_integrity(plan).ok. It creates a Job from the plan and calls process_job().

| Order | Entry / code | Input -> output | validation / skip / provenance | failure |
| --- | --- | --- | --- | --- |
| 1 | process_approved_review_plan(), scripts/produce_batch.py | production plan -> report/job | approved; MP3/TXT available; integrity valid; plan style frozen | blocks this command/job |
| 2 | process_job() -> ProductionRegistry.find_valid()/backfill_completed() | shared identity -> storage/production_registry.sqlite3; hit -> batch final MP4 | valid registry target must pass valid_mp4; backfill requires completed report, frozen inputs, and recipe provenance | blocks job |
| 3 | write_manifest()/make_manifest() | job/plan -> storage/tasks/<task>/batch-manifest.json | written for registry miss; MPT plan path is included only when canonical review plan is approved | blocks |
| 4 | run_worker(master) -> batch_mpt_worker.run_master() | manifest -> storage/tasks/<task>/final-1.mp4 | skip iff valid_mp4: nonempty, ffprobe-readable, video stream, positive duration; no master fingerprint | blocks |
| 5 | run_master() -> selection_result_from_plan() -> acquire_selected_materials() -> _stage_human_review_timeline() -> task.start(stop_at="video") | frozen UIDs -> acquired assets -> temporary timeline clips -> master | timeline shortfall <=0.01; material decision/count/files must agree | blocks |
| 6 | apply_visual_style() | master -> optional final-styled-warm-sepia.mp4 | none uses master; warm-sepia skip requires report style/version/path plus file validation; checks video, dimensions, audio, duration delta <=0.35s | blocks |
| 7 | run_worker(subtitles) -> run_subtitles() -> subtitle.create()/correct() | canonical MP3/script -> subtitle.srt, subtitle.raw.srt, subtitle-alignment.json | skip iff valid SRT + approved report + matching subtitle-stage fingerprint | low confidence becomes review_required, skips HF, command returns 2 |
| 8 | subtitle_quality_issues()/repair_subtitle_semantics() | SRT/script -> repaired SRT/report/stage metadata | one deterministic repair, then recheck | unresolved issue blocks |
| 9 | run_hyperframes() | delivery master + SRT -> HyperFrames input/output -> final-subtitled.mp4 | skip iff valid MP4 + HF-stage fingerprint + <=0.35s duration delta | blocks |
| 10 | link_or_copy(); ProductionRegistry.upsert() | task final -> batch final -> registry row | batch final valid_mp4; registration is last | blocks; partial output is not registered |

The generic directory-batch branch catches one job failure and continues later jobs, returning 3 if any fail. This approved-plan command has one job and ends on its exception.

## 3. Current pipeline map

approved production-plan.json + frozen MP3/TXT
-> process_approved_review_plan(); human_review.validate_approved_plan_integrity()
-> shared completed-production lookup / qualified historical backfill
-> ProductionRegistry.find_valid(); backfill_completed(); production_registry.identity()
-> task manifest
-> write_manifest()/make_manifest()
-> approved Asset Hub/local materialization + exact Human Review timeline staging
-> selection_result_from_plan(); acquire_selected_materials(); _stage_human_review_timeline()
-> MPT video-only master
-> batch_mpt_worker.run_master(); task.start(stop_at="video")
-> optional warm-sepia delivery master
-> apply_visual_style()
-> canonical MP3 Whisper transcription + canonical-script semantic alignment
-> batch_mpt_worker.run_subtitles(); subtitle.create()/correct()
-> semantic SRT quality gate / one repair
-> subtitle_quality_issues(); repair_subtitle_semantics()
-> HyperFrames build/render
-> run_hyperframes()
-> duration-validated final + global registration
-> ensure_similar_duration(); valid_mp4(); ProductionRegistry.upsert()

There is no WAV stage. CANONICAL_ALIGNMENT_SOURCE="MP3", SUBTITLE_WAV_REQUIRED=False, and extract_subtitle_audio() has no caller.

## 4. Human Review / timeline policy

Source: app/custom/human_review.py.

- Primary is segment.selected_asset. Backups are only segment.backup_assets. Suggestions never enter the renderer.
- Frozen source duration is only asset.metadata.duration via _asset_source_duration(); scene target is never substituted.
- PREFERRED_PLAYBACK_SPEED=0.90; HARD_MIN_PLAYBACK_SPEED=0.85; MIN_BACKUP_OUTPUT_SECONDS=0.75.
- A sufficiently long primary is normal speed. A short primary can slow to its target only at >=0.85. Otherwise plan at 0.90 then consume unique approved backups >=0.75s.
- MAX_SEGMENT_FREEZE_SECONDS=1.25. Remaining segment gap in (0,1.25] freezes the final planned asset (0.04s source); longer gap remains a shortfall.
- Required duration is plan.duration +0.10; final segment gets that 0.10s.
- MAX_TIMELINE_AUTOFILL_SECONDS=5.0. Only after all segments are resolved, a <=5s unsegmented tail may EXTEND unused final source, LOOP/replay it, then FREEZE it. No discovery/substitution occurs.
- FLIP_HORIZONTAL_DEFAULT=True. asset_flip_horizontal() normalizes absent data; _stage_human_review_timeline() adds hflip for true.
- Orientation compatibility is selection/review policy; collect_warnings() flags a landscape selected asset for a portrait plan. Production integrity does not itself reject the warning.

Production blocks for unapproved/invalid plan; no segments; invalid target/source duration; missing UID; duplicate authorized UIDs; unresolved segment/tail coverage; or any acquisition/staging/render failure. Stored coverage is compared/recomputed but drift is logged, not a standalone failure.

## 5. Asset Hub contract

Production path:
human_review.selection_result_from_plan()
-> material_acquisition.acquire_selected_materials()
-> _asset_hub_materials()
-> kurukin_asset_hub_wiring.wire_explicit_asset_hub_bundle()
-> provider.create_bundle()/materialize_bundle()/get_renderer_manifest().

selection_result_from_plan() derives the exact frozen render timeline and materializes each used UID only once. _approved_plan_asset_hub_uids() derives Asset Hub UIDs from that selection. _asset_hub_materials() blocks unless decision UIDs exactly equal plan-derived UIDs; it groups selected decisions by search term into synthetic scene-### requests and retains ordered selected_asset_uids. No discovery/substitution fallback exists.

wire_explicit_asset_hub_bundle() validates UID values, creates bundle, materializes with force=False, requires ready, validates renderer manifest and exact scene/UID selection, and resolves ready files. It retries once only for KurukinAssetHubMaterializationNotReady, or the exact stale selected-UID mismatch; retry recreates the same frozen selection and calls force=True. Second failure blocks.

asset_hub_manifest.py rules: relative_path wins over local_path, resolves under <AssetHub root>/<bundle_uid>, and must be relative/no parent traversal. local_path fallback must resolve under AssetHub root. Ready asset, allowed type and existing file are required. extract_asset_hub_local_assets() normalizes local_path to the resolved file.

Parallel implementations:
- KEEP production: _asset_hub_materials() + wire_explicit_asset_hub_bundle().
- LEGACY/other workflow: asset_materializer.materialize_assets_for_aroll_broll(); not called by run_master().
- RELATED duplicate converter: convert_asset_hub_manifest_to_materials() consumes all valid assets, while production exact-filters requested UIDs.

## 6. Subtitle pipeline

run_subtitles() fixes Whisper to medium/CPU/int8 and uses manifest audio_file: the frozen canonical MP3. subtitle.create() uses faster-whisper word timestamps, beam_size=5, VAD 500ms. subtitle.correct() saves subtitle.raw.srt, aligns canonical script to Whisper tokens, writes subtitle-alignment.json, and on success writes canonical semantic subtitle.srt.

_build_alignment_result() requires nonempty script tokens, global coverage >= GLOBAL_OK_THRESHOLD=0.90, every line >= LINE_MIN_COVERAGE=0.40, valid monotonic timing, and one output per script line. _semantic_subtitle_items() computes semantic spans; _rebalance_semantic_spans() repairs connector/orphan boundaries. Limits: target 20-52 chars, soft 64, hard 76, target 4-7 words (app/services/subtitle.py).

subtitle_quality_issues() requires parseable, nonempty, ordered, non-overlapping cues; no out-of-audio timing; no duplicate adjacent text; no dangling terminal orphan/no; no nonterminal one-word cue; no dangling hyphen/ellipsis. After rebuild it invokes repair_subtitle_semantics() once, which reuses _build_alignment_result() and writers. A low-confidence worker report is review_required (no repair; HF blocked).

SUBTITLE_RECIPE_VERSION="semantic-cues-v4". subtitle-stage fingerprint contains MP3 SHA-256, TXT SHA-256, version, language=es, policy=punctuation-clause-natural-phrase, semantic_rebalance=connector-v1, max_display_lines=2. SUBTITLES SKIP requires valid SRT + approved report + fingerprint. Any existing SRT or metadata not satisfying this logs SUBTITLES STALE then SUBTITLES REBUILD.

Older segmentation remains reachable outside production: app/custom/subtitle_optimizer.py optimize_srt_file()/split_caption_text(), called by app/services/task.py generate_subtitle() in ordinary MPT flows. Production disables task subtitles/optimizer and uses its separate worker. The punctuation splitting in subtitle.create() is only raw intermediate production output before correct() overwrites canonical output.

## 7. HyperFrames pipeline

run_hyperframes() takes video_for_delivery: final-1.mp4 for style none, otherwise validated final-styled-warm-sepia.mp4; and canonical subtitle.srt. It copies them to /opt/apps/hyperframes/input/<task>/master.mp4 and subtitle.srt, then runs build-and-render.mjs with preset/position/build-only and render-job.mjs in the running hyperframes_hyperframes container.

Source output: /opt/apps/hyperframes/output/<task>.mp4. Task output: storage/tasks/<task>/final-subtitled.mp4. Batch output: storage/batch_outputs/<batch>/<stem>.mp4.

HYPERFRAMES_RECIPE_VERSION="hyperframes-editorial-gold-v2". Final fingerprint includes delivery-master SHA-256, SRT SHA-256, subtitle/HF recipe versions, preset, position, visual style and style version. SKIP requires valid final, matching metadata and <=0.35s duration delta. A valid final with bad/missing provenance logs HYPERFRAMES STALE; otherwise HYPERFRAMES REBUILD. Rebuild requires valid_mp4 and the duration check.

MPT-side code passes SRT to HyperFrames and never rewrites it afterward. Thus HyperFrames does not change temporal cue boundaries in this pipeline.

## 8. Global production registry

Shared implementation is scripts/production_registry.py:identity(). It NFKC/casefold/whitespace-normalizes title and material_title, hashes MP3 and TXT, then hashes:
material_title normalized, title normalized, audio_sha256, script_sha256, production_recipe_version.

Stored record fields include production_fingerprint, normalized_title, original_title, material_title, audio_sha256, script_sha256, production_recipe_version, final path/duration/size/status.

Current recipe is production_recipe_for(style,preset,position): production-v4:semantic-cues-v4:hyperframes-editorial-gold-v2 plus style@style-version, preset, position. Subtitle/HF versions therefore participate in global identity via recipe.

- --reindex-completed -> backfill_completed(emit=True) -> identity(), only after valid final, completed report, frozen inputs, and nonempty historical recipe.
- normal production -> identity() -> find_valid(), then targeted safe backfill on miss.
- successful production -> identity record -> upsert() only after final validation.

All three use the single shared identity() implementation. Historical recipe provenance versus current generated recipe is intentionally different source data; recipe-less legacy reports are not backfilled.

## 9. Resume/cache matrix

| Artifact | Reused when | Invalidated by | Rebuilt from |
| --- | --- | --- | --- |
| Asset Hub materialization | no persistent production reuse; each master render materializes | master rebuild; not-ready/stale selection retries once | frozen timeline UIDs -> bundle/manifest |
| master final-1.mp4 | valid_mp4 only | absent/corrupt/nonvideo/nonpositive duration only | plan -> acquisition -> staged clips -> MPT |
| styled master | matching report style/version/path + style validation | style report mismatch, missing/invalid output; not master hash | master + ffmpeg |
| canonical SRT | valid SRT + approved alignment report + subtitle-stage fingerprint | MP3/TXT hash, subtitle recipe/policy, bad report/SRT | MP3 + Whisper + script alignment |
| subtitle alignment metadata | required approved report as above | missing/status-confidence-review failure or stage mismatch | subtitle.correct()/repair |
| HyperFrames final | valid MP4 + HF fingerprint + duration delta | delivery-master/SRT hash, subtitle/HF recipe, preset, position, style/version, duration | delivery master + SRT -> HF |
| global completion | matching identity + valid registry target, cross-batch only | content/title/material/recipe identity change or corrupt target | validated batch final; qualified reindex |

Old artifacts can survive: master has no recipe/plan fingerprint, and styled master has no master hash. Downstream stages have content-sensitive fingerprints.

## 10. Real batch check (no render)

Inspected storage/review_queue/noche-mi-otra-yo-2026-08-20/*/production-plan.json.

| Measure | Count |
| --- | ---: |
| plans | 10 |
| approved and integrity-valid | 10 |
| invalid | 0 |
| existing valid masters | 3 |
| existing valid SRTs | 3 |
| existing valid finals | 2 |
| registry hits for frozen style/editorial-gold/bottom | 0 |
| stale subtitle provenance among existing SRTs | 3 |
| stale HyperFrames provenance among existing finals | 2 |
| plans requiring timeline fallback | 7 |

Fallback distribution: 1 LOOP-only, 2 EXTEND-only, 3 FREEZE-only, 1 EXTEND+LOOP. Every plan has computed shortfall 0; fallback is allowed recovery, not a blocker.

## 11. Duplication / patch debt

| Concern | KEEP | LEGACY/DUPLICATE | Why |
| --- | --- | --- | --- |
| coverage | render_timeline_from_plan() + resolve_human_review_segment_duration() | coverage_summary()/segment_coverage_metrics() | accounting is derived from canonical resolver, but duplicates duration arithmetic |
| Asset Hub selection | _asset_hub_materials() + validate_explicit_manifest_selection() | materials_from_approved_plan(), convert_asset_hub_manifest_to_materials() | production first pair exact-enforces; others have other scopes |
| Asset Hub path resolution | asset_hub_manifest path resolver | kurukin_asset_hub.resolve_ready_asset_paths() | both implement ready/path behavior in production-adjacent layers |
| materialization retry | wire_explicit_asset_hub_bundle() | KurukinAssetProvider.materialize_bundle() behavior | different layers; wiring owns one-retry stale-manifest rule |
| MP4/duration validation | valid_mp4()+ffprobe_media() | validate_styled_master()/ensure_similar_duration() | shared reader but repeated contract checks |
| subtitle segmentation | subtitle.correct() semantic writer | subtitle_optimizer.optimize_srt_file()/split_caption_text() | ordinary task flow remains reachable; production bypasses |
| subtitle repair | subtitle.correct() | repair_subtitle_semantics() | repair orchestrates same private builder/writer |
| production fingerprint | production_registry.identity() | none | global identity singular |
| master resume | process_job valid_mp4 check | none | singular production path |
| HyperFrames resume | process_job final-stage fingerprint | none | singular production path |

## A. Current golden path

Approved integrity-valid plan -> registry miss -> exact material/timeline staging -> valid master -> frozen style -> approved semantic SRT -> semantic gate -> duration-matched HyperFrames final -> batch copy -> registry upsert.

## B. Current blocking conditions

Invalid/unapproved plan; missing MP3/TXT; duration/UID/duplicate/coverage integrity error; Asset Hub mismatch/not-ready after retry; staging/MPT/style failure; invalid master/final; low-confidence subtitles; unresolved semantic SRT issue; HyperFrames failure; >0.35s final duration delta; final registry failure.

## C. Current auto-recovery conditions

Cross-batch valid registry reuse; qualified historical reindex; rebuilding corrupt/missing master/final; one Asset Hub forced retry; warm-sepia audio-copy fallback to AAC; one semantic SRT repair; rebuild of stale subtitle/HF stages.

## D. Current cache/resume rules

See section 9. The highest-impact gap is validity-only master reuse. Styled-master reuse also lacks master-content provenance.

## E. Duplicate/legacy code found

Reachable non-production subtitle optimizer; generic versus approved-production Asset Hub materializers/converters; multiple Asset Hub ready/path layers; derived coverage helpers; repair wrapper around subtitle private APIs.

## F. Risks before mass production

1. Master can survive changed plan/material/timeline/renderer policy if still a valid MP4.
2. Styled master can survive changed master because no master hash is checked.
3. Asset Hub bundle scenes are grouped by search term rather than original segment ID.
4. Two reachable repository-wide subtitle segmentation policies can diverge.
5. This batch has 3 stale SRT stages, 2 stale HF stages, zero registry hits, and 7 fallback-dependent plans.

## G. Recommended consolidation items

Do not implement from this audit: add master/styled-master provenance fingerprints; provide one public semantic subtitle rebuild API; consolidate Asset Hub ready/path extraction; preserve approved plan scene IDs through bundle creation; version master timeline/materialization policy; decide whether generic subtitle optimizer remains supported.
