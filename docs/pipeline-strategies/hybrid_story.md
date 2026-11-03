# Hybrid Story Pipeline Strategy

## Clean-Room Boundary

`hybrid_story` is implemented from scratch for Script2Video Studio. It uses the
existing Script2Video pipeline DSL, artifact schemas, provider contracts,
worker jobs, and Web workspace controls. It does not copy third-party remix or
story-composition schemas, prompts, or pipeline files.

## Product Positioning

This pipeline is roadmap item V3 #6. The user provides real footage and assets;
the platform plans the story around them and generates only the supplemental
material that is missing, then composes everything into one video.

Primary use cases:

- User footage story: turn raw personal or event footage into a narrated story.
- UGC remix: combine community-submitted clips into one publishable video.
- Brand asset story: weave existing brand footage with generated connective
  tissue (b-roll, titles, transitions).
- Event recap: build a highlight story from attendee or event recordings.

The design rule is that user assets are authoritative: the pipeline plans
around what exists and generates the minimum needed to cover the gaps.

## Stage Strategy

`intake`

- Normalize the story goal, user asset references, platform, duration, and
  aspect ratio.

`story_plan`

- Produce the story outline: ordered beats plus the explicit split between
  beats covered by user assets and beats that need AI supplemental assets.
- Generation scope must be bounded and named; the plan is reviewed before any
  provider is called.

`visual_plan`

- Map every beat to a shot backed by a user asset or a planned generated asset.
- Every shot names its source so the later stages can bind concretely.
- The stage key deliberately avoids the shared `storyboard` name so the
  deterministic runner does not substitute the spokesperson planner's shot
  list for a hybrid-specific plan.

`ingest`

- Ingest the referenced user assets and run media analysis so shots bind to
  concrete, analyzed sources.
- Ingestion failures are recorded per asset, never silently dropped.

`supplement`

- Generate the supplemental assets the storyboard marked as AI-provided
  (images, video clips, narration, music).
- Stays planned (not auto-dispatched) in this beta; provider blockers are
  recorded in the asset manifest.

`asset_review`

- Review the combined user + generated asset set before edit planning, with
  style consistency and beat coverage as explicit review focuses.

`edit`

- Build the timeline that stitches user and generated assets, with subtitles
  and audio mix intent.

`compose`

- Render the story video from the approved combined assets.

`final_review`

- Review story coherence, asset transitions, subtitle fit, and audio mix.

`export`

- Produce a publish package with render asset id and access links.

## Artifact Contract

Reused v1 schemas: `intake_brief.v1`, `shot_list.v1`,
`source_asset_manifest.v1`, `asset_manifest.v1`, `review_notes.v1`,
`edit_plan.v1`, `render_summary.v1`, `publish_package.v1`.

New v1 schema introduced by this pipeline:

- `story_outline.v1`: story beats with the user-asset / generated-asset split,
  the user asset plan, and the bounded supplemental generation plan.

Future variants should add fields through backward-compatible schema versions
instead of changing existing v1 semantics in place.

## Capability Mapping

Required: `asset_ingestion`, `media_analysis`, `video_composition`,
`storage`. Optional: `llm_generation`, `image_generation`,
`video_generation`, `tts`, `music_generation`, `subtitle_generation`,
`audio_processing`.

All required capabilities have tools in `tool_catalog/default.json`. The
supplemental generation capabilities are optional by design: a run with no
generation provider can still compose a story purely from user assets, which
matches the product rule that user assets are authoritative.

## Verification Scope

Minimum verification for this pipeline:

- Loader resolves `hybrid_story` and the snapshot round-trips.
- Preflight, execution plan, and stage manifest build for the new key.
- Database run creation stores a pipeline version snapshot and the review
  gates advance a smoke run to `final_review`.
- Placeholder artifacts validate against the new schema (it allows the
  runner's `{"stage_key", "status": "placeholder"}` shape).
