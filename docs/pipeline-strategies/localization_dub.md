# Localization Dub Pipeline Strategy

## Clean-Room Boundary

`localization_dub` is implemented from scratch for Script2Video Studio. It uses
the existing Script2Video pipeline DSL, artifact schemas, provider contracts,
worker jobs, and Web workspace controls. It does not copy third-party dubbing
service schemas, prompts, or pipeline files.

## Product Positioning

This pipeline is roadmap item V3 #5. It takes an existing video as the primary
input and produces a localized version: translated subtitles, dubbed narration,
and an optional lip-sync pass.

Primary use cases:

- Video translation: make an existing video understandable in another language.
- Cross-border marketing: reuse a produced ad for another market.
- Course localization: translate training or course videos for another region.
- Content reposting: prepare an existing video for another platform or audience.

## Stage Strategy

`intake`

- Normalize the source video reference, source language, target languages,
  platform, duration, and aspect ratio.
- Preserve the source reference; this pipeline does not reshoot anything.

`analyze`

- Run media analysis on the source: transcript (speech-to-text), scene
  boundaries, and audio inventory.
- Missing streams must be recorded as explicit limitations, never silently
  dropped: a video without usable speech produces an honest transcript gap, not
  an empty dub.

`localize_plan`

- Decide target languages, translation glossary (brand and product terms),
  voice casting per language, and the lip-sync policy.
- Lip-sync is opt-in and may be explicitly "not requested"; the plan is the
  single place where that decision is recorded and reviewed.

`translate`

- Produce timed translated narration segments and captions per target language.
- Segments preserve original timing as closely as possible so the dub fits the
  original edit.

`dub`

- Generate dubbed narration audio per target language through the selected TTS
  tool and record alignment status and lip-sync status in the dubbing manifest.
- Long-running provider work stays planned (not auto-dispatched) in this beta.

`edit`

- Plan subtitle styling, the audio mix between original and dub, and
  recomposition intent.

`compose`

- Render the localized video with translated subtitles and dubbed audio.

`final_review`

- Review translation accuracy, dub timing, subtitle fit, and the audio mix.

`export`

- Produce a publish package with render asset id and access links.

## Artifact Contract

Reused v1 schemas: `intake_brief.v1`, `media_analysis_report.v1`,
`spoken_script.v1`, `edit_plan.v1`, `render_summary.v1`, `review_notes.v1`,
`publish_package.v1`.

New v1 schemas introduced by this pipeline:

- `localization_plan.v1`: target languages, glossary, voice casting, and
  lip-sync policy.
- `dubbing_manifest.v1`: dub audio tracks, subtitle tracks, alignment status,
  and lip-sync status.

Future variants should add fields through backward-compatible schema versions
instead of changing existing v1 semantics in place.

## Capability Mapping

Required: `media_analysis`, `speech_to_text`, `tts`, `subtitle_generation`,
`video_composition`, `storage`. Optional: `llm_generation`,
`audio_processing`.

All required capabilities have tools in `tool_catalog/default.json`. Preflight
reports the pipeline as blocked (not degraded) until the speech-to-text and TTS
provider keys are configured — this is intentional honesty, not a defect.

## Verification Scope

Minimum verification for this pipeline:

- Loader resolves `localization_dub` and the snapshot round-trips.
- Preflight, execution plan, and stage manifest build for the new key.
- Database run creation stores a pipeline version snapshot and the review
  gates advance a smoke run to `final_review`.
- Placeholder artifacts validate against the new schemas (both allow the
  runner's `{"stage_key", "status": "placeholder"}` shape).
